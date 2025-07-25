#!/usr/bin/env python3
"""
Script to run the agent on a SWE-bench problem in a Docker container.

This script loads a SWE-bench problem, starts a Docker container for it,
and runs the agent inside the container by calling cli.py.
"""

from functools import partial
import os
import logging
import threading
import sys
import json
import argparse
from pathlib import Path
from multiprocessing import Pool, Manager
import time
import numpy as np

from rich.console import Console
from rich.panel import Panel
from datasets import load_dataset

from utils.docker_utils import MAX_DOCKER_CONCURRENCY, setup_workspace, stop_container
from utils.common import generate_patch
from cli import main as cli_main
import uuid
from utils.swebench_eval_utils import get_dataset_name, run_evaluation

console = Console()

def run_eval_on_single_problem(problem_id: str, workspace_path: Path, console: Console):
    eval_file = None

    eval_outcomes = {
        "is_success": False,
    }

    try:
        run_evaluation(
            predictions_file=workspace_path / "predictions.json",
            dataset=get_dataset_name(
                "full"
            ),  # Always use the full dataset for evaluation.
            run_id=problem_id,
            swebench_venv_path=Path(
                f"{os.environ['HOME']}/swebench_eval_tools_env/bin/python"
            ),
            console=console,
        )
        eval_file = workspace_path / f"augment-agent.{problem_id}.json"
        eval_dict = json.loads(eval_file.read_text())
        eval_outcomes["is_success"] = problem_id in eval_dict["resolved_ids"]
        console.print(f"Evaluated {problem_id} successfully.")
    except FileNotFoundError as exc:
        console.print(f"Failed to report results for {problem_id}")
        console.print(exc)
    return eval_outcomes


def run_agent_on_single_problem(
    problem_id: str,
    problem_statement: str,
    rollout_idx: int,
    workspace_base_path: Path,
    lock: threading.Lock,
    semaphore: threading.Semaphore,
) -> tuple[str, float, dict]:
    """
    Run the agent on a single SWE-bench problem.

    Args:
        problem_id: The ID of the problem
        problem_statement: The problem statement
        lock: Threading lock for Docker operations
        semaphore: Threading semaphore for Docker operations

    Returns:
        dict: The diff data generated by the agent
        float: The time taken to generate the diff
        dict: The evaluation outcomes
    """
    console = Console()
    logs_prefix = f"[bold blue]{problem_id}[/bold blue]"

    workspace_path = workspace_base_path / problem_id / f"rollout_{rollout_idx}"
    output_file = workspace_path / "agent_logs.txt"

    # Ensure workspace directory exists
    workspace_path.mkdir(parents=True, exist_ok=True)

    # Start the Docker container
    container_id = None

    try:
        env, container_id = setup_workspace(workspace_path, problem_id, lock, semaphore)
        console.print(f"{logs_prefix} Docker container started with ID: {container_id}")

        # Set environment variables
        for key, value in env.items():
            os.environ[key] = value

        # Save original sys.argv
        original_argv = sys.argv.copy()

        # Create new sys.argv for cli.py
        cli_args = [
            "cli.py",
            "--workspace",
            str(workspace_path),
            "--problem-statement",
            problem_statement,
            "--docker-container-id",
            container_id,
            "--use-container-workspace",
            "/testbed",
            "--minimize-stdout-logs",
        ]

        # Set logs path if output_file is specified
        if output_file:
            cli_args.extend(["--logs-path", str(output_file)])

        # Replace sys.argv with our custom arguments
        sys.argv = cli_args

        # Run the agent via cli.py
        console.print(f"{logs_prefix} Starting agent run...")
        start_time = time.time()
        cli_main()
        agent_duration = time.time() - start_time
        console.print(f"{logs_prefix} Agent run completed in {agent_duration:.2f}s.")

        # Restore original sys.argv
        sys.argv = original_argv

        # Generate patch after the agent has completed its work
        repo_path = str(workspace_path)
        console.print(f"Generating patch in {repo_path}")
        diff = generate_patch(repo_path)

        
        with (workspace_path / "predictions.json").open("w") as f:
            json.dump(
                [
                    {
                        "instance_id": problem_id,
                        "model_name_or_path": "augment-agent",
                        "model_patch": diff,
                        # "search_tool_calls": self.num_search_tool_calls,
                    }
                ],
                f,
                indent=2,
            )
            
        # Also save to /evals folder
        evals_dir = Path("./evals")
        console.print(f"Saving predictions to {evals_dir / f'{problem_id}_predictions.json'}")
        evals_dir.mkdir(exist_ok=True, parents=True)
        with (evals_dir / f"{problem_id}_predictions.json").open("w") as f:
            json.dump(
                [
                    {
                        "instance_id": problem_id,
                        "model_name_or_path": "augment-agent",
                        "model_patch": diff,
                        # "search_tool_calls": self.num_search_tool_calls,
                    }
                ],
                f,
                indent=2,
            )
    finally:
        # Stop and clean up the Docker container
        if container_id is not None:
            console.print(f"{logs_prefix} Stopping Docker container...")
            stop_container(container_id)
            console.print(f"{logs_prefix} Docker container stopped")

    # Evaluate the generated diff
    console.print(f"{logs_prefix} Evaluating the generated diff...")
    start_time = time.time()
    eval_outcomes = run_eval_on_single_problem(problem_id, workspace_path, console)
    eval_duration = time.time() - start_time
    console.print(f"{logs_prefix} Evaluation completed in {eval_duration:.2f}s.")

    assert diff is not None
    return diff, agent_duration, eval_outcomes


