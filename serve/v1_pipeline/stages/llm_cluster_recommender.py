#!/usr/bin/env python3

import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from shared.llm_gemini_3 import Gemini3Client, GeminiModelType, ThinkingLevel
from shared.logger import get_logger

logger = get_logger(__name__)


class ClusterRecommendation(BaseModel):
    rank: int = Field(..., description="Rank 1-3")
    theme: str = Field(..., description="Exact theme name from the cluster")
    category: str = Field(..., description="Category from the cluster")
    reasoning: str = Field(..., description="2-3 sentences explaining why this cluster is important")


class Top3ClustersResponse(BaseModel):
    top_clusters: list[ClusterRecommendation] = Field(
        description="Top 3 most substantive, actionable clusters"
    )
    overall_assessment: str = Field(
        description="1-2 sentences on the overall themes across all responses"
    )


def _aggregate_by_theme(unified_records) -> dict[str, dict[str, Any]]:
    """Group records by theme and calculate metrics"""
    theme_data = defaultdict(lambda: {
        'messages': [],
        'unique_phones': set(),
        'sentiments': [],
        'categories': set(),
        'confidence_scores': [],
        'action_items': [],
        'key_topics': []
    })

    for record in unified_records:
        if record.multi_cluster_data:
            cluster_info = next(iter(record.multi_cluster_data.values()))
            theme = cluster_info.get('cluster_theme', 'Uncategorized')

            theme_data[theme]['messages'].append(record.message_text)
            theme_data[theme]['unique_phones'].add(record.phone_number)
            theme_data[theme]['sentiments'].append(cluster_info.get('cluster_sentiment', 'neutral'))
            theme_data[theme]['categories'].add(cluster_info.get('cluster_category', 'Other'))
            theme_data[theme]['confidence_scores'].append(cluster_info.get('theme_confidence', 0.0))

            action_items = cluster_info.get('action_items', [])
            if action_items:
                theme_data[theme]['action_items'].extend(action_items)

            key_topics = cluster_info.get('key_topics', [])
            if key_topics:
                theme_data[theme]['key_topics'].extend(key_topics)

    cluster_summary = {}
    total_respondents = len(set(r.phone_number for r in unified_records))

    for theme, data in theme_data.items():
        unique_respondents = len(data['unique_phones'])
        total_messages = len(data['messages'])

        sentiment_counts = {}
        for s in data['sentiments']:
            sentiment_counts[s] = sentiment_counts.get(s, 0) + 1
        most_common_sentiment = max(sentiment_counts.items(), key=lambda x: x[1])[0] if sentiment_counts else 'neutral'

        cluster_summary[theme] = {
            'category': list(data['categories'])[0] if data['categories'] else 'Other',
            'unique_respondents': unique_respondents,
            'total_messages': total_messages,
            'coverage_pct': round((unique_respondents / total_respondents * 100), 2) if total_respondents > 0 else 0,
            'sentiment': most_common_sentiment,
            'avg_confidence': round(sum(data['confidence_scores']) / len(data['confidence_scores']), 3) if data['confidence_scores'] else 0,
            'action_items_count': len(data['action_items']),
            'key_topics': list(set(data['key_topics']))[:5],
            'sample_messages': data['messages'][:3]
        }

    return cluster_summary


def _format_clusters_for_llm(cluster_summary: dict[str, dict[str, Any]]) -> str:
    """Format cluster data for LLM prompt"""
    lines = []

    for idx, (theme, data) in enumerate(sorted(
        cluster_summary.items(),
        key=lambda x: x[1]['unique_respondents'],
        reverse=True
    ), 1):
        lines.append(f"{idx}. {theme}")
        lines.append(f"   Category: {data['category']}")
        lines.append(f"   Unique Respondents: {data['unique_respondents']} ({data['coverage_pct']:.1f}%)")
        lines.append(f"   Total Messages: {data['total_messages']}")
        lines.append(f"   Sentiment: {data['sentiment']}")
        lines.append(f"   Confidence: {data['avg_confidence']:.2f}")
        lines.append(f"   Action Items: {data['action_items_count']}")

        if data['key_topics']:
            topics_str = ', '.join(data['key_topics'])
            lines.append(f"   Key Topics: {topics_str}")

        if data['sample_messages']:
            lines.append("   Sample Messages:")
            for msg in data['sample_messages'][:2]:
                msg_short = msg[:100] + '...' if len(msg) > 100 else msg
                lines.append(f"     - \"{msg_short}\"")

        lines.append("")

    return "\n".join(lines)


