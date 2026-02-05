#!/usr/bin/env python3

from shared.logger import get_logger
from shared.llm_gemini_3 import Gemini3Client, GeminiModelType, ThinkingLevel
from ..models import ClusterTheme, ClusterAssignment

logger = get_logger(__name__)

async def analyze_single_message(message, pipeline_state):
    logger.info("Analyzing single message with LLM...")

    llm_client = Gemini3Client(
        default_model=GeminiModelType.FLASH_3,
        default_temperature=0.0,
        thinking_level=ThinkingLevel.MINIMAL
    )

    # Handle both AtomicMessage and EmbeddedMessage
    message_text = getattr(message, 'atomic_text', None) or getattr(message, 'text', '')

    prompt = f"""Analyze this single civic engagement message from a political campaign.

MESSAGE:
{message_text}

Please provide a comprehensive analysis in this EXACT format:

CATEGORY: [High-level civic category: Infrastructure/Public Safety/Education/Healthcare/Housing/Transportation/Environment/Governance/Economic Development/Community Services/Other]
THEME: [2-4 word concise theme/label]
ISSUES_SUMMARY: [1 sentence describing the core issue or concern expressed]
DETAILED_ANALYSIS: [2-3 sentences analyzing the concern, pattern, underlying issue, and what the citizen is experiencing]
VERBATIM_QUOTES: [The exact message text]
KEY_TOPICS: [topic1, topic2, topic3, topic4, topic5]
SENTIMENT: [positive/negative/neutral/mixed/concerned]
ACTION_ITEMS: [Specific actions, changes, or solutions requested or that would address this concern]
CIVIC_RELEVANCE: [How this relates to local governance and community needs]
CONFIDENCE: [High/Medium/Low - based on message clarity]

Focus on understanding WHY the citizen contacted the campaign and what specific issue they are experiencing."""

    try:
        response = llm_client.generate_content(prompt)

        theme = parse_single_message_response(response)

        usage_stats = llm_client.get_usage_stats() if hasattr(llm_client, 'get_usage_stats') else {}
        total_cost = usage_stats.get('total_cost', 0)

        pipeline_state.total_cost += total_cost
        pipeline_state.stage_costs["single_message_analysis"] = total_cost
        pipeline_state.gemini_usage["single_message_analysis"] = usage_stats

        logger.info(f"Single message analysis complete - Theme: {theme.theme}")
        logger.info(f"Single Message Analysis Cost: ${total_cost:.4f}")

        message.cluster_assignment = ClusterAssignment(
            cluster_id=0,
            cluster_confidence=theme.confidence_score,
            is_noise=False
        )

        message.single_message_theme = theme

        return message

    except Exception as e:
        logger.error(f"Failed to analyze single message: {e}")
        message.cluster_assignment = ClusterAssignment(
            cluster_id=0,
            cluster_confidence=0.5,
            is_noise=False
        )
        message.single_message_theme = ClusterTheme(
            category="Other",
            theme="Single Message",
            summary="Analysis unavailable",
            issues_summary="Analysis unavailable",
            detailed_analysis="",
            verbatim_quotes=[message_text],
            key_topics=[],
            sentiment="neutral",
            action_items=[],
            civic_relevance="General civic engagement",
            confidence_score=0.5
        )
        return message

def parse_single_message_response(response: str):
    lines = response.strip().split('\n')

    category = "Other"
    theme = "Single Message"
    issues_summary = "Message analyzed"
    summary = "Message analyzed"
    detailed_analysis = ""
    verbatim_quotes = []
    key_topics = []
    sentiment = "neutral"
    action_items = []
    civic_relevance = "General civic engagement"
    confidence_score = 0.5

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('CATEGORY:'):
            category = line.replace('CATEGORY:', '').strip()
        elif line.startswith('THEME:'):
            theme = line.replace('THEME:', '').strip()
        elif line.startswith('ISSUES_SUMMARY:'):
            issues_summary = line.replace('ISSUES_SUMMARY:', '').strip()
            summary = issues_summary
        elif line.startswith('DETAILED_ANALYSIS:'):
            analysis_parts = [line.replace('DETAILED_ANALYSIS:', '').strip()]
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                if not next_line or any(next_line.startswith(prefix) for prefix in
                    ['VERBATIM_QUOTES:', 'KEY_TOPICS:', 'SENTIMENT:', 'ACTION_ITEMS:', 'CIVIC_RELEVANCE:', 'CONFIDENCE:']):
                    break
                analysis_parts.append(next_line)
                j += 1
            detailed_analysis = ' '.join([p for p in analysis_parts if p]).strip()
        elif line.startswith('VERBATIM_QUOTES:'):
            quote_text = line.replace('VERBATIM_QUOTES:', '').strip()
            if quote_text:
                verbatim_quotes = [quote_text]
        elif line.startswith('KEY_TOPICS:'):
            topics_str = line.replace('KEY_TOPICS:', '').strip()
            key_topics = [topic.strip() for topic in topics_str.split(',') if topic.strip()]
        elif line.startswith('SENTIMENT:'):
            sentiment = line.replace('SENTIMENT:', '').strip().lower()
        elif line.startswith('ACTION_ITEMS:'):
            actions_str = line.replace('ACTION_ITEMS:', '').strip()
            if ',' in actions_str:
                action_items = [action.strip() for action in actions_str.split(',') if action.strip()]
            else:
                action_items = [actions_str] if actions_str else []
        elif line.startswith('CIVIC_RELEVANCE:'):
            civic_relevance = line.replace('CIVIC_RELEVANCE:', '').strip()
        elif line.startswith('CONFIDENCE:'):
            confidence_text = line.replace('CONFIDENCE:', '').strip().lower()
            if confidence_text == 'high':
                confidence_score = 0.9
            elif confidence_text == 'medium':
                confidence_score = 0.7
            elif confidence_text == 'low':
                confidence_score = 0.3

        i += 1

    return ClusterTheme(
        category=category,
        theme=theme,
        summary=summary,
        issues_summary=issues_summary,
        detailed_analysis=detailed_analysis,
        verbatim_quotes=verbatim_quotes,
        key_topics=key_topics,
        sentiment=sentiment,
        action_items=action_items,
        civic_relevance=civic_relevance,
        confidence_score=confidence_score
    )
