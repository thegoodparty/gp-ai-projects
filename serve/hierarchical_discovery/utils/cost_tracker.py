#!/usr/bin/env python3

from typing import Dict, Any

def generate_cost_summary(pipeline_state) -> Dict[str, Any]:
    total_tokens = 0
    total_api_calls = 0

    for stage, usage_stats in pipeline_state.gemini_usage.items():
        if usage_stats:
            total_tokens += usage_stats.get('total_tokens', usage_stats.get('total_input_tokens', 0))

    cost_summary = {
        "total_cost": pipeline_state.total_cost,
        "total_api_calls": pipeline_state.api_calls,
        "total_tokens": total_tokens,
        "stage_breakdown": dict(pipeline_state.stage_costs),
        "stage_usage": dict(pipeline_state.gemini_usage),
        "cost_per_message": 0,
        "cost_per_token": 0
    }

    if pipeline_state.clustered_messages_count > 0:
        cost_summary["cost_per_message"] = pipeline_state.total_cost / pipeline_state.clustered_messages_count

    if total_tokens > 0:
        cost_summary["cost_per_token"] = pipeline_state.total_cost / total_tokens

    return cost_summary
