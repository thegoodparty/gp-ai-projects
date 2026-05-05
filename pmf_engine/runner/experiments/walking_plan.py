from pathlib import Path

_instruction_path = Path(__file__).parent / "instructions" / "walking_plan.md"
_instruction = _instruction_path.read_text()

EXPERIMENT = {
    "instruction": _instruction,
    "contract": {
        "type": "json",
        "s3_key_template": "{experiment_id}/{run_id}/walking_plan.json",
        "schema": {
            "organization_slug": "string",
            "district": {
                "state": "string",
            },
            "generated_at": "string",
            "summary": {
                "total_areas": "number",
                "total_doors": "number",
                "estimated_total_hours": "number",
                "top_issues": ["string"],
            },
            "areas": [{
                "name": "string",
                "zip": "string",
                "city": "string",
                "priority_rank": "number",
                "door_count": "number",
                "estimated_minutes": "number",
                "maps_url": "string",
                "voters": [{
                    "order": "number",
                    "address": "string",
                    "voter_name": "string",
                    "party": "string",
                    "voter_status": "string",
                    "age": "number",
                    "talking_points": ["string"],
                }],
            }],
            "methodology": "string",
        },
    },
    "harness": "claude_sdk",
    "model": "sonnet",
    "mode": "win",
    "max_turns": 60,
    "cpu": "2048",
    "memory": "4096",
    "timeout_seconds": 900,
    "required_params": ["state", "city", "l2DistrictType", "l2DistrictName"],
}
