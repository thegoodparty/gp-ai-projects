---
name: Braintrust Integration Plan
overview: Integrate Braintrust directly into the hierarchical discovery pipeline to log LLM inputs/outputs from cluster analysis prompts, with automatic enablement when BRAINTRUST_API_KEY is present.
todos:
  - id: "1"
    content: Add braintrust>=0.3.10 dependency to pyproject.toml dependencies list
    status: completed
  - id: "2"
    content: Create serve/hierarchical_discovery/braintrust_integration.py with init_braintrust, traced_llm_call, load_prompt_from_braintrust, flush_logs, is_enabled, and CLUSTER_ANALYSIS_PROMPT_TEMPLATE
    status: completed
  - id: "3"
    content: "Modify serve/hierarchical_discovery/stages/multi_cluster_analyzer.py: add imports for braintrust_integration functions"
    status: completed
  - id: "4"
    content: "Modify serve/hierarchical_discovery/stages/multi_cluster_analyzer.py: call init_braintrust() in __init__ method"
    status: completed
  - id: "5"
    content: "Modify serve/hierarchical_discovery/stages/multi_cluster_analyzer.py: wrap LLM call in _analyze_single_cluster() with traced_llm_call()"
    status: completed
  - id: "6"
    content: Update .env.example to include BRAINTRUST_API_KEY=your-braintrust-api-key and BRAINTRUST_PROJECT=hierarchical-discovery (optional)
    status: completed
  - id: "7"
    content: Update root README.md to document BRAINTRUST_API_KEY in Required API Keys section
    status: completed
  - id: "8"
    content: Update serve/hierarchical_discovery/README.md to add Braintrust section explaining automatic enablement and log viewing
    status: completed
---

# Braintrust Integration Plan

## Overview

Integrate Braintrust for LLM observability into the hierarchical discovery pipeline. Braintrust will automatically enable when `BRAINTRUST_API_KEY` is present and log cluster analysis LLM calls for prompt evaluation and iteration.

## Integration Components

### 1. Dependency Management

**File**: `pyproject.toml`

- Add `braintrust>=0.3.10` to the `dependencies` list (line 44)

### 2. Core Integration Module

**File**: `serve/hierarchical_discovery/braintrust_integration.py`

- Create the main integration module with:
  - `init_braintrust()` - Initialize Braintrust logger with project/API key
  - `traced_llm_call()` - Wrap LLM calls with Braintrust tracing
  - `load_prompt_from_braintrust()` - Load prompts from Braintrust UI with local fallback
  - `flush_logs()` - Flush pending logs
  - `is_enabled()` - Check if Braintrust is enabled
  - Default prompt templates (e.g., `CLUSTER_ANALYSIS_PROMPT_TEMPLATE`)
- Uses environment variables: `BRAINTRUST_API_KEY`, `BRAINTRUST_PROJECT`
- Gracefully degrades if API key not set (local-only mode)
- Handles missing `braintrust` package gracefully

### 3. Environment Configuration

**Files**: `.env.example`, `README.md`

- Add `BRAINTRUST_API_KEY=your-braintrust-api-key` to `.env.example`
- Add `BRAINTRUST_PROJECT=hierarchical-discovery` to `.env.example` (optional)
- Document in main `README.md` under "Required API Keys" section

### 4. MultiClusterAnalyzer Integration

**File**: `serve/hierarchical_discovery/stages/multi_cluster_analyzer.py`

Modifications:

- Import Braintrust integration functions at top:
  ```python
  from ..braintrust_integration import init_braintrust, traced_llm_call, is_enabled
  ```

- In `__init__()` method: Call `init_braintrust()` to initialize if API key present
- In `_analyze_single_cluster()` method: Wrap the LLM call that uses `_create_analysis_prompt()` with `traced_llm_call()`
- Structure the input_data for clean Braintrust UI display:
  ```python
  input_data = {
      "cluster_info": {
          "cluster_id": cluster_id,
          "total_messages": len(messages),
          "unique_citizens": person_metrics['unique_respondents'],
          ...
      },
      "messages": example_texts[:10]
  }
  ```


### 5. Orchestrator Integration (Optional)

**File**: `serve/hierarchical_discovery/orchestrator.py`

- Alternative approach: Initialize Braintrust at orchestrator level before creating MultiClusterAnalyzer
- This ensures one-time initialization for all cluster analyses
- Add log flush at end of pipeline

### 6. Documentation Updates

**Files**:

- `README.md` (root) - Add BRAINTRUST_API_KEY to environment setup
- `serve/hierarchical_discovery/README.md` - Add Braintrust section

Document:

