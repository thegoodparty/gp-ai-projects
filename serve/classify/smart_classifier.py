#!/usr/bin/env python3

import asyncio
from typing import List, Optional, Dict, Any
import re
from concurrent.futures import ThreadPoolExecutor

from shared.llm_gemini import GeminiClient, GeminiModelType
from shared.logger import get_logger

from .models import (
    MessageData, SmartCategorization, HierarchicalIssueWithContext,
    IssueStance, Sentiment, MessageQuality, ContentType, HIERARCHICAL_TAXONOMY,
    IssueListResponse
)
from .classification_rules import ClassificationRules

logger = get_logger(__name__)

class WorldClassClassifier:
    """
    World-class multi-pass classification system based on manual review insights
    """

    def __init__(self, model_type: GeminiModelType = GeminiModelType.FLASH, temperature: float = 0.0, target_concurrency: int = 1200):
        self.logger = get_logger(__name__)

        # PRODUCTION PATTERN: ThreadPoolExecutor for maximum LLM concurrency
        self.max_workers = target_concurrency
        self.thread_pool = ThreadPoolExecutor(max_workers=self.max_workers)

        # PRODUCTION-LEVEL LLM client configuration (1200 concurrent connections)
        self.llm_client = GeminiClient(
            default_model=model_type,
            default_temperature=temperature,
            thinking_budget=0,  # Disable thinking for cost efficiency (~$0.075/1M tokens)
            max_connections=target_concurrency,
            max_keepalive_connections=target_concurrency // 4  # 300 keepalive
        )

        self.logger.info(f"🚀 WorldClassClassifier initialized with {target_concurrency} max connections + ThreadPoolExecutor ({self.max_workers} workers)")

    async def classify_message(self, message: MessageData) -> SmartCategorization:
        """
        Main classification method with multi-pass approach
        """
        logger.debug(f"Classifying message: {message.message_text[:50]}...")

        # Pass 1: Check if should be uncategorized
        should_uncategorize, uncategorize_reason = ClassificationRules.should_be_uncategorized(
            message.message_text
        )

        if should_uncategorize:
            logger.debug(f"Message uncategorized: {uncategorize_reason}")
            return SmartCategorization(
                issues=[],
                should_be_uncategorized=True,
                uncategorized_reason=uncategorize_reason,
                overall_sentiment=Sentiment.OTHER,
                message_quality=MessageQuality.MINIMAL_RESPONSE,
                content_type=ContentType.INAPPROPRIATE if "personal attack" in uncategorize_reason else ContentType.GENERAL_COMPLAINT,
                confidence_score=0.9
            )

        # Pass 2: LLM-based issue identification
        issues = await self._identify_issues_with_llm(message)

        # Pass 3: Apply learned classification rules
        issues = self._apply_classification_rules(issues, message.message_text)

        # Pass 4: Refinement based on context
        issues = ClassificationRules.apply_refinement_rules(issues, message.message_text)

        # Pass 5: Overall sentiment and quality assessment
        overall_sentiment, message_quality, content_type, confidence = await self._assess_overall_attributes(
            message, issues
        )

        return SmartCategorization(
            issues=issues,
            should_be_uncategorized=False,
            overall_sentiment=overall_sentiment,
            message_quality=message_quality,
            content_type=content_type,
            confidence_score=confidence
        )

    async def _identify_issues_with_llm(self, message: MessageData) -> List[HierarchicalIssueWithContext]:
        """
        Use LLM to identify issues with context and stance
        """
        taxonomy_str = self._format_taxonomy_for_prompt()

        system_prompt = f"""You are an expert civic message classifier. Analyze the message and identify ALL civic issues mentioned, with your stance toward each specific issue.

HIERARCHICAL TAXONOMY:
{taxonomy_str}

For each issue identified, determine:
1. Primary category and secondary category from the taxonomy
2. Your specific stance toward that issue (positive, negative, neutral, requesting)
3. Brief description of the specific concern
4. Whether this is a root cause of other issues mentioned

STANCE DEFINITIONS:
- positive: Supporting, praising, appreciative of this aspect
- negative: Complaining, frustrated, angry about this aspect
- neutral: Neutral observation or factual question about this aspect
- requesting: Asking for specific action/improvement on this aspect

EXAMPLES:
"High property taxes but great parks" →
- housing_and_development/taxes_and_assessments, negative, "property taxes too high"
- quality_of_life/recreation_and_libraries, positive, "parks are great"

"Need truck route enforcement and better roads" →
- infrastructure_and_transportation/roads_and_bridges, requesting, "road improvements needed"
- public_safety/police, requesting, "truck route enforcement needed"

Return a JSON array of issues. If no civic issues are found, return empty array."""

        user_prompt = f"""Message: "{message.message_text}"

Analyze this civic message and identify all issues with their stances."""

        try:
            # PRODUCTION PATTERN: Non-blocking LLM call via ThreadPoolExecutor with structured output
            response = await asyncio.get_event_loop().run_in_executor(
                self.thread_pool,
                lambda: self.llm_client.generate_structured_content(
                    prompt=user_prompt,
                    response_schema=IssueListResponse,
                    system_instruction=system_prompt
                )
            )

            # Response is already validated Pydantic model with normalized case!
            if isinstance(response, dict):
                response = IssueListResponse(**response)

            logger.debug(f"LLM identified {len(response.issues)} issues")
            return response.issues

        except Exception as e:
            logger.error(f"Error in LLM issue identification: {e}")
            return []

    def _apply_classification_rules(self, issues: List[HierarchicalIssueWithContext], message_text: str) -> List[HierarchicalIssueWithContext]:
        """
        Apply learned classification rules to ensure required categories are included
        """
        # Get required categories based on patterns
        required_categories = ClassificationRules.get_required_categories(message_text)
        categories_to_avoid = ClassificationRules.get_categories_to_avoid(message_text)

        # Check if required categories are missing (case-insensitive)
        existing_categories = set(
            f"{issue.primary_category.lower()}/{issue.secondary_category.lower()}"
            for issue in issues
        )

        for req_cat in required_categories:
            cat_key = f"{req_cat['primary'].lower()}/{req_cat['secondary'].lower()}"
            if cat_key not in existing_categories:
                logger.debug(f"Adding missing required category: {cat_key}")

                issue = HierarchicalIssueWithContext(
                    primary_category=req_cat["primary"],
                    secondary_category=req_cat["secondary"],
                    stance=req_cat.get("stance", IssueStance.NEGATIVE),
                    specific_concern=req_cat.get("concern_template", "Issue identified by classification rules"),
                    is_root_cause=req_cat.get("is_root_cause", False)
                )
                issues.append(issue)

        # Remove categories that should be avoided (case-insensitive)
        filtered_issues = []
        categories_to_avoid_lower = {cat.lower() for cat in categories_to_avoid}
        for issue in issues:
            cat_key = f"{issue.primary_category.lower()}/{issue.secondary_category.lower()}"
            if cat_key not in categories_to_avoid_lower:
                filtered_issues.append(issue)
            else:
                logger.debug(f"Removing avoided category: {cat_key}")

        # Apply special rules (e.g., e-bike police check)
        final_issues = []
        for issue in filtered_issues:
            if "police" in issue.secondary_category:
                if ClassificationRules.should_add_police_category(
                    message_text,
                    f"{issue.primary_category}/{issue.secondary_category}"
                ):
                    final_issues.append(issue)
                else:
                    logger.debug(f"Removing police category based on e-bike rule: {issue.secondary_category}")
            else:
                final_issues.append(issue)

        return final_issues

    async def _assess_overall_attributes(self, message: MessageData, issues: List[HierarchicalIssueWithContext]) -> tuple[Sentiment, MessageQuality, ContentType, float]:
        """
        Assess overall message sentiment, quality, and content type
        """
        # Quick heuristic-based assessment for efficiency
        text = message.message_text.lower().strip()

        # Sentiment assessment
        if not issues:
            sentiment = Sentiment.OTHER
        elif any(issue.stance == IssueStance.POSITIVE for issue in issues):
            if any(issue.stance == IssueStance.NEGATIVE for issue in issues):
                sentiment = Sentiment.CONSTRUCTIVE_DETAILED
            else:
                sentiment = Sentiment.APPRECIATIVE_POSITIVE
        elif any("angry" in issue.specific_concern.lower() for issue in issues if issue.specific_concern):
            sentiment = Sentiment.ANGRY_CONFRONTATIONAL
        elif len(issues) > 1 or len(text) > 100:
            sentiment = Sentiment.FRUSTRATED_URGENT
        else:
            sentiment = Sentiment.FRUSTRATED_URGENT

        # Message quality assessment
        if len(issues) > 0 and len(text) > 50:
            quality = MessageQuality.SUBSTANTIVE
        elif len(text) < 20 or not issues:
            quality = MessageQuality.MINIMAL_RESPONSE
        else:
            quality = MessageQuality.SUBSTANTIVE

        # Content type assessment
        if any(issue.stance == IssueStance.POSITIVE for issue in issues):
            content_type = ContentType.SUPPORT_APPRECIATION
        elif any(issue.stance == IssueStance.REQUESTING for issue in issues):
            if "?" in text:
                content_type = ContentType.QUESTION_REQUEST
            else:
                content_type = ContentType.POLICY_FEEDBACK
        elif issues:
            content_type = ContentType.POLICY_FEEDBACK if quality == MessageQuality.SUBSTANTIVE else ContentType.GENERAL_COMPLAINT
        else:
            content_type = ContentType.GENERAL_COMPLAINT

        # Confidence based on number of issues and text length
        confidence = min(0.95, 0.7 + (len(issues) * 0.1) + (min(len(text), 200) / 1000))

        return sentiment, quality, content_type, confidence

    def _format_taxonomy_for_prompt(self) -> str:
        """Format the hierarchical taxonomy for the LLM prompt"""
        formatted = []
        for primary, secondaries in HIERARCHICAL_TAXONOMY.items():
            formatted.append(f"{primary.upper()}:")
            for secondary, description in secondaries.items():
                formatted.append(f"  {secondary}: {description}")
        return "\n".join(formatted)

    async def classify_batch(self, messages: List[MessageData]) -> List[SmartCategorization]:
        """
        Classify a batch of messages with high-throughput parallel processing (CLAUDE.md pattern)
        """
        logger.info(f"Classifying batch of {len(messages)} messages in parallel")

        # Parallel execution pattern with asyncio.gather() (from CLAUDE.md)
        tasks = [self.classify_message(message) for message in messages]

        # Execute all classifications in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Handle exceptions and build final classifications
        classifications = []
        error_count = 0

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Error classifying message {i}: {result}")
                error_count += 1
                # Create fallback classification
                classifications.append(SmartCategorization(
                    issues=[],
                    should_be_uncategorized=True,
                    uncategorized_reason="classification error",
                    overall_sentiment=Sentiment.OTHER,
                    message_quality=MessageQuality.MINIMAL_RESPONSE,
                    content_type=ContentType.GENERAL_COMPLAINT,
                    confidence_score=0.1
                ))
            else:
                classifications.append(result)

        if error_count > 0:
            logger.warning(f"Classification completed with {error_count} errors out of {len(messages)} messages")
        else:
            logger.info(f"Successfully classified all {len(messages)} messages in parallel")

        return classifications

    def cleanup(self):
        """Cleanup ThreadPoolExecutor resources"""
        if hasattr(self, 'thread_pool'):
            self.thread_pool.shutdown(wait=True)
            self.logger.info("🧹 ThreadPoolExecutor cleaned up")


