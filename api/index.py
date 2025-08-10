"""
Vercel serverless function handler for the Campaign Plan Generator API.

This file wraps the existing FastAPI application to work with Vercel's serverless 
infrastructure. It sets up the Python path and imports the main FastAPI app.

"""

import os
import sys
from pathlib import Path

# Add the parent directory to Python path to import our modules.
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from api_wrapper import app as fastapi_app  # noqa: E402

# Vercel expects the ASGI app to be available as 'app'
app = fastapi_app
