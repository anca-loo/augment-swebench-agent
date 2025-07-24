import docker
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Tuple
import platform
import shutil
from rich.console import Console

from utils.common import generate_patch
AUGMENT_ROOT = Path(__file__).parent.parent
MAX_DOCKER_CONCURRENCY = 4
console = Console()

def get_repo_info(problem_id: str) -> dict:
    """Extract repository information from the problem ID."""
    parts = problem_id.split("__")
    if len(parts) != 2:
        raise ValueError(f"Invalid problem ID format: {problem_id}")
    
    repo_name = parts[0]
    issue_number = parts[1].split("-")[0]
    
    return {
        "repo_name": repo_name,
        "issue_number": issue_number,
        "full_name": f"{repo_name}/{repo_name}",  # Most GitHub repos follow this pattern
        "clone_url": f"https://github.com/{repo_name}/{repo_name}.git"
    }

def get_issue_image_name(problem_id: str, workspace: Path) -> str:
    """Fetch a docker image for the issue."""
    issue_key = problem_id.replace("__", "_1776_")
    return f"swebench/sweb.eval.x86_64.{issue_key}:latest"


def set_volume_permissions(container_id, volume_path: Path):
    """Make the host‑side volume path readable/writable by the current user."""
    # macOS / Docker‑Desktop: path already owned by current user
    if platform.system() == "Darwin" and os.access(volume_path, os.W_OK):
        console.print(f"{volume_path} already writable; skipping chmod/chown.")
        return

    my_uid, my_gid = os.getuid(), os.getgid()
    console.print(f"Fixing permissions for {volume_path} to {my_uid}:{my_gid}")
    env = os.environ.copy()

    try:
        subprocess.check_call(
            ["sudo", "chmod", "a+rx", volume_path.as_posix()],
            env=env,
        )
    except subprocess.CalledProcessError as e:
        console.print(f"chmod failed on {volume_path}: {e}")

    try:
        subprocess.check_call(
            ["sudo", "chown", "-R", f"{my_uid}:{my_gid}", volume_path.as_posix()],
            env=env,
        )
    except subprocess.CalledProcessError as e:
        console.print(f"chown failed on {volume_path}: {e}")


def start_container(workspace: Path, problem_id: str, semaphore: Any) -> str:
    """Start a docker container for the issue."""
    console.print(f"[{problem_id}] START: workspace={workspace}")
    stop_container(f"sweb.augment.{problem_id}")
    image_name = get_issue_image_name(problem_id, workspace)
    console.print(f"Starting container for {problem_id}")
    client = docker.from_env()
    console.print(f"Pulling image {image_name}")
    with semaphore:
        client.images.pull(image_name)
    console.print(f"Running docker run for {image_name} in {workspace}")

    # host directory that is mounted into /testbed
    host_testbed = (workspace / f"{problem_id}_testbed").resolve()
    host_testbed.mkdir(parents=True, exist_ok=True)
  

    with semaphore:
        console.print(f"Starting run for {image_name}")
        container = client.containers.run(
            name=f"sweb.augment.{problem_id}_{uuid.uuid4().hex[:8]}",
            image=image_name,
            detach=True,
            command="bash -c 'git config --global user.email a && git config --global user.name a && git config --global --add safe.directory /testbed && git commit --allow-empty -m augment && sleep 7200'",  # Time out and die, eventually, if we are interrupted
        )
        console.print(f"Finished startup for {image_name}")
    # Give it a second to start
    time.sleep(5)

     # Create a directory for the agent to work in
    container_id = container.id
    assert container_id is not None
    console.print(f"Started {container_id} for {problem_id}")
    
    repo_link = workspace

    #  Make sure the target directory exists and is empty
    if repo_link.exists():
        console.print(f"[{problem_id}] Removing existing repo at {repo_link}")
        shutil.rmtree(repo_link, ignore_errors=True)
    
    repo_link.mkdir(parents=True, exist_ok=True)
    
    # Copy files directly from container to local workspace
    console.print(f"[{problem_id}] Copying files from container to {repo_link}")
    try:
        subprocess.run(
            ["docker", "cp", f"{container_id}:/testbed/.", str(repo_link)],
            check=True
        )
    except subprocess.CalledProcessError as e:
        console.print(f"[{problem_id}] Error copying from container: {e}")
        raise

    files_in_repo = list(repo_link.iterdir())
    console.print(f"[{problem_id}] Files in repo_link: {len(files_in_repo)}, including: {files_in_repo[:5]}")
    


   
    # List files in container for debugging
    console.print(f"[{problem_id}] Listing files in container:")
    subprocess.run([
        "docker", "exec", container_id, "ls", "-la", "/testbed", "|", "head", "-n", "5"
    ], check=False)
    # Initialize git in the copied directory
    
    # Verify git repo validity
    git_check = subprocess.run(
        ["git", "-C", str(repo_link), "rev-parse", "--git-dir"],
        capture_output=True, text=True, check=False
    )
    if git_check.returncode != 0:
        console.print(f"[{problem_id}] Git repo is invalid: {git_check.stderr}")
    else:
        console.print(f"[{problem_id}] Git repo is valid")

    # no permission fix needed - host_testbed is already writable
    console.print(f"[{problem_id}] Container setup COMPLETE")

    #  check if we cna generate a patch
    try:
        diff = generate_patch(repo_link)
        console.print(f"[{problem_id}] Generated patch: {diff}")
    except Exception as e:
        console.print(f"[{problem_id}] Failed to generate patch: {e}")

    return container_id


