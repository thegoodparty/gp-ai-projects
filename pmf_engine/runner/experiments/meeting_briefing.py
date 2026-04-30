from pathlib import Path

_instruction_path = Path(__file__).parent / "instructions" / "meeting_briefing.md"
_instruction = _instruction_path.read_text()

EXPERIMENT = {
    "instruction": _instruction,
    "contract": {
        "type": "json",
        "s3_key_template": "{experiment_id}/{run_id}/meeting_briefing.json",
        "schema": {
            "eo": {
                "name": "string",
                "city": "string",
                "state": "string",
                "office": "string",
            },
            "meeting": {
                "body": "string",
                "date": "string",
                "time": "string",
                "agenda_source": "string",
            },
            "agenda_items": [{
                "item_number": "string",
                "title": "string",
                "type": "string",
                "requires_vote": "boolean",
            }],
            "fiscal": {
                "tax_rate": "string",
                "budget_total": "string",
                "source": "string",
            },
            "data_quality": {
                "agenda": "string",
                "fiscal": "string",
                "platform": "string",
                "overall": "string",
            },
            "teaser_email": "string",
            "briefing_content": "string",
            "score": {
                "total": "number",
                "max": "number",
                "recommendation": "string",
                "dimensions": [{
                    "id": "string",
                    "name": "string",
                    "score": "number",
                    "justification": "string",
                }],
            },
            "sources": [{
                "id": "string",
                "type": "string",
                "title": "string",
                "url": "string",
                "accessed_at": "string",
            }],
            "generated_at": "string",
            "based_on_district_intel_run": "string",
        },
        "constraints": {
            "enums": [
                {"path": "score.recommendation", "values": ["send", "review", "hold"]},
                {"path": "data_quality.agenda", "values": ["high", "medium", "low", "not_applicable"]},
                {"path": "data_quality.fiscal", "values": ["high", "medium", "low", "not_applicable"]},
                {"path": "data_quality.platform", "values": ["high", "medium", "low", "not_applicable"]},
                {"path": "data_quality.overall", "values": ["high", "medium", "low", "not_applicable"]},
                {"path": "sources[].type", "values": [
                    "government_record", "news", "staff_report",
                    "campaign", "modeled", "web_search",
                ]},
                {"path": "agenda_items[].type", "values": [
                    "consent", "public_hearing", "ordinance", "resolution",
                    "discussion", "presentation", "business", "informational",
                ]},
            ],
            "ranges": [
                {"path": "score.dimensions[].score", "min": 0, "max": 10},
                {"path": "score.total", "min": 0, "max": 120},
                {"path": "score.max", "min": 120, "max": 120},
            ],
            "array_length": [
                {"path": "score.dimensions", "exact": 12},
                {"path": "agenda_items", "min": 1},
                {"path": "sources", "min": 1},
            ],
            "exact_ids": [
                {"path": "score.dimensions[].id", "values": [
                    "legislative_record",
                    "fiscal_depth",
                    "voter_constituent_intelligence",
                    "gap_analysis",
                    "political_intelligence",
                    "strategic_roadmap",
                    "procedural_guidance",
                    "personal_tailoring",
                    "news_narrative_context",
                    "state_policy_integration",
                    "source_transparency",
                    "accuracy_risk_management",
                ]},
            ],
            "equals": [
                {"left": "score.total", "right": {"sum": "score.dimensions[].score"}},
            ],
        },
    },
    "harness": "claude_sdk",
    "model": "sonnet",
    "mode": "serve",
    "max_turns": 100,
    "cpu": "2048",
    "memory": "4096",
    "timeout_seconds": 3000,
    "required_params": ["state", "city", "l2DistrictType", "l2DistrictName"],
}
