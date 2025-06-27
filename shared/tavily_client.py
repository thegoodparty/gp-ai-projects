import os
import asyncio
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from tavily import TavilyClient

from shared.logger import get_logger

load_dotenv()

class SharedTavilyClient:
    """
    A shared wrapper for TavilyClient that provides common functionality,
    error handling, and logging integration across the application.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the shared Tavily client.
        
        Args:
            api_key: Optional API key. If not provided, will use TAVILY_API_KEY environment variable.
        """
        self.logger = get_logger(__name__)
        self.api_key = api_key or os.getenv("TAVILY_API_KEY")
        
        if not self.api_key:
            raise ValueError("Tavily API key is required. Set TAVILY_API_KEY environment variable or pass api_key parameter.")
        
        self.client = TavilyClient(api_key=self.api_key)
        self.logger.info("SharedTavilyClient initialized successfully")
    
    async def search(
        self, 
        query: str, 
        search_depth: str = "basic",
        topic: str = "general",
        days: Optional[int] = None,
        max_results: int = 5,
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
        include_answer: bool = False,
        include_raw_content: bool = False,
        include_images: bool = False
    ) -> Dict[str, Any]:
        """
        Perform a search using Tavily API with comprehensive error handling and logging.
        
        Args:
            query: Search query string
            search_depth: Depth of search ("basic" or "advanced")
            topic: Search topic ("general", "news", etc.)
            days: Number of days to search back (optional)
            max_results: Maximum number of results to return
            include_domains: List of domains to include in search
            exclude_domains: List of domains to exclude from search
            include_answer: Whether to include AI-generated answer
            include_raw_content: Whether to include raw content
            include_images: Whether to include images
            
        Returns:
            Dict containing search results
            
        Raises:
            Exception: If search fails
        """
        self.logger.info(f"Performing Tavily search for query: '{query}'")
        self.logger.debug(f"Search parameters: depth={search_depth}, topic={topic}, max_results={max_results}")
        
        try:
            # Prepare search parameters
            search_params = {
                "query": query,
                "search_depth": search_depth,
                "topic": topic,
                "max_results": max_results,
                "include_answer": include_answer,
                "include_raw_content": include_raw_content,
                "include_images": include_images
            }
            
            # Add optional parameters
            if days is not None:
                search_params["days"] = days
            if include_domains:
                search_params["include_domains"] = include_domains
            if exclude_domains:
                search_params["exclude_domains"] = exclude_domains
            
            # Perform the search asynchronously to avoid blocking the event loop
            result = await asyncio.to_thread(self.client.search, **search_params)
            
            self.logger.info(f"Tavily search completed successfully. Found {len(result.get('results', []))} results")
            self.logger.debug(f"Search result keys: {list(result.keys())}")
            
            return result
            
        except Exception as e:
            self.logger.error(f"Tavily search failed for query '{query}': {str(e)}")
            self.logger.debug(f"Search error details: {type(e).__name__}: {e}", exc_info=True)
            raise
    
    async def get_search_context(
        self, 
        query: str, 
        search_depth: str = "basic",
        max_results: int = 5,
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None
    ) -> str:
        """
        Get search context as a formatted string suitable for LLM processing.
        
        Args:
            query: Search query string
            search_depth: Depth of search ("basic" or "advanced")
            max_results: Maximum number of results to return
            include_domains: List of domains to include in search
            exclude_domains: List of domains to exclude from search
            
        Returns:
            Formatted string containing search context
        """
        self.logger.info(f"Getting search context for query: '{query}'")
        
        try:
            result = await self.search(
                query=query,
                search_depth=search_depth,
                max_results=max_results,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                include_answer=True,
                include_raw_content=True
            )
            
            context_parts = []
            
            if result.get("answer"):
                context_parts.append(f"AI Summary: {result['answer']}")
            
            if result.get("results"):
                context_parts.append("\nSearch Results:")
                for i, res in enumerate(result["results"], 1):
                    context_parts.append(f"\n{i}. {res.get('title', 'No Title')}")
                    context_parts.append(f"   URL: {res.get('url', 'No URL')}")
                    if res.get("content"):
                        context_parts.append(f"   Content: {res['content'][:500]}...")
            
            context = "\n".join(context_parts)
            self.logger.info(f"Search context generated successfully. Length: {len(context)} characters")
            
            return context
            
        except Exception as e:
            self.logger.error(f"Failed to get search context for query '{query}': {str(e)}")
            raise


if __name__ == "__main__":
    async def main():
        client = SharedTavilyClient()
        result = await client.get_search_context(query="local media outlets newspapers radio TV stations Chicopee, MA")
        print(result)
    
    asyncio.run(main())