def remove_container_image(image_name: str) -> None:
    """Remove a docker image."""
    try:
        client = docker.from_env()
        client.images.remove(image=image_name, force=True)
        console.print(f"Removed image {image_name}")
    except docker.errors.APIError as e:  # type: ignore
        console.print(f"Failed to remove image {image_name}: {e}")


def stop_container(container_id: str, remove_image: str = "") -> None:
    """Stop a docker container for the issue."""
    container = None
    try:
        client = docker.from_env()
        container = client.containers.get(container_id)
    except Exception as e:
        console.print(f"Container {container_id} not found: {e}")

    if container:
        try:
            console.print(f"Stopping container {container_id}")
            container.stop()
            console.print(f"Stopped container {container_id}")
        except docker.errors.NotFound as e:  # type: ignore
            console.print(f"Failed to stop container {container_id}: {e}")
        except docker.errors.APIError as e:  # type: ignore
            console.print(f"Failed to stop container {container_id}: {e}")
        try:
            console.print(f"Removing container {container_id}")
            container.remove()
            time.sleep(10)
            console.print(f"Removed container {container_id}")
        except docker.errors.NotFound as e:  # type: ignore
            console.print(f"Failed to stop container {container_id}: {e}")
        except docker.errors.APIError as e:  # type: ignore
            console.print(f"Failed to stop container {container_id}: {e}")

    if remove_image:
        # Add a small delay to ensure container removal is complete
        time.sleep(5)
        remove_container_image(remove_image)


def setup_workspace(
    workspace: Path, problem_id: str, lock: Any, semaphore: Any
) -> Tuple[Dict[str, str], str]:
    """Setup the workspace for the agent."""
    env: Dict[str, str] = os.environ.copy()

    # Create a conda environment; we don't use it, but it protects the
    # agent's environment from changes.
    workspace.mkdir(parents=True, exist_ok=True)
    
    # Create the problem directory within the workspace to avoid nested path issues
    problem_dir = workspace / problem_id
    problem_dir.mkdir(parents=True, exist_ok=True)
    
    # Multiple simultaneous conda installs are no good.
    with lock:
        subprocess.check_output(
            [
                "conda",
                "create",
                "-y",
                "-q",
                "-p",
                str(workspace / "conda_3.11"),
                "python==3.11",
            ]
        )

    env["ISSUE_ID"] = problem_id
    env["SWEBENCH_WORKSPACE"] = str(workspace)
    env["PATH"] = f"{workspace}/python_wrappers/bin:{workspace}/conda_3.11/bin" + (
        f":{env['PATH']}" if "PATH" in env else ""
    )
    env["PYTHONPATH"] = f"{AUGMENT_ROOT}" + (
        f":{env['PYTHONPATH']}" if "PYTHONPATH" in env else ""
    )
    # for k, v in env.items():
    #     console.print(f"ENV {k}=={v}")

    # Copy the python wrapper into the workspace
    container_id = start_container(workspace, problem_id, semaphore)

    return env, container_id