- How to get Braintrust API key (https://www.braintrust.dev/)
- How to view logs in Braintrust UI
- Prompt management workflow (optional feature)
- Automatic enablement (no config needed)

## Implementation Steps

1. **Add dependency** to `pyproject.toml`
2. **Create integration module** `braintrust_integration.py` with all core functions
3. **Modify MultiClusterAnalyzer**:

   - Add import statements
   - Initialize Braintrust in `__init__()`
   - Wrap LLM call in `_analyze_single_cluster()` with `traced_llm_call()`

4. **Update environment template** `.env.example` with BRAINTRUST_API_KEY
5. **Update documentation** in README files

## Key Design Decisions

- **Automatic Enablement**: Braintrust enables when `BRAINTRUST_API_KEY` is present, no config flag needed
- **Graceful Degradation**: Works without API key (local-only mode, no errors)
- **Targeted Logging**: Only logs cluster analysis prompts (from `_create_analysis_prompt`)
- **No POC Files**: Direct integration into main pipeline, no separate POC/CLI files
- **Structured Input**: Clean input/output data structure for Braintrust UI evaluation

## Files to Create/Modify

**New Files**:

- `serve/hierarchical_discovery/braintrust_integration.py` (~400 lines)

**Modified Files**:

- `pyproject.toml` (add braintrust dependency)
- `serve/hierarchical_discovery/stages/multi_cluster_analyzer.py` (add imports, init, wrap LLM call)
- `.env.example` (add BRAINTRUST_API_KEY)
- `README.md` (document Braintrust setup)
- `serve/hierarchical_discovery/README.md` (add Braintrust section)

## Environment Variables

- `BRAINTRUST_API_KEY` - Optional, enables Braintrust logging when present
- `BRAINTRUST_PROJECT` - Optional, defaults to "hierarchical-discovery"
- `GEMINI_API_KEY` - Already required

## Code Changes in MultiClusterAnalyzer

The main change is in the `_analyze_single_cluster()` method where the primary LLM call happens:

```python
# Before (current):
response = self.llm_client.generate_structured_content(
    prompt=prompt,
    response_schema=ClusterAnalysisResponse,
    system_instruction=system_instruction
)

# After (with Braintrust):
response = traced_llm_call(
    name="cluster_analysis",
    input_data=input_data,  # Clean structured data
    llm_call_fn=lambda: self.llm_client.generate_structured_content(
        prompt=prompt,
        response_schema=ClusterAnalysisResponse,
        system_instruction=system_instruction
    ),
    prompt=prompt,  # Stored in metadata
    metadata={"cluster_id": cluster_id, "k": k}
)
```

## Structured Input/Output Formatting for Braintrust

**Key Design Pattern**: Separate what users see in Braintrust UI from debugging information.

### Input Data Structuring

Create a clean, structured dictionary before the LLM call:

```python
import yaml

# Build clean structured input for Braintrust UI display
input_data = {
    "cluster_info": {
        "cluster_id": cluster_id,
        "total_messages": len(messages),
        "unique_citizens": person_metrics['unique_respondents'],
        "avg_mentions_per_citizen": round(person_metrics['avg_mentions_per_respondent'], 1),
        "respondent_coverage_pct": round(person_metrics['respondent_coverage_pct'], 1),
        "total_clusters": k
    },
    "messages": example_texts[:10]  # Sample messages for display
}
```

**Why this works:**

- Braintrust automatically renders the dict as formatted JSON in the UI
- Nested structure makes it easy to see cluster metadata vs actual messages
- Limited to 10 messages to keep UI readable

### Prompt Formatting with YAML

The actual prompt sent to the LLM can format this data as YAML for better readability:

```python
# Format input as YAML for the prompt
input_yaml = yaml.dump(input_data, default_flow_style=False, sort_keys=False)

# Create prompt with YAML-formatted data
prompt = f"""Analyze this cluster of civic engagement messages.

INPUT:
{input_yaml}

Provide an analysis with these fields:
1. **Theme**: 2-4 word label
2. **Issues Summary**: 1 sentence
3. **Detailed Analysis**: 2-3 paragraphs
..."""
```

### Separation of Concerns in traced_llm_call

```python
response = traced_llm_call(
    name="cluster_analysis",
    input_data=input_data,        # ← Clean dict, shows in Braintrust UI as JSON
    llm_call_fn=lambda: ...,      # ← The actual LLM call
    prompt=prompt,                 # ← Full prompt with YAML, stored in metadata (for debugging)
    metadata={                     # ← Additional debugging info
        "cluster_id": cluster_id,
        "k": k,
        "model": "flash"
    }
)
```

**What users see in Braintrust:**

- **Input tab**: Clean JSON structure of `input_data`
- **Output tab**: Structured response (theme, summary, analysis, etc.)
- **Metadata tab**: Full prompt text, cluster_id, k, model, duration_ms

**Benefits:**

1. **Clean UI**: Users can quickly scan cluster info and sample messages
2. **Searchable**: JSON structure allows filtering/searching in Braintrust
3. **Comparable**: Consistent structure across all logged calls enables evaluation
4. **Debuggable**: Full prompt still accessible in metadata if needed

### Output Handling

The `traced_llm_call` function automatically handles structured outputs:

```python
# In braintrust_integration.py
if hasattr(result, 'model_dump'):
    output_data = result.model_dump()  # Pydantic models → dict
elif hasattr(result, '__dict__'):
    output_data = result.__dict__
else:
    output_data = {"result": str(result)}
```

This converts Pydantic `ClusterAnalysisResponse` objects into clean JSON for Braintrust:

```json
{
  "category": "Infrastructure",
  "theme": "Road Repair Needs",
  "issues_summary": "Citizens reporting deteriorating road conditions",
  "detailed_analysis": "...",
  "key_topics": ["potholes", "road conditions", "maintenance"],
  "sentiment": "concerned",
  "action_items": ["Increase road maintenance budget", "..."],
  "civic_relevance": "...",
  "confidence": "High"
}
```