def _create_evaluation_prompt(cluster_summary: dict[str, dict[str, Any]]) -> str:
    """Create LLM prompt for cluster evaluation"""

    formatted_clusters = _format_clusters_for_llm(cluster_summary)

    prompt = f"""Analyze these {len(cluster_summary)} civic message clusters and identify the TOP 3 most important ones that a city council or campaign should prioritize.

CLUSTERS SUMMARY:
{formatted_clusters}

SELECTION CRITERIA:
1. **Substantiveness**: Avoid superficial themes like "general feedback" or "positive comments"
2. **Actionability**: Prioritize clusters with specific, concrete concerns that can be addressed
3. **Respondent Engagement**: Consider unique respondent count (not just total messages)
4. **Civic Importance**: Focus on issues that impact local governance, public safety, infrastructure, or community wellbeing
5. **Specificity**: Prefer specific issues over vague/generic themes

THEMES TO AVOID:
- Generic themes like "Other", "General Feedback", "Miscellaneous", "Uncategorized"
- Purely positive/appreciation clusters unless they contain substantive policy suggestions
- Clusters with very low respondent counts (<10 unique people)
- Vague sentiment without specific issues or concerns
- Clusters that are just "thank you" messages or appreciation

SELECT THE TOP 3 CLUSTERS:
For each cluster, provide:
1. The exact theme name (copy it exactly as shown above)
2. The category
3. A 2-3 sentence explanation of WHY this cluster is significant and should be prioritized

Also provide a brief 1-2 sentence overall assessment of what the key themes are across all citizen responses."""

    return prompt


async def recommend_top_clusters_via_llm(unified_records, config: dict[str, Any]) -> tuple:
    """
    Use LLM to select the top 3 most significant clusters

    Args:
        unified_records: List of UnifiedCampaignRecord objects
        config: Pipeline configuration

    Returns:
        Tuple of (top_clusters list, overall_assessment string)
    """
    logger.info(f"Starting LLM-based cluster recommendation for {len(unified_records)} records")

    top_clusters_config = config.get('top_clusters', {})
    min_respondents = top_clusters_config.get('min_respondents', 10)
    llm_model = top_clusters_config.get('llm_model', 'flash')
    temperature = top_clusters_config.get('temperature', 0.0)

    cluster_summary = _aggregate_by_theme(unified_records)

    filtered_summary = {
        theme: data for theme, data in cluster_summary.items()
        if data['unique_respondents'] >= min_respondents
    }

    logger.info(f"Aggregated {len(cluster_summary)} themes, {len(filtered_summary)} meet min_respondents threshold ({min_respondents})")

    if len(filtered_summary) < 3:
        logger.warning(f"Only {len(filtered_summary)} clusters meet threshold. Using all available clusters.")
        filtered_summary = cluster_summary

    if not filtered_summary:
        logger.warning("No clusters found for recommendation")
        return [], "No substantive clusters found in the dataset"

    prompt = _create_evaluation_prompt(filtered_summary)

    llm_client = Gemini3Client(
        default_model=GeminiModelType.FLASH_3 if llm_model.lower() == 'flash' else GeminiModelType.PRO_3,
        default_temperature=temperature,
        thinking_level=ThinkingLevel.MINIMAL
    )

    try:
        response = llm_client.generate_structured_content(
            prompt=prompt,
            response_schema=Top3ClustersResponse,
            system_instruction="You are an expert civic analyst evaluating citizen feedback clusters. Select the most substantive, actionable clusters that local government should prioritize."
        )

        if not response or not response.top_clusters:
            logger.error("LLM returned empty response")
            return [], "Failed to generate recommendations"

        logger.info(f"LLM selected {len(response.top_clusters)} top clusters")

        for cluster in response.top_clusters:
            logger.debug(f"Rank {cluster.rank}: {cluster.theme} ({cluster.category})")

        return response.top_clusters, response.overall_assessment

    except Exception as e:
        logger.error(f"Failed to generate LLM recommendations: {e}", exc_info=True)
        return [], f"Error generating recommendations: {str(e)}"


def format_recommendations_for_logging(top_clusters: list[ClusterRecommendation], overall_assessment: str) -> str:
    """Format recommendations for pretty logging output"""
    lines = []
    lines.append("=" * 70)
    lines.append("🏆 TOP 3 RECOMMENDED CLUSTERS (LLM-SELECTED)")
    lines.append("=" * 70)
    lines.append(f"Overall Assessment: {overall_assessment}")
    lines.append("")

    for cluster in top_clusters:
        lines.append(f"#{cluster.rank} - {cluster.theme}")
        lines.append(f"     Category: {cluster.category}")
        lines.append(f"     Why: {cluster.reasoning}")
        lines.append("")

    lines.append("=" * 70)

    return "\n".join(lines)
