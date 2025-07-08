import os
import sys
from mangum import Mangum

# Add the current directory to the path so we can import modules
sys.path.insert(0, os.path.dirname(__file__))

from api_wrapper import app

handler = Mangum(app, lifespan="off") 