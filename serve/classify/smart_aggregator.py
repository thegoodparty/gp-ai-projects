#!/usr/bin/env python3

from typing import List, Dict, Any, Tuple, Optional
from collections import defaultdict, Counter
from dataclasses import dataclass
import math
import re

from .models import EnrichedMessage, IssueStance, Sentiment, MessageQuality, ContentType, HIERARCHICAL_TAXONOMY, HierarchicalCategoryCounts
from shared.logger import get_logger

logger = get_logger(__name__)


@dataclass
class IssueInsight:
    """Detailed insight for a specific issue category"""
    primary_category: str
    secondary_category: str
    unique_respondents: int  # Count of unique people who mentioned this issue
    total_mentions: int
    mentions_per_respondent: float  # Average intensity per person
    respondent_stances: Dict[str, int]  # Stance counts by unique respondent
    stance_distribution: Dict[str, int]  # All mentions stance distribution (legacy)
    sentiment_breakdown: Dict[str, int]
    sample_messages: List[str]
    urgency_score: float  # Based on unique respondents and negative sentiment
    key_concerns: List[str]


@dataclass
class CompoundIssuePattern:
    """Pattern of issues that commonly appear together"""
    issue_combination: List[str]
    frequency: int
    common_stances: Dict[str, str]  # issue -> most common stance
    sample_messages: List[str]


@dataclass
class CampaignInsights:
    """Comprehensive insights for a campaign's messages"""
    total_messages: int
    substantive_messages: int
    uncategorized_messages: int

    # Top-level summaries
    top_issues: List[IssueInsight]
    most_criticized_issues: List[IssueInsight]
    most_praised_issues: List[IssueInsight]
    most_requested_improvements: List[IssueInsight]

    # Pattern analysis
    compound_issue_patterns: List[CompoundIssuePattern]
    root_cause_analysis: Dict[str, List[str]]  # root_cause -> issues_it_causes

    # Sentiment analysis
    overall_sentiment_distribution: Dict[str, int]
    issue_specific_sentiment: Dict[str, Dict[str, int]]

    # Quality metrics
    message_quality_distribution: Dict[str, int]
    content_type_distribution: Dict[str, int]

    # Actionability insights
    priority_issues: List[str]  # Issues to focus on first
    success_stories: List[str]  # What's working well
    immediate_actions: List[str]  # Quick wins

    # Hierarchical category distribution
    hierarchical_category_counts: HierarchicalCategoryCounts


