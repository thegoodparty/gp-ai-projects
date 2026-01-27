#!/usr/bin/env python3

import asyncio
from typing import List, Dict, Any, Optional
from collections import defaultdict
from dataclasses import dataclass
import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.metrics.pairwise import cosine_similarity

from shared.llm_gemini_3 import Gemini3Client, GeminiModelType, ThinkingLevel
from shared.llm_gemini import GeminiEmbeddingClient
from shared.logger import get_logger

try:
    from .models import EnrichedMessage, IssueStance
except ImportError:
    from models import EnrichedMessage, IssueStance

logger = get_logger(__name__)


@dataclass
class ThemeCluster:
    """A cluster of similar concerns within an issue category"""
    issue_category: str  # e.g., "public_safety/police"
    theme_summary: str  # AI-generated theme description
    unique_respondents: int
    total_mentions: int
    stance_distribution: Dict[str, int]
    representative_quotes: List[str]  # 3-5 best examples
    specific_concerns: List[str]  # All unique concern descriptions
    sample_messages: List[str]  # Full message texts for context


@dataclass
class IssueThemes:
    """All themes for a specific issue category"""
    issue_category: str
    primary_category: str
    secondary_category: str
    total_unique_respondents: int
    total_mentions: int
    theme_clusters: List[ThemeCluster]
    overall_stance_distribution: Dict[str, int]


