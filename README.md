# Getting started

make sure you have uv installed, set up the relevant api keys in .env

## Setup

run these commands to get you python env set up

```bash
uv sync
source .venv/bin/activate
```

## Running

run this to check each section

```bash
uv run ai_generated_campaign_plan/sections/one_overview.py  #replace with each section
```

for debug mode logging, for production level logging, replace development with production

```bash
ENVIRONMENT=development uv run ai_generated_campaign_plan/sections/five_know_your_community.py
```