class SmartAggregator:
    """
    Advanced insights generator for classified civic messages
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.category_descriptions = self._flatten_taxonomy()
        self.config = config or {}

        # Get aggregation settings from config
        insights_config = self.config.get("insights", {})
        self.aggregation_mode = insights_config.get("aggregation_mode", "respondent")
        self.normalize_phone_numbers = insights_config.get("normalize_phone_numbers", True)

    def _normalize_phone(self, phone: str) -> str:
        """Normalize phone number for consistent respondent tracking"""
        if not phone:
            return ""

        # If normalization is disabled, return as-is
        if not self.normalize_phone_numbers:
            return phone

        # Remove all non-digits
        digits = re.sub(r'\D', '', phone)
        # Handle US numbers (remove leading 1 if 11 digits)
        if len(digits) == 11 and digits[0] == '1':
            digits = digits[1:]
        return digits

    def _get_strongest_stance(self, mentions: List[Dict[str, Any]]) -> str:
        """
        Get the strongest stance from multiple mentions by the same person
        Priority: negative > requesting > positive > neutral
        """
        stances = [mention['stance'] for mention in mentions]

        # Priority order (strongest first)
        if 'negative' in stances:
            return 'negative'
        elif 'requesting' in stances:
            return 'requesting'
        elif 'positive' in stances:
            return 'positive'
        else:
            return 'neutral'

    def _flatten_taxonomy(self) -> Dict[str, str]:
        """Flatten hierarchical taxonomy for lookups"""
        descriptions = {}
        for primary, secondaries in HIERARCHICAL_TAXONOMY.items():
            for secondary, description in secondaries.items():
                key = f"{primary}/{secondary}"
                descriptions[key] = description
        return descriptions

    def generate_insights(self, messages: List[EnrichedMessage]) -> CampaignInsights:
        """Generate comprehensive insights from classified messages"""
        logger.info(f"Generating insights for {len(messages)} messages")

        # Filter to messages with classifications
        classified_messages = [
            msg for msg in messages
            if msg.smart_classification and not msg.smart_classification.should_be_uncategorized
        ]

        uncategorized_messages = len(messages) - len(classified_messages)

        logger.info(f"Analyzing {len(classified_messages)} classified messages")

        # Core analysis
        issue_insights = self._analyze_issues(classified_messages)
        compound_patterns = self._analyze_compound_patterns(classified_messages)
        root_cause_analysis = self._analyze_root_causes(classified_messages)
        sentiment_analysis = self._analyze_sentiment_patterns(classified_messages)
        quality_analysis = self._analyze_message_quality(messages)
        actionability_analysis = self._generate_actionability_insights(issue_insights, compound_patterns)
        hierarchical_counts = self._generate_hierarchical_counts(classified_messages)

        # Create comprehensive insights
        insights = CampaignInsights(
            total_messages=len(messages),
            substantive_messages=len(classified_messages),
            uncategorized_messages=uncategorized_messages,

            top_issues=sorted(issue_insights.values(), key=lambda x: x.total_mentions, reverse=True)[:10],
            most_criticized_issues=self._get_issues_by_stance(issue_insights, IssueStance.NEGATIVE)[:10],
            most_praised_issues=self._get_issues_by_stance(issue_insights, IssueStance.POSITIVE)[:5],
            most_requested_improvements=self._get_issues_by_stance(issue_insights, IssueStance.REQUESTING)[:10],

            compound_issue_patterns=sorted(compound_patterns, key=lambda x: x.frequency, reverse=True)[:10],
            root_cause_analysis=root_cause_analysis,

            overall_sentiment_distribution=sentiment_analysis["overall"],
            issue_specific_sentiment=sentiment_analysis["by_issue"],

            message_quality_distribution=quality_analysis["quality"],
            content_type_distribution=quality_analysis["content_type"],

            priority_issues=actionability_analysis["priority"],
            success_stories=actionability_analysis["success"],
            immediate_actions=actionability_analysis["immediate"],

            hierarchical_category_counts=hierarchical_counts
        )

        logger.info("Insights generation complete")
        return insights

    def _analyze_issues(self, messages: List[EnrichedMessage]) -> Dict[str, IssueInsight]:
        """Analyze individual issues across all messages with configurable aggregation"""

        # Use legacy message-based aggregation if configured
        if self.aggregation_mode == "message":
            return self._analyze_issues_legacy(messages)

        # Use respondent-based aggregation (default)
        logger.info(f"Using respondent-based aggregation with phone normalization: {self.normalize_phone_numbers}")

        # Track unique respondents per issue and all mentions for intensity analysis
        issue_respondents = defaultdict(set)  # issue_key -> set of normalized phone numbers
        issue_mentions = defaultdict(list)    # issue_key -> list of all mention details

        for message in messages:
            if not message.smart_classification:
                continue

            phone = self._normalize_phone(message.original_data.contact_phone_number)
            if not phone:
                logger.warning(f"Skipping message without valid phone number")
                continue

            for issue in message.smart_classification.issues:
                key = f"{issue.primary_category}/{issue.secondary_category}"

                # Track unique respondent
                issue_respondents[key].add(phone)

                # Track all mentions for analysis
                issue_mentions[key].append({
                    'phone': phone,
                    'stance': issue.stance.value,
                    'sentiment': message.smart_classification.overall_sentiment.value,
                    'concern': issue.specific_concern,
                    'message_text': message.original_data.message_text[:100],
                    'is_substantive': message.is_substantive
                })

        # Convert to IssueInsight objects
        issue_insights = {}
        for key in issue_respondents.keys():
            primary, secondary = key.split('/', 1)

            respondent_set = issue_respondents[key]
            mention_list = issue_mentions[key]

            unique_count = len(respondent_set)
            total_count = len(mention_list)
            mentions_per_respondent = total_count / unique_count if unique_count > 0 else 0

            # Aggregate stances by respondent (take strongest stance per person)
            respondent_stances = {}
            for phone in respondent_set:
                person_mentions = [m for m in mention_list if m['phone'] == phone]
                strongest_stance = self._get_strongest_stance(person_mentions)
                respondent_stances[phone] = strongest_stance

            # Count unique respondents by stance
            respondent_stance_counts = Counter(respondent_stances.values())

            # Legacy stance distribution (all mentions)
            all_stance_counts = Counter(m['stance'] for m in mention_list)

            # Sentiment breakdown
            sentiment_counts = Counter(m['sentiment'] for m in mention_list)

            # Sample messages from different respondents
            sample_messages = []
            seen_phones = set()
            for mention in mention_list:
                if len(sample_messages) >= 5:
                    break
                if mention['phone'] not in seen_phones:
                    sample_messages.append(mention['message_text'])
                    seen_phones.add(mention['phone'])

            # Key concerns from unique respondents
            unique_concerns = []
            for mention in mention_list:
                if mention['concern'] and mention['concern'] not in unique_concerns:
                    unique_concerns.append(mention['concern'])

            # Calculate urgency score based on unique respondents and negative sentiment
            negative_respondent_ratio = respondent_stance_counts['negative'] / unique_count if unique_count > 0 else 0
            respondent_score = min(1.0, unique_count / 30)  # Lower threshold for unique respondents
            urgency_score = (negative_respondent_ratio * 0.7 + respondent_score * 0.3) * 100

            insight = IssueInsight(
                primary_category=primary,
                secondary_category=secondary,
                unique_respondents=unique_count,
                total_mentions=total_count,
                mentions_per_respondent=round(mentions_per_respondent, 1),
                respondent_stances=dict(respondent_stance_counts),
                stance_distribution=dict(all_stance_counts),  # Legacy for backward compatibility
                sentiment_breakdown=dict(sentiment_counts),
                sample_messages=sample_messages,
                urgency_score=urgency_score,
                key_concerns=unique_concerns[:5]  # Top 5 unique concerns
            )
            issue_insights[key] = insight

        logger.info(f"Analyzed {len(issue_insights)} unique issue categories from {len(messages)} messages")
        return issue_insights

    def _analyze_issues_legacy(self, messages: List[EnrichedMessage]) -> Dict[str, IssueInsight]:
        """Legacy message-based aggregation for backward compatibility"""
        logger.info("Using legacy message-based aggregation")

        issue_data = defaultdict(lambda: {
            'total_mentions': 0,
            'stance_counts': Counter(),
            'sentiment_counts': Counter(),
            'sample_messages': [],
            'concerns': []
        })

        # Collect data for each issue (legacy approach)
        for message in messages:
            if not message.smart_classification:
                continue

            for issue in message.smart_classification.issues:
                key = f"{issue.primary_category}/{issue.secondary_category}"
                data = issue_data[key]

                data['total_mentions'] += 1
                data['stance_counts'][issue.stance.value] += 1
                data['sentiment_counts'][message.smart_classification.overall_sentiment.value] += 1

                # Collect sample messages (limit to avoid too much data)
                if len(data['sample_messages']) < 5:
                    data['sample_messages'].append(message.original_data.message_text[:100])

                # Collect specific concerns
                if issue.specific_concern and issue.specific_concern not in data['concerns']:
                    data['concerns'].append(issue.specific_concern)

        # Convert to IssueInsight objects (adjusted for new structure)
        issue_insights = {}
        for key, data in issue_data.items():
            primary, secondary = key.split('/', 1)

            # Calculate urgency score based on frequency and negative sentiment
            negative_ratio = data['stance_counts']['negative'] / data['total_mentions'] if data['total_mentions'] > 0 else 0
            frequency_score = min(1.0, data['total_mentions'] / 50)  # Normalize to max 50 mentions
            urgency_score = (negative_ratio * 0.7 + frequency_score * 0.3) * 100

            insight = IssueInsight(
                primary_category=primary,
                secondary_category=secondary,
                unique_respondents=data['total_mentions'],  # In legacy mode, treat each message as unique respondent
                total_mentions=data['total_mentions'],
                mentions_per_respondent=1.0,  # 1:1 in legacy mode
                respondent_stances=dict(data['stance_counts']),  # Same as stance distribution in legacy
                stance_distribution=dict(data['stance_counts']),
                sentiment_breakdown=dict(data['sentiment_counts']),
                sample_messages=data['sample_messages'],
                urgency_score=urgency_score,
                key_concerns=data['concerns'][:5]  # Top 5 concerns
            )
            issue_insights[key] = insight

        return issue_insights

    def _analyze_compound_patterns(self, messages: List[EnrichedMessage]) -> List[CompoundIssuePattern]:
        """Analyze patterns of issues that appear together"""
        pattern_counts = Counter()
        pattern_stances = defaultdict(lambda: defaultdict(Counter))
        pattern_samples = defaultdict(list)

        for message in messages:
            if not message.smart_classification or len(message.smart_classification.issues) < 2:
                continue

            # Get all issue combinations in this message
            issue_keys = [
                f"{issue.primary_category}/{issue.secondary_category}"
                for issue in message.smart_classification.issues
            ]

            # Sort to ensure consistent ordering
            issue_keys.sort()
            pattern_key = tuple(issue_keys)

            pattern_counts[pattern_key] += 1

            # Track stances for each issue in this pattern
            for issue in message.smart_classification.issues:
                issue_key = f"{issue.primary_category}/{issue.secondary_category}"
                pattern_stances[pattern_key][issue_key][issue.stance.value] += 1

            # Collect sample messages
            if len(pattern_samples[pattern_key]) < 3:
                pattern_samples[pattern_key].append(message.original_data.message_text[:150])

        # Convert to CompoundIssuePattern objects
        patterns = []
        for pattern_key, frequency in pattern_counts.most_common(20):
            if frequency >= 2:  # Only patterns that appear multiple times
                # Get most common stance for each issue in pattern
                common_stances = {}
                for issue_key, stance_counts in pattern_stances[pattern_key].items():
                    common_stances[issue_key] = stance_counts.most_common(1)[0][0]

                pattern = CompoundIssuePattern(
                    issue_combination=list(pattern_key),
                    frequency=frequency,
                    common_stances=common_stances,
                    sample_messages=pattern_samples[pattern_key]
                )
                patterns.append(pattern)

        return patterns

    def _analyze_root_causes(self, messages: List[EnrichedMessage]) -> Dict[str, List[str]]:
        """Analyze root cause relationships"""
        root_cause_effects = defaultdict(set)

        for message in messages:
            if not message.smart_classification:
                continue

            root_causes = []
            effects = []

            for issue in message.smart_classification.issues:
                issue_key = f"{issue.primary_category}/{issue.secondary_category}"
                if issue.is_root_cause:
                    root_causes.append(issue_key)
                else:
                    effects.append(issue_key)

            # Connect root causes to their effects
            for root_cause in root_causes:
                for effect in effects:
                    if root_cause != effect:
                        root_cause_effects[root_cause].add(effect)

        # Convert to regular dict with lists
        return {
            root_cause: list(effects)
            for root_cause, effects in root_cause_effects.items()
            if len(effects) > 0
        }

    def _analyze_sentiment_patterns(self, messages: List[EnrichedMessage]) -> Dict[str, Any]:
        """Analyze sentiment patterns overall and by issue"""
        overall_sentiment = Counter()
        issue_sentiment = defaultdict(Counter)

        for message in messages:
            if not message.smart_classification:
                continue

            overall_sentiment[message.smart_classification.overall_sentiment.value] += 1

            for issue in message.smart_classification.issues:
                issue_key = f"{issue.primary_category}/{issue.secondary_category}"
                issue_sentiment[issue_key][issue.stance.value] += 1

        return {
            "overall": dict(overall_sentiment),
            "by_issue": {k: dict(v) for k, v in issue_sentiment.items()}
        }

    def _analyze_message_quality(self, messages: List[EnrichedMessage]) -> Dict[str, Dict[str, int]]:
        """Analyze message quality and content type distribution"""
        quality_counts = Counter()
        content_type_counts = Counter()

        for message in messages:
            if message.smart_classification:
                quality_counts[message.smart_classification.message_quality.value] += 1
                content_type_counts[message.smart_classification.content_type.value] += 1

        return {
            "quality": dict(quality_counts),
            "content_type": dict(content_type_counts)
        }

    def _generate_hierarchical_counts(self, messages: List[EnrichedMessage]) -> HierarchicalCategoryCounts:
        """Generate hierarchical category counts (primary and secondary level breakdown)"""
        primary_counts = Counter()
        secondary_counts = defaultdict(Counter)
        total_categorized = 0

        for message in messages:
            if not message.smart_classification or message.smart_classification.should_be_uncategorized:
                continue

            for issue in message.smart_classification.issues:
                primary = issue.primary_category
                secondary = issue.secondary_category

                # Count primary categories
                primary_counts[primary] += 1

                # Count secondary categories within each primary
                secondary_counts[primary][secondary] += 1

                total_categorized += 1

        # Convert counters to regular dicts
        return HierarchicalCategoryCounts(
            primary_counts=dict(primary_counts),
            secondary_counts={primary: dict(secondary_dict) for primary, secondary_dict in secondary_counts.items()},
            total_categorized=total_categorized
        )

    def _get_issues_by_stance(self, issue_insights: Dict[str, IssueInsight], stance: IssueStance) -> List[IssueInsight]:
        """Get issues filtered and sorted by specific stance"""
        stance_value = stance.value
        filtered_issues = []

        for insight in issue_insights.values():
            stance_count = insight.stance_distribution.get(stance_value, 0)
            # Only include issues where this stance represents a significant portion (at least 20% or 2+ mentions)
            if stance_count >= 2 or (stance_count > 0 and stance_count / insight.total_mentions >= 0.2):
                # Keep original urgency score, sort by stance ratio without modifying
                stance_ratio = stance_count / insight.total_mentions
                filtered_issues.append((insight, stance_ratio))

        # Sort by stance ratio but return original insights with correct urgency scores
        sorted_items = sorted(filtered_issues, key=lambda x: x[1], reverse=True)
        return [item[0] for item in sorted_items]

    def _generate_actionability_insights(self, issue_insights: Dict[str, IssueInsight],
                                       compound_patterns: List[CompoundIssuePattern]) -> Dict[str, List[str]]:
        """Generate actionable insights for decision makers"""

        # Priority issues: High frequency + high negative sentiment
        priority_issues = []
        for insight in issue_insights.values():
            negative_ratio = insight.stance_distribution.get('negative', 0) / insight.total_mentions
            if insight.total_mentions >= 5 and negative_ratio >= 0.7:
                issue_name = f"{insight.primary_category.replace('_', ' ').title()}: {insight.secondary_category.replace('_', ' ').title()}"
                priority_issues.append(f"{issue_name} ({insight.total_mentions} complaints, {negative_ratio:.0%} negative)")

        # Success stories: Issues with positive sentiment
        success_stories = []
        for insight in issue_insights.values():
            positive_count = insight.stance_distribution.get('positive', 0)
            if positive_count >= 2:
                issue_name = f"{insight.primary_category.replace('_', ' ').title()}: {insight.secondary_category.replace('_', ' ').title()}"
                success_stories.append(f"{issue_name} ({positive_count} positive mentions)")

        # Immediate actions: High-impact root causes
        immediate_actions = []
        root_cause_impact = {}

        for insight in issue_insights.values():
            if any(issue.is_root_cause for msg in [insight] for issue in getattr(msg, 'issues', [])):
                issue_name = f"{insight.primary_category.replace('_', ' ').title()}: {insight.secondary_category.replace('_', ' ').title()}"
                # Count how many compound patterns include this as root cause
                pattern_impact = sum(
                    1 for pattern in compound_patterns
                    if f"{insight.primary_category}/{insight.secondary_category}" in pattern.issue_combination
                )
                if pattern_impact > 0:
                    immediate_actions.append(f"Address {issue_name} (root cause affecting {pattern_impact} issue combinations)")

        return {
            "priority": sorted(priority_issues)[:5],
            "success": sorted(success_stories)[:3],
            "immediate": sorted(immediate_actions)[:3]
        }

    def generate_insights_report(self, insights: CampaignInsights) -> str:
        """Generate a comprehensive insights report"""
        report_lines = [
            "# Civic Message Insights Report",
            "",
            f"**Total Messages:** {insights.total_messages:,}",
            f"**Substantive Messages:** {insights.substantive_messages:,} ({insights.substantive_messages/insights.total_messages:.1%})",
            f"**Uncategorized Messages:** {insights.uncategorized_messages:,}",
            "",
            "## 🔥 Top Priority Issues",
        ]

        # Priority issues - now based on unique respondents
        for i, issue in enumerate(insights.top_issues[:5], 1):
            negative_count = issue.respondent_stances.get('negative', 0)
            requesting_count = issue.respondent_stances.get('requesting', 0)
            issue_name = f"{issue.primary_category.replace('_', ' ').title()}: {issue.secondary_category.replace('_', ' ').title()}"

            report_lines.append(f"{i}. **{issue_name}**")
            report_lines.append(f"   - **{issue.unique_respondents} unique residents** ({issue.total_mentions} total mentions)")

            if issue.mentions_per_respondent > 1.0:
                report_lines.append(f"   - {issue.mentions_per_respondent} mentions per person on average")

            # Show stance breakdown for respondents
            stance_parts = []
            if negative_count > 0:
                stance_parts.append(f"{negative_count} with concerns")
            if requesting_count > 0:
                stance_parts.append(f"{requesting_count} requesting action")
            if stance_parts:
                report_lines.append(f"   - Resident stances: {', '.join(stance_parts)}")

            report_lines.append(f"   - Urgency Score: {issue.urgency_score:.0f}/100")
            if issue.key_concerns:
                report_lines.append(f"   - Key Concerns: {', '.join(issue.key_concerns[:2])}")
            report_lines.append("")

        # Category Distribution
        if insights.hierarchical_category_counts.total_categorized > 0:
            report_lines.extend([
                "## 📊 Category Distribution",
                "",
                "**Primary Categories:**"
            ])

            # Sort primary categories by count
            sorted_primary = sorted(
                insights.hierarchical_category_counts.primary_counts.items(),
                key=lambda x: x[1],
                reverse=True
            )

            for primary, count in sorted_primary:
                percentage = count / insights.hierarchical_category_counts.total_categorized * 100
                primary_name = primary.replace('_', ' ').title()
                report_lines.append(f"- {primary_name}: {count} mentions ({percentage:.1f}%)")

            report_lines.extend(["", "**Detailed Breakdown:**"])

            # Show breakdown for each primary category
            for primary, count in sorted_primary[:5]:  # Top 5 primary categories
                primary_name = primary.replace('_', ' ').title()
                report_lines.append(f"{primary_name} ({count}):")

                # Get secondary categories for this primary
                secondary_dict = insights.hierarchical_category_counts.secondary_counts.get(primary, {})
                sorted_secondary = sorted(secondary_dict.items(), key=lambda x: x[1], reverse=True)

                for secondary, sec_count in sorted_secondary:
                    secondary_name = secondary.replace('_', ' ').title()
                    report_lines.append(f"  - {secondary_name}: {sec_count}")

                report_lines.append("")

        # Success stories
        if insights.most_praised_issues:
            report_lines.extend([
                "## ✅ What's Working Well",
            ])
            for issue in insights.most_praised_issues[:3]:
                positive_count = issue.respondent_stances.get('positive', 0)
                total_positive_mentions = issue.stance_distribution.get('positive', 0)
                issue_name = f"{issue.primary_category.replace('_', ' ').title()}: {issue.secondary_category.replace('_', ' ').title()}"

                if total_positive_mentions > positive_count:
                    report_lines.append(f"- **{issue_name}**: {positive_count} residents with positive feedback ({total_positive_mentions} total positive mentions)")
                else:
                    report_lines.append(f"- **{issue_name}**: {positive_count} residents with positive feedback")

        report_lines.append("")

        # Compound patterns
        if insights.compound_issue_patterns:
            report_lines.extend([
                "## 🔗 Related Issue Patterns",
                "Issues that frequently appear together:"
            ])
            for pattern in insights.compound_issue_patterns[:3]:
                issue_names = [name.split('/')[-1].replace('_', ' ').title() for name in pattern.issue_combination]
                report_lines.append(f"- **{' + '.join(issue_names)}** ({pattern.frequency} messages)")

        # Root causes
        if insights.root_cause_analysis:
            report_lines.extend([
                "",
                "## 🎯 Root Cause Analysis",
                "Address these root causes to solve multiple issues:"
            ])
            for root_cause, effects in list(insights.root_cause_analysis.items())[:3]:
                cause_name = root_cause.split('/')[-1].replace('_', ' ').title()
                effect_names = [effect.split('/')[-1].replace('_', ' ').title() for effect in effects[:2]]
                report_lines.append(f"- **{cause_name}** → affects {', '.join(effect_names)}")

        # Actionable recommendations
        if insights.immediate_actions:
            report_lines.extend([
                "",
                "## 🚀 Immediate Action Items",
            ])
            for action in insights.immediate_actions:
                report_lines.append(f"- {action}")

        return "\n".join(report_lines)


def main():
    """Test the aggregator"""
    # This would normally be called with real classified messages
    print("SmartAggregator ready for testing with classified messages")


if __name__ == "__main__":
    main()