async def main():
    """Test the classifier"""
    from .models import MessageData

    classifier = WorldClassClassifier()

    # Test messages
    test_messages = [
        MessageData(
            campaign_id="test",
            campaign_name="Test Campaign",
            contact_phone_number="1234567890",
            carrier="TEST",
            campaign_number="123",
            is_automatic_reply=False,
            send_direction="INBOUND",
            send_status="",
            error_code="",
            sent_at="2025-01-01T00:00:00.000Z",
            message_text="Property taxes are too high and truck traffic is destroying our roads!",
            texter_name="",
            message_type="SMS",
            mms_attachments=""
        ),
        MessageData(
            campaign_id="test2",
            campaign_name="Test Campaign",
            contact_phone_number="1234567891",
            carrier="TEST",
            campaign_number="123",
            is_automatic_reply=False,
            send_direction="INBOUND",
            send_status="",
            error_code="",
            sent_at="2025-01-01T00:00:00.000Z",
            message_text="Thanks for reaching out!",
            texter_name="",
            message_type="SMS",
            mms_attachments=""
        )
    ]

    classifications = await classifier.classify_batch(test_messages)

    for i, classification in enumerate(classifications):
        print(f"\nMessage {i+1}: {test_messages[i].message_text}")
        print(f"Uncategorized: {classification.should_be_uncategorized}")
        if classification.should_be_uncategorized:
            print(f"Reason: {classification.uncategorized_reason}")
        else:
            print(f"Issues ({len(classification.issues)}):")
            for issue in classification.issues:
                print(f"  - {issue.primary_category}/{issue.secondary_category}: {issue.stance.value}")
                print(f"    Concern: {issue.specific_concern}")
                print(f"    Root cause: {issue.is_root_cause}")


if __name__ == "__main__":
    asyncio.run(main())