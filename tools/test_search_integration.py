from tools.search import SearchTool

if __name__ == "__main__":
    search_tool = SearchTool()
    result = search_tool.run_impl({
        "query": "python testing",
        "num_results": 2
    })
    print(result)