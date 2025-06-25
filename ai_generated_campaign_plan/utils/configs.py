import os
from dotenv import load_dotenv

load_dotenv()

def get_server_configs():
    """
    Get MCP server configurations.
    
    Returns:
        dict: Dictionary of server configurations with API keys loaded from environment
    """
    return {
        'brave': {
            'params': {
                "command": "npx", 
                'args': ["-y", "@modelcontextprotocol/server-brave-search"], 
                'env': {"BRAVE_API_KEY": os.getenv("BRAVE_API_KEY")}
            }
        },
        'tavily': {
            'params': {
                "command": "npx", 
                'args': ["-y", "tavily-mcp@0.2.4"], 
                'env': {"TAVILY_API_KEY": os.getenv("TAVILY_API_KEY")}
            }
        },
        'playwright': {
            'params': {
                "command": "npx", 
                'args': ["-y", "playwright-mcp@latest", "--headless"]
            }
        },
        'fetch': {
            'params': {
                "command": "uvx", 
                'args': ["mcp-server-fetch@latest"]
            }
        }
    }