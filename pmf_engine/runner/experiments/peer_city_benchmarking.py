from pathlib import Path

_instruction_path = Path(__file__).parent / "instructions" / "peer_city_benchmarking.md"
_instruction = _instruction_path.read_text()

EXPERIMENT = {
    "instruction": _instruction,
    "contract": {
        "type": "json",
        "s3_key_template": "{experiment_id}/{run_id}/peer_city_benchmarking.json",
        "schema": {
            "official_name": "string",
            "office": "string",
            "district": {"state": "string", "name": "string"},
            "generated_at": "string",
            "based_on_district_intel_run": "string",
            "summary": {
                "home_city_population": "number",
                "peer_cities_analyzed": "number",
                "issues_compared": "number",
                "sources_consulted": "number",
            },
            "home_city": {
                "name": "string",
                "state": "string",
                "population": "number",
            },
            "peer_cities": [{
                "name": "string",
                "state": "string",
                "population": "number",
                "similarity_reason": "string",
            }],
            "comparisons": [{
                "issue": "string",
                "home_city_approach": "string",
                "peer_approaches": [{
                    "city": "string",
                    "approach": "string",
                    "outcome": "string",
                    "budget": "string",
                    "timeline": "string",
                    "sources": [{"id": "number", "name": "string", "url": "string", "date": "string"}],
                }],
                "takeaways": "string",
            }],
            "methodology": "string",
        },
        "constraints": {
            "array_length": [
                {"path": "peer_cities", "min": 1},
                {"path": "comparisons", "min": 1},
                {"path": "comparisons[].peer_approaches", "min": 1},
                {"path": "comparisons[].peer_approaches[].sources", "min": 1},
            ],
            "equals": [
                {"left": "summary.peer_cities_analyzed", "right": {"count": "peer_cities"}},
                {"left": "summary.issues_compared", "right": {"count": "comparisons"}},
            ],
        },
    },
    "harness": "claude_sdk",
    "model": "sonnet",
    "mode": "serve",
    "max_turns": 60,
    "cpu": "2048",
    "memory": "4096",
    "timeout_seconds": 1800,
}
