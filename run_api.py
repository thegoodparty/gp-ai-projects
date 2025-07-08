#!/usr/bin/env python3
"""
Run the Campaign Plan Generator API
"""
import uvicorn
import sys
import os

# Add the current directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    print("🚀 Starting Campaign Plan Generator API...")
    print("📋 Web Form: http://localhost:8000")
    print("📊 Health Check: http://localhost:8000/health")
    print("🔧 API Docs: http://localhost:8000/docs")
    print("=" * 50)
    
    try:
        uvicorn.run(
            "api_wrapper:app",
            host="0.0.0.0",
            port=8000,
            reload=True,
            log_level="info"
        )
    except KeyboardInterrupt:
        print("\n👋 API server stopped")
    except Exception as e:
        print(f"❌ Error starting API: {e}")
        sys.exit(1) 