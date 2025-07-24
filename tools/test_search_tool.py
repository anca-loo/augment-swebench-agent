"""Unit tests for the SearchTool class.

This module contains tests for the core functionality of SearchTool,
including query execution and result handling.
"""

import unittest
from unittest.mock import patch, MagicMock
import pytest

from tools.search import SearchTool, ToolImplOutput


class SearchToolTest(unittest.TestCase):
    """Tests for the SearchTool class."""

    def setUp(self):
        """Set up test fixtures."""
        # Create the search tool
        self.search_tool = SearchTool()
        
        # Create a patch for the Exa search method
        self.exa_search_patch = patch('tools.search.exa.search')
        self.mock_exa_search = self.exa_search_patch.start()
        
        # Sample search results
        self.sample_results = [
            {"title": "Test Result 1", "url": "https://example.com/1"},
            {"title": "Test Result 2", "url": "https://example.com/2"},
        ]
        
        # Set up the mock to return sample results
        self.mock_exa_search.return_value = self.sample_results

        # self.mock_exa_tool_result = ToolImplOutput(
        #     output=self.sample_results,
        #     auxiliary_data=AuxiliaryData(num_results=2, query="python testing", titles=["Test Result 1", "Test Result 2"])
        # )

    def tearDown(self):
        """Tear down test fixtures."""
        self.exa_search_patch.stop()

    def test_init(self):
        """Test SearchTool initialization."""
        # Check that the tool has the expected attributes
        self.assertEqual(self.search_tool.name, "search")
        self.assertTrue("searching the web" in self.search_tool.description)
        self.assertIn("query", self.search_tool.input_schema["properties"])
        self.assertIn("num_results", self.search_tool.input_schema["properties"])

    def test_run_impl_basic_query(self):
        """Test running a basic search query."""
        # Run the search tool with a basic query
        result = self.search_tool.run_impl({
            "query": "python testing",
            "num_results": 2
        })
        print("Result", result)
        print(type(result))

        # Check that exa.search was called with the correct parameters
        self.mock_exa_search.assert_called_once_with("python testing", num_results=2)
        
        # Check the result
        self.assertEqual(result.tool_result_message, "Searched for 'python testing' and found 2 results.")
        self.assertEqual(result.auxiliary_data["success"], True)
        self.assertEqual(result.auxiliary_data["query"], "python testing")
        self.assertEqual(result.auxiliary_data["num_results"], 2)

    def test_run_impl_no_results(self):
        """Test handling when no results are found."""
        # Set up the mock to return empty results
        self.mock_exa_search.return_value = []
        
        # Run the search tool
        result = self.search_tool.run_impl({
            "query": "nonexistent query xyz123",
            "num_results": 5
        })
        
        # Check the result
        self.assertEqual(result.tool_output, "No results found for 'nonexistent query xyz123'.")
        self.assertEqual(result.tool_result_message, "Searched for 'nonexistent query xyz123' but found no results.")
        self.assertEqual(result.auxiliary_data["success"], True)
        self.assertEqual(result.auxiliary_data["query"], "nonexistent query xyz123")
        self.assertEqual(result.auxiliary_data["num_results"], 0)

    def test_run_impl_api_error(self):
        """Test handling of API errors."""
        # Make the mock raise an exception
        self.mock_exa_search.side_effect = Exception("API Error")
        
        # Run the search tool
        result = self.search_tool.run_impl({
            "query": "error query",
            "num_results": 3
        })
        
        
        # Check the result
        self.assertTrue("Error searching for" in result.tool_output)
        self.assertTrue("Error" in result.tool_result_message)
        self.assertEqual(result.auxiliary_data["success"], False)

    def test_default_num_results(self):
        """Test that default num_results is used when not provided."""
        # Run the search tool without specifying num_results
        result = self.search_tool.run_impl({
            "query": "default test"
        })

        print("Result", result)
        print(type(result))
        
        # Check that exa.search was called with the default num_results
        # Assuming default is 10, adjust as needed
        self.mock_exa_search.assert_called_once_with("default test", num_results=10)
        
        # Check the result
        self.assertEqual(result.auxiliary_data["num_results"], 2)

    def test_get_tool_start_message(self):
        """Test getting the tool start message."""
        message = self.search_tool.get_tool_start_message({
            "query": "start message test",
            "num_results": 5
        })
        
        # Check message
        self.assertEqual(message, "Searching the web for: start message test")


if __name__ == "__main__":
    unittest.main() 