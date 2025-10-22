#!/usr/bin/env python3

import asyncio
import sys
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from serve.classify.theme_aggregator import ThemeAggregator
from serve.classify.models import (
    MessageData, EnrichedMessage, SmartCategorization,
    HierarchicalIssueWithContext, IssueStance,
    Sentiment, MessageQuality, ContentType
)


async def main():
    print("Loading classified messages...")
    df = pd.read_csv('serve/classify/output/cara_classified_messages.csv', nrows=200)

    print(f"Loaded {len(df)} classified messages")

    # Convert to EnrichedMessage objects
    messages = []
    for _, row in df.iterrows():
        if pd.isna(row['hierarchical_issues_with_stance']) or not row['hierarchical_issues_with_stance']:
            continue

        # Parse issues
        issues = []
        for issue_str in str(row['hierarchical_issues_with_stance']).split('|'):
            if ':' not in issue_str:
                continue

            category, stance_concern = issue_str.split(':', 1)
            if '/' not in category:
                continue

            primary, secondary = category.split('/', 1)
            stance_part = stance_concern.split('(')[0] if '(' in stance_concern else stance_concern
            concern = stance_concern.split('(')[1].rstrip(')') if '(' in stance_concern else ''

            stance_enum = {
                'negative': IssueStance.NEGATIVE,
                'positive': IssueStance.POSITIVE,
                'neutral': IssueStance.NEUTRAL,
                'requesting': IssueStance.REQUESTING
            }.get(stance_part.lower().strip(), IssueStance.NEUTRAL)

            issues.append(HierarchicalIssueWithContext(
                primary_category=primary,
                secondary_category=secondary,
                stance=stance_enum,
                specific_concern=concern,
                is_root_cause=False
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

    print(f"Converted {len(messages)} messages for theme aggregation\n")

    # Aggregate themes
    print("Aggregating themes...")
    aggregator = ThemeAggregator()
    all_themes = await aggregator.aggregate_themes(messages, min_cluster_size=3)

    print(f"\n{'='*60}")
    print(f"THEME AGGREGATION RESULTS")
    print(f"{'='*60}\n")

    for issue_themes in all_themes[:5]:  # Show top 5 issues
        report = aggregator.generate_theme_report(issue_themes)
        print(report)
        print()


if __name__ == "__main__":
    asyncio.run(main())