def should_process_issue(problem_id):
    # Check if predictions file exists in evals directory
    console.print(f"Checking if {problem_id} should be processed")
    evals_dir = Path("./evals")
    prediction_file = evals_dir / f"{problem_id}_predictions.json"
    console.print(f"Prediction file: {prediction_file}")
    
    # Skip if file already exists
    if prediction_file.exists():
        console.print(f"Skipping {problem_id} - already processed (found in /evals)")
        return False
    console.print(f"Processing {problem_id}")
    return True


def main():
    """Main entry point for the script."""
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="Run the agent on SWE-bench problems")
    parser.add_argument(
        "--num-examples",
        type=int,
        default=None,
        help="Optionally, specify the number of examples to run on",
    )
    parser.add_argument(
        "--shard-ct",
        type=int,
        default=1,
        help="Number of shards to split the work into",
    )
    parser.add_argument(
        "--shard-id", type=int, default=0, help="Shard ID to run (0-indexed)"
    )
    parser.add_argument(
        "--num-processes",
        type=int,
        default=8,
        help="Number of processes to use for each example",
    )
    parser.add_argument(
        "--num-candidate-solutions",
        type=int,
        default=8,
        help="Number of candidate solutions to generate for each example",
    )

    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Check if ANTHROPIC_API_KEY is set
    if "ANTHROPIC_API_KEY" not in os.environ:
        print("Error: ANTHROPIC_API_KEY environment variable is not set.")
        print("Please set it to your Anthropic API key.")
        sys.exit(1)

    # Initialize console
    console = Console()

    # Load the SWE-bench dataset
    console.print("Loading SWE-bench dataset...")
    swebench_dataset = load_dataset("princeton-nlp/SWE-bench_Verified")[  # pyright: ignore[reportIndexIssue]
        "test"
    ].to_pandas()  # pyright: ignore

    # Sharding
    num_examples_per_shard = len(swebench_dataset) // args.shard_ct  # pyright: ignore[reportArgumentType]
    examples = swebench_dataset.iloc[  # pyright: ignore[reportAttributeAccessIssue]
        args.shard_id * num_examples_per_shard : (args.shard_id + 1)
        * num_examples_per_shard
    ]

    # Get the number of examples to run
    assert args.num_examples is None or args.num_examples <= len(examples), (
        f"num_examples ({args.num_examples}) is greater than the number of examples in the shard ({len(examples)}). Either decrease num_examples or decrease the number of shards."
    )
    num_examples = args.num_examples if args.num_examples is not None else len(examples)
    console.print(
        f"Running on {num_examples} examples from shard {args.shard_id} out of {args.shard_ct} shards."
    )
    console.print(
        f"We will generate {args.num_candidate_solutions} candidate solutions for each example with parallelism of {args.num_processes}."
    )

    # print out all example ids we'll be processing
    console.print(
        "Selected examples:",
        "\n - " + "\n - ".join(examples.iloc[:num_examples]["instance_id"].tolist()),
    )

    # List to store all diff data
    all_diff_data = []

    # get workspace base dir
    workspace_base_path = Path(f"/tmp/workspace/{uuid.uuid4().hex[:8]}").resolve()
    console.print(f"Workspace base path: {workspace_base_path}")

    output_path = f"pre-ensemble_results_shard{args.shard_id}_of_{args.shard_ct}.jsonl"

    # Add timing for the entire process
    overall_start_time = time.time()

    # Iterate over the specified number of examples
    for i in range(num_examples):
        try:
            problem = examples.iloc[i]
            problem_id = problem["instance_id"]
            problem_statement = problem["problem_statement"]

            console.print(f"\nProcessing example {i + 1}/{num_examples}")

            if should_process_issue(problem_id):
                # Run the agent on the selected problem
                with Manager() as manager:
                    lock = manager.Lock()
                    semaphore = manager.Semaphore(MAX_DOCKER_CONCURRENCY)
                    with Pool(processes=args.num_processes) as pool:
                        diffs = pool.starmap(
                            partial(
                                run_agent_on_single_problem,
                                lock=lock,
                                semaphore=semaphore,
                                workspace_base_path=workspace_base_path,
                            ),
                            [
                                (problem_id, problem_statement, rollout_idx)
                                for rollout_idx in range(args.num_candidate_solutions)
                            ],
                        )
                        diffs, agent_durations, eval_outcomes = zip(*diffs)
                    median_duration = np.median(agent_durations)
                    diff_data = {
                        "id": problem_id,
                        "instruction": problem_statement,
                        "diffs": diffs,
                        "agent_durations": agent_durations,
                        "median_duration": median_duration,
                        "eval_outcomes": eval_outcomes,
                    }
                    all_diff_data.append(diff_data)

                # Save the results after each example in case of failures
                with open(output_path, "w") as f:
                    for diff_data in all_diff_data:
                        f.write(json.dumps(diff_data) + "\n")

                console.print(f"Completed example {i + 1}/{num_examples}")
        except Exception as e:
            console.print(f"Error processing example {i + 1}: {str(e)}")
            continue

    all_durations = [d["median_duration"] for d in all_diff_data]
    # print out latencies at 25perc, min, max 75perc
    if len(all_durations) > 0:
        console.print(f"Rollout latency min: {np.min(all_durations)}")
        console.print(f"Rollout latency at 25perc: {np.percentile(all_durations, 25)}")
        console.print(f"Rollout latency at median: {np.median(all_durations)}")
        console.print(f"Rollout latency at 75perc: {np.percentile(all_durations, 75)}")
        console.print(f"Rollout latency max: {np.max(all_durations)}")

    console.print(f"\nAll examples processed. Results saved to {output_path}")
    console.print("Done!")

    # Calculate overall duration
    overall_duration = time.time() - overall_start_time
    console.print(f"\nTotal execution time: {overall_duration:.2f}s")
    console.print(f"Average time per example: {overall_duration/num_examples:.2f}s")

    ensemble_instruction = Panel(
        f"""
Now you have generated rollouts ({args.num_candidate_solutions} per problem) for {num_examples} problems and collected eval results for each rollout.

You can manually analyze results by looking into the workspace directory: {workspace_base_path}. You'll be interested to look at files like:
- agent_logs.txt: The logs from the agent
- predictions.json: The diff generated by the agent
- augment-agent.<problem_id>.json: The eval results
- logs/run_evaluation/<problem_id>/augment-agent/<problem_id>/test_output.txt: The raw output from running tests during eval step
- logs/run_evaluation/<problem_id>/augment-agent/<problem_id>/report.json: Structured enumeration of what tests failed vs passed duringe eval step.
    - FAIL_TO_PASS tests are testing new functionality.
    - PASS_TO_PASS tests are testing existing functionality to make sure the diff didn't break any existing features.

The user has two next steps:
- aggregate results across shards
- run the ensembler to select the best solution for each problem

STEP 1: Aggregate results across shards
--------
Each shard output is a JSONL of reuslts. We simply need to concatenate these to merge them.
(Path for this shard's output: {output_path})

Run `python merge_shards.py --input <list of all shard output paths> --output pre-ensemble_result_all_shards.jsonl`

Next step for user is to run the ensembling step with command. Make sure to set OPENAI_API_KEY environment variable before running the command!
--------
python majority_vote_ensembler.py pre-ensemble_result_all_shards.jsonl --output_path ensembler_results_all_shards.json
--------
        """,
        title="Action items for user",
        border_style="blue",
        padding=(1, 2),
    )
    console.print(ensemble_instruction)


if __name__ == "__main__":
    main()
