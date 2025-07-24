from typing import Optional, TypedDict
from exa_py import Exa
from utils.common import DialogMessages, LLMTool, ToolImplOutput
from rich.console import Console

console = Console()

exa = Exa("79ce2f4b-4751-46c4-9148-e198630a99ec")

class SearchResult(TypedDict):
    """A search result."""
    title: str
    url: str

class AuxiliaryData(TypedDict):
    """Auxiliary data for the search tool."""
    num_results: int
    query: str
    titles: list[str]


class SearchTool(LLMTool):
    """A tool for searching the web."""

    name = "search"
    description = """A tool for searching the web. This tool is useful for finding information
    about a topic you might be lacking context for. For example, the latest changes in javascript 
    tooling or stack overflow posts about a specific bug. You will be given a list of results, and  each
    result will have a title and a url.
    
    When to use this tool:
    - You need to find information about a topic you are lacking context for.
    - You need to find the latest information about a topic.
    - You need to find stack overflow posts about a specific bug.

    Key features:
    - You can adjust num_results to retrieve more or less results up to 100 results
    - You can search for multiple queries at once by calling this tool multiple times

    Parameters:
    - query: The query to search for.
    - num_results: The number of results to return.
    """

    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The query to search for"},
            "num_results": {"type": "integer", "description": "The number of results to return"},
        },
        "required": ["query"],
        
    }

    def get_tool_start_message(self, tool_input: dict[str,  str | int]) -> str:
        """Return a user-friendly message to be shown to the model when the tool is called."""
        return f"Searching the web for: {tool_input['query']}"

    def run_impl(self, tool_input: dict[str,  str | int], dialog_messages: Optional[DialogMessages] = None) -> ToolImplOutput:
        """Run the search tool."""
        query = tool_input["query"]
        num_results = tool_input.get("num_results", 5)
        try:
            result= exa.search_and_contents(query, num_results=num_results, text= {"max_characters": 2000})
            results = result.results
            # console.print(f"Search results: {results[0]}")
            
            if not results:
                return ToolImplOutput(
                    tool_output=f"No results found for '{query}'.",
                    tool_result_message=f"Searched for '{query}' but found no results.",
                    auxiliary_data={
                        "success": True,
                        "query": query,
                        "num_results": 0
                    }
                )
            
            # Format the results as a nicely formatted string
            formatted_results = []
            for i, result in enumerate(results, 1):
                formatted_results.append(f"{i}. {result.title}\n   URL: {result.url} \n   Content: {result.text}")
            
            # Join all results into a single string
            formatted_output = "\n\n".join(formatted_results)
            
            return ToolImplOutput(
                tool_output=formatted_output,
                tool_result_message=f"Searched for '{query}' and found {len(results)} results.",
                auxiliary_data={
                    "success": True,
                    "query": query,
                    "num_results": len(results),
                    "raw_results": formatted_results 
                }
            )
        except Exception as e:
            return ToolImplOutput(
                tool_output=f"Error searching for '{query}': {str(e)}",
                tool_result_message=f"Error searching for '{query}': {str(e)}",
                auxiliary_data={"success": False, "query": query}
            )
