# Getting started

Set up the relevant API keys in `.env` file (see setup instructions below).

## Setup Options

### Option 1: Using pip (Standard Python)

1. **Create and activate a virtual environment:**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

2. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

3. **Set up environment variables:**
   ```bash
   cp .env.example .env
   # Edit .env file and add your API keys
   ```

### Option 2: Using uv (Alternative)

```bash
uv sync
source .venv/bin/activate
```

## Required API Keys

You need the following API keys (add them to your `.env` file):

- **TAVILY_API_KEY**: Get from [tavily.com](https://tavily.com/)
- **GEMINI_API_KEY**: Get from [Google AI Studio](https://aistudio.google.com/app/apikey)
- **TOGETHER_API_KEY**: (Optional) Get from [TogetherAI](https://api.together.xyz/settings/api-keys)

## Running

### Individual Campaign Sections

Run individual sections to test:

```bash
python ai_generated_campaign_plan/sections/one_overview.py  # Replace with each section
```

For debug mode logging:

```bash
ENVIRONMENT=development python ai_generated_campaign_plan/sections/five_know_your_community.py
```

### API Server

To start the FastAPI web server for API endpoints:

```bash
source .venv/bin/activate  # Make sure virtual environment is active
python api_wrapper.py
```

The API will be available at `http://localhost:8000`:

- **Web Form**: `http://localhost:8000/` (open in browser)
- **API Endpoint**: `POST http://localhost:8000/generate-campaign-plan`
- **Health Check**: `GET http://localhost:8000/health`

See `README_API.md` for detailed API documentation.
