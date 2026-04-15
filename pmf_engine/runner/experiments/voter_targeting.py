from pathlib import Path

_instruction_path = Path(__file__).parent / "instructions" / "voter_targeting.md"
_instruction = _instruction_path.read_text()

EXPERIMENT = {
    "instruction": _instruction,
    "contract": {
        "type": "json",
        "s3_key_template": "{experiment_id}/{run_id}/voter_targeting.json",
        "schema": {
            "candidate_id": "string",
            "district": {
                "state": "string",
                "type": "string",
                "name": "string",
            },
            "generated_at": "string",
            "summary": {
                "total_voters_in_district": "number",
                "win_number": "number",
                "projected_turnout": "number",
            },
            "segments": [{
                "tier": "number",
                "name": "string",
                "description": "string",
                "count": "number",
                "demographics": {
                    "party_breakdown": {},
                    "age_distribution": {},
                    "gender_split": {},
                },
                "outreach_priority": "string",
                "recommended_channels": ["string"],
                "voters": [{
                    "voter_id": "string",
                    "first_name": "string",
                    "last_name": "string",
                    "address": "string",
                    "city": "string",
                    "zip": "string",
                    "age": "number",
                    "gender": "string",
                    "party": "string",
                    "voter_status": "string",
                }],
            }],
            "geographic_clusters": [{
                "area": "string",
                "voter_count": "number",
                "density_rank": "number",
            }],
            "methodology": "string",
        },
    },
    "harness": "claude_sdk",
    "model": "sonnet",
    "mode": "win",
    "max_turns": 50,
    "cpu": "2048",
    "memory": "4096",
    "timeout_seconds": 900,
}
