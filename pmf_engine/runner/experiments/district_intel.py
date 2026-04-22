from pathlib import Path

_instruction_path = Path(__file__).parent / "instructions" / "district_intel.md"
_instruction = _instruction_path.read_text()

EXPERIMENT = {
    "instruction": _instruction,
    "contract": {
        "type": "json",
        "s3_key_template": "{experiment_id}/{run_id}/district_intel.json",
        "schema": {
            "official_name": "string",
            "office": "string",
            "district": {
                "state": "string",
                "type": "string",
                "name": "string",
            },
            "generated_at": "string",
            "summary": {
                "total_constituents": "number",
                "issues_identified": "number",
                "meetings_analyzed": "number",
                "sources_consulted": "number",
            },
            "issues": [{
                "title": "string",
                "summary": "string",
                "status": "string",
                "affected_constituents": "number",
                "affected_segments": [{
                    "name": "string",
                    "count": "number",
                    "description": "string",
                }],
                "sources": [{
                    "id": "number",
                    "name": "string",
                    "url": "string",
                    "date": "string",
                }],
            }],
            "demographic_snapshot": {
                "total_voters": "number",
                "party_breakdown": [{"party": "string", "count": "number"}],
                "age_distribution": [{"range": "string", "count": "number"}],
            },
            "methodology": "string",
        },
        "constraints": {
            "enums": [
                {"path": "issues[].status", "values": ["active", "upcoming", "recently_decided"]},
            ],
            "array_length": [
                {"path": "issues", "min": 1},
                {"path": "issues[].affected_segments", "min": 1},
                {"path": "issues[].sources", "min": 1},
            ],
            "equals": [
                {"left": "summary.issues_identified", "right": {"count": "issues"}},
            ],
        },
    },
    "harness": "claude_sdk",
    "model": "sonnet",
    "mode": "serve",
    "max_turns": 60,
    "cpu": "2048",
    "memory": "4096",
    "timeout_seconds": 3000,
    "required_params": ["state", "city", "l2DistrictType", "l2DistrictName"],
}