class ThemeAggregator:
    """
    Aggregate classified messages into semantic theme clusters
    for digestible LLM consumption and action planning
    """

    def __init__(self, max_theme_size: int = 30):
        self.llm_client = Gemini3Client(
            default_model=GeminiModelType.FLASH_3,
            default_temperature=0.0,
            thinking_level=ThinkingLevel.MINIMAL
        )
        self.embedding_client = GeminiEmbeddingClient()
        self.max_theme_size = max_theme_size

    async def aggregate_themes(
        self,
        messages: List[EnrichedMessage],
        min_cluster_size: int = 3
    ) -> List[IssueThemes]:
        """
        Main method: Convert classified messages into theme clusters
        """
        logger.info(f"Aggregating themes from {len(messages)} messages")

        # Group messages by issue category
        issue_groups = self._group_by_issue_category(messages)

        logger.info(f"Found {len(issue_groups)} unique issue categories")

        # Process each issue category
        all_issue_themes = []
        for issue_category, issue_messages in issue_groups.items():
            if len(issue_messages) < min_cluster_size:
                logger.debug(f"Skipping {issue_category}: only {len(issue_messages)} messages")
                continue

            logger.info(f"Processing {issue_category}: {len(issue_messages)} messages")

            issue_themes = await self._process_issue_category(
                issue_category,
                issue_messages,
                min_cluster_size
            )

            if issue_themes:
                all_issue_themes.append(issue_themes)

        logger.info(f"Generated themes for {len(all_issue_themes)} issue categories")
        return all_issue_themes

    def _group_by_issue_category(
        self,
        messages: List[EnrichedMessage]
    ) -> Dict[str, List[tuple[EnrichedMessage, Any]]]:
        """Group messages by issue category (primary/secondary)"""
        issue_groups = defaultdict(list)

        for message in messages:
            if not message.smart_classification:
                continue

            for issue in message.smart_classification.issues:
                category_key = f"{issue.primary_category}/{issue.secondary_category}"
                issue_groups[category_key].append((message, issue))

        return issue_groups

    async def _process_issue_category(
        self,
        issue_category: str,
        issue_messages: List[tuple[EnrichedMessage, Any]],
        min_cluster_size: int
    ) -> Optional[IssueThemes]:
        """Process a single issue category into theme clusters"""

        # Extract concerns and generate embeddings
        concerns = []
        concern_to_message = []

        for message, issue in issue_messages:
            concern_text = issue.specific_concern or message.original_data.message_text[:100]
            concerns.append(concern_text)
            concern_to_message.append((message, issue))

        if len(concerns) < min_cluster_size:
            return None

        # Generate embeddings for semantic clustering
        logger.debug(f"Generating embeddings for {len(concerns)} concerns")
        embeddings = await self.embedding_client._create_embeddings_parallel(concerns)

        # Cluster by semantic similarity
        clusters = self._cluster_concerns(embeddings, min_cluster_size)

        logger.debug(f"Found {len(set(clusters))} clusters (excluding noise)")

        # Generate theme summaries for each cluster
        theme_clusters = []
        for cluster_id in set(clusters):
            if cluster_id == -1:  # Noise cluster
                continue

            cluster_indices = [i for i, c in enumerate(clusters) if c == cluster_id]
            cluster_messages = [concern_to_message[i] for i in cluster_indices]

            theme_cluster = await self._create_theme_cluster(
                issue_category,
                cluster_messages
            )

            if theme_cluster:
                theme_clusters.append(theme_cluster)

        if not theme_clusters:
            return None

        # Calculate overall stats
        unique_respondents = len(set(
            msg.original_data.contact_phone_number
            for msg, _ in issue_messages
        ))

        stance_distribution = defaultdict(int)
        for _, issue in issue_messages:
            stance_distribution[issue.stance.value] += 1

        primary, secondary = issue_category.split('/')

        return IssueThemes(
            issue_category=issue_category,
            primary_category=primary,
            secondary_category=secondary,
            total_unique_respondents=unique_respondents,
            total_mentions=len(issue_messages),
            theme_clusters=theme_clusters,
            overall_stance_distribution=dict(stance_distribution)
        )

    def _cluster_concerns(
        self,
        embeddings: List[List[float]],
        min_samples: int
    ) -> List[int]:
        """Cluster concerns by semantic similarity using DBSCAN"""
        if len(embeddings) < min_samples:
            return [-1] * len(embeddings)

        # Convert to numpy array
        X = np.array(embeddings)

        # DBSCAN clustering (density-based, doesn't require predefined k)
        # eps: maximum distance between samples in same cluster
        # min_samples: minimum cluster size
        clustering = DBSCAN(
            eps=0.3,  # Adjust based on embedding space
            min_samples=min_samples,
            metric='cosine'
        )

        labels = clustering.fit_predict(X)
        return labels.tolist()

    async def _create_theme_cluster(
        self,
        issue_category: str,
        cluster_messages: List[tuple[EnrichedMessage, Any]]
    ) -> Optional[ThemeCluster]:
        """Create a theme cluster with AI-generated summary"""

        if len(cluster_messages) == 0:
            return None

        # Extract data
        unique_phones = set()
        stance_distribution = defaultdict(int)
        specific_concerns = []
        sample_messages = []

        for message, issue in cluster_messages:
            unique_phones.add(message.original_data.contact_phone_number)
            stance_distribution[issue.stance.value] += 1

            if issue.specific_concern and issue.specific_concern not in specific_concerns:
                specific_concerns.append(issue.specific_concern)

            if len(sample_messages) < 5:
                sample_messages.append(message.original_data.message_text)

        # Generate theme summary from concerns
        theme_summary = await self._generate_theme_summary(
            issue_category,
            specific_concerns[:10],  # Limit for LLM context
            sample_messages[:3]
        )

        # Select representative quotes (diverse examples)
        representative_quotes = self._select_representative_quotes(
            cluster_messages,
            max_quotes=5
        )

        return ThemeCluster(
            issue_category=issue_category,
            theme_summary=theme_summary,
            unique_respondents=len(unique_phones),
            total_mentions=len(cluster_messages),
            stance_distribution=dict(stance_distribution),
            representative_quotes=representative_quotes,
            specific_concerns=specific_concerns,
            sample_messages=sample_messages[:self.max_theme_size]
        )

    async def _generate_theme_summary(
        self,
        issue_category: str,
        specific_concerns: List[str],
        sample_messages: List[str]
    ) -> str:
        """Generate AI theme summary from concerns"""

        prompt = f"""Analyze these constituent concerns about {issue_category.replace('_', ' ')} and create a concise theme summary (1-2 sentences).

Specific concerns mentioned:
{chr(10).join(f"- {concern}" for concern in specific_concerns[:10])}

Sample messages:
{chr(10).join(f'"{msg[:150]}"' for msg in sample_messages[:3])}

Generate a theme summary that captures the common thread across these concerns. Be specific and actionable."""

        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.llm_client.generate_content(
                    prompt=prompt,
                    max_tokens=100
                )
            )
            return response.strip()
        except Exception as e:
            logger.error(f"Failed to generate theme summary: {e}")
            # Fallback: use most common concern
            return specific_concerns[0] if specific_concerns else "General concerns"

    def _select_representative_quotes(
        self,
        cluster_messages: List[tuple[EnrichedMessage, Any]],
        max_quotes: int = 5
    ) -> List[str]:
        """Select diverse representative quotes"""

        # Prioritize: negative stance > requesting > longer messages
        scored_messages = []

        for message, issue in cluster_messages:
            text = message.original_data.message_text

            score = 0
            if issue.stance == IssueStance.NEGATIVE:
                score += 3
            elif issue.stance == IssueStance.REQUESTING:
                score += 2
            elif issue.stance == IssueStance.NEUTRAL:
                score += 1

            # Bonus for detailed messages
            if len(text) > 100:
                score += 1

            scored_messages.append((score, text))

        # Sort by score and take top quotes
        scored_messages.sort(key=lambda x: x[0], reverse=True)

        quotes = []
        for score, text in scored_messages[:max_quotes]:
            # Truncate long messages
            if len(text) > 150:
                text = text[:150] + "..."
            quotes.append(text)

        return quotes

    def generate_theme_report(self, issue_themes: IssueThemes) -> str:
        """Generate human-readable report for an issue's themes"""

        lines = [
            f"# Theme Analysis: {issue_themes.issue_category.replace('_', ' ').title()}",
            "",
            f"**Total Unique Respondents:** {issue_themes.total_unique_respondents}",
            f"**Total Mentions:** {issue_themes.total_mentions}",
            f"**Stance Distribution:** {issue_themes.overall_stance_distribution}",
            "",
            f"## Identified Themes ({len(issue_themes.theme_clusters)} clusters)",
            ""
        ]

        for i, theme in enumerate(issue_themes.theme_clusters, 1):
            lines.extend([
                f"### Theme {i}: {theme.theme_summary}",
                f"- **Respondents:** {theme.unique_respondents} ({theme.total_mentions} mentions)",
                f"- **Stance:** {theme.stance_distribution}",
                "",
                "**Representative Quotes:**"
            ])

            for quote in theme.representative_quotes:
                lines.append(f'> "{quote}"')

            lines.append("")

        return "\n".join(lines)


async def main():
    """Test the theme aggregator"""
    import pandas as pd
    from .models import MessageData, EnrichedMessage, SmartCategorization, HierarchicalIssueWithContext

    # Load classified messages
    df = pd.read_csv('serve/classify/output/cara_classified_messages.csv', nrows=200)

    print(f"Loaded {len(df)} classified messages")

    # Convert to EnrichedMessage objects (simplified for testing)
    messages = []
    for _, row in df.iterrows():
        if pd.isna(row['hierarchical_issues_with_stance']):
            continue

        # Parse issues (simplified)
        issues = []
        for issue_str in str(row['hierarchical_issues_with_stance']).split('|'):
            if ':' in issue_str:
                category, stance_concern = issue_str.split(':', 1)
                primary, secondary = category.split('/')
                stance = stance_concern.split('(')[0] if '(' in stance_concern else stance_concern
                concern = stance_concern.split('(')[1].rstrip(')') if '(' in stance_concern else ''

                from .models import IssueStance
                stance_enum = {
                    'negative': IssueStance.NEGATIVE,
                    'positive': IssueStance.POSITIVE,
                    'neutral': IssueStance.NEUTRAL,
                    'requesting': IssueStance.REQUESTING
                }.get(stance.lower(), IssueStance.NEUTRAL)

                issues.append(HierarchicalIssueWithContext(
                    primary_category=primary,
                    secondary_category=secondary,
                    stance=stance_enum,
                    specific_concern=concern
                ))

        if not issues:
            continue

        message_data = MessageData(
            campaign_id=str(row['campaign_id']),
            campaign_name=str(row['campaign_name']),
            contact_phone_number=str(row['contact_phone_number']),
            carrier=str(row.get('carrier', '')),
            campaign_number=str(row['campaign_number']),
            is_automatic_reply=False,
            send_direction='INBOUND',
            send_status='',
            error_code='',
            sent_at=str(row['sent_at']),
            message_text=str(row['message_text']),
            texter_name='',
            message_type='SMS',
            mms_attachments=''
        )

        from .models import Sentiment, MessageQuality, ContentType
        classification = SmartCategorization(
            issues=issues,
            should_be_uncategorized=False,
            overall_sentiment=Sentiment.FRUSTRATED_URGENT,
            message_quality=MessageQuality.SUBSTANTIVE,
            content_type=ContentType.POLICY_FEEDBACK
        )

        enriched = EnrichedMessage(
            original_data=message_data,
            smart_classification=classification,
            is_substantive=True
        )

        messages.append(enriched)

    print(f"Converted {len(messages)} messages for theme aggregation")

    # Aggregate themes
    aggregator = ThemeAggregator()
    all_themes = await aggregator.aggregate_themes(messages, min_cluster_size=3)

    print(f"\n{'='*60}")
    print(f"THEME AGGREGATION RESULTS")
    print(f"{'='*60}\n")

    for issue_themes in all_themes[:3]:  # Show top 3 issues
        report = aggregator.generate_theme_report(issue_themes)
        print(report)
        print()


if __name__ == "__main__":
    asyncio.run(main())
