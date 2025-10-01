#!/usr/bin/env python3

"""
Multi-cluster analyzer stage for hierarchical clustering pipeline.
Handles cluster analysis for multiple cluster counts efficiently.
"""

import asyncio
import random
import time
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from pydantic import BaseModel, Field

from shared.logger import get_logger
from shared.llm_gemini import GeminiClient, GeminiModelType
from ..models import PipelineConfig, ClusterAnalysis, ClusterTheme, ClusteredMessage

logger = get_logger(__name__)

class ClusterAnalysisResponse(BaseModel):
    category: str = Field(..., description="High-level civic category from: Infrastructure, Public Safety, Education, Healthcare, Housing, Transportation, Environment, Governance, Economic Development, Community Services, Other")
    theme: str = Field(..., description="2-4 word concise theme/label for this cluster")
    issues_summary: str = Field(..., description="1 sentence describing the core issues or concerns")
    detailed_analysis: str = Field(..., description="2-3 paragraphs analyzing common concerns, patterns, underlying issues")
    verbatim_quotes: List[str] = Field(default_factory=list, description="3-5 short snippets (max 15 words each). Extract ONLY the most relevant phrase from original messages. If message has multiple topics, extract ONLY the part about THIS theme. Remove dashes and quotes.")
    key_topics: List[str] = Field(default_factory=list, description="5 key topics mentioned in this cluster")
    sentiment: str = Field(..., description="Overall sentiment: positive, negative, neutral, mixed, or concerned")
    action_items: List[str] = Field(default_factory=list, description="At least 3 specific, actionable items. If citizens don't explicitly request actions, infer reasonable actions from their concerns")
    civic_relevance: str = Field(..., description="How this relates to local governance and community needs")
    confidence: str = Field(..., description="Confidence level: High, Medium, or Low")

class MultiClusterAnalyzer:
    """Efficient multi-cluster analysis with proper API management"""

    def __init__(self, config: PipelineConfig):
        self.config = config
        analysis_config = getattr(config, 'analysis', {})
        llm_config = analysis_config.get('llm_config', {})

        # Conservative settings for multi-cluster to avoid API overload
        self.llm_client = GeminiClient(
            default_model=GeminiModelType.FLASH,
            default_temperature=llm_config.get('temperature', 0.0),
            thinking_budget=llm_config.get('thinking_budget', 0),
            max_connections=25,  # Reduced from 100
            max_keepalive_connections=10  # Reduced from 25
        )

        self.max_example_messages = analysis_config.get('max_example_messages', 30)
        self.save_example_messages = analysis_config.get('save_example_messages', 5)

        # Multi-cluster specific settings
        self.delay_between_runs = 5.0  # 5 second delay between cluster counts
        self.delay_between_clusters = 0.2  # 200ms delay between individual cluster analyses

    async def analyze_multi_cluster(self, multi_cluster_results: Dict[str, Dict]) -> Dict[str, List[ClusterAnalysis]]:
        """
        Analyze themes for multiple cluster configurations

        Args:
            multi_cluster_results: Dict with cluster count as key, containing 'clustered_messages'

        Returns:
            Dict mapping cluster count to list of ClusterAnalysis objects
        """
        logger.info(f"🔍 Starting multi-cluster theme analysis for {len(multi_cluster_results)} configurations")

        analyzed_results = {}

        for i, (cluster_count_str, result) in enumerate(multi_cluster_results.items()):
            cluster_count = int(cluster_count_str)
            clustered_messages = result['clustered_messages']

            logger.info(f"🔄 Analyzing themes for {cluster_count} clusters ({len(clustered_messages)} messages)...")

            # Add delay between cluster count runs to prevent API overload
            if i > 0:
                logger.info(f"⏸️  Waiting {self.delay_between_runs}s between cluster configurations...")
                time.sleep(self.delay_between_runs)

            try:
                # Analyze this cluster configuration
                cluster_analyses = await self._analyze_single_configuration(clustered_messages, cluster_count)
                analyzed_results[cluster_count_str] = cluster_analyses

                if cluster_analyses:
                    logger.info(f"✅ Successfully analyzed {len(cluster_analyses)} clusters for {cluster_count}-cluster configuration")
                    # Log first theme as example
                    first_cluster = cluster_analyses[0]
                    logger.info(f"Example theme: Cluster {first_cluster.cluster_id} -> '{first_cluster.theme_analysis.theme}'")
                else:
                    logger.warning(f"❌ No themes generated for {cluster_count} clusters")

            except Exception as e:
                logger.error(f"❌ Failed to analyze {cluster_count} clusters: {e}")
                analyzed_results[cluster_count_str] = []

        logger.info(f"🎯 Multi-cluster analysis complete. Generated themes for {sum(1 for v in analyzed_results.values() if v)} configurations")
        return analyzed_results

    async def _analyze_single_configuration(self, clustered_messages: List[ClusteredMessage], cluster_count: int) -> List[ClusterAnalysis]:
        """Analyze all clusters for a single cluster count configuration"""

        # Calculate total unique respondents for this dataset
        total_respondents = len(set(msg.csv_row_index for msg in clustered_messages))
        logger.info(f"Total unique respondents in dataset: {total_respondents}")

        # Group messages by cluster
        clusters_dict = {}
        for message in clustered_messages:
            cluster_id = message.cluster_assignment.cluster_id
            if cluster_id not in clusters_dict:
                clusters_dict[cluster_id] = []
            clusters_dict[cluster_id].append(message)

        logger.info(f"Found {len(clusters_dict)} unique clusters for {cluster_count}-cluster configuration")

        # Create analysis tasks for all clusters
        analysis_tasks = []
        for cluster_id, messages in clusters_dict.items():
            task = self._analyze_single_cluster(cluster_id, messages, cluster_count, total_respondents)
            analysis_tasks.append(task)

            # Small delay between creating tasks to spread out API calls
            if len(analysis_tasks) > 1:
                await asyncio.sleep(self.delay_between_clusters)

        # Execute all cluster analyses concurrently but with limited concurrency
        cluster_analyses = []
        chunk_size = 5  # Process 5 clusters at a time to avoid overwhelming API

        for i in range(0, len(analysis_tasks), chunk_size):
            chunk = analysis_tasks[i:i + chunk_size]
            logger.info(f"Processing cluster analysis chunk {i//chunk_size + 1}/{(len(analysis_tasks) + chunk_size - 1)//chunk_size}")

            try:
                chunk_results = await asyncio.gather(*chunk, return_exceptions=True)

                for result in chunk_results:
                    if isinstance(result, Exception):
                        logger.error(f"Cluster analysis failed: {result}")
                    elif result:
                        cluster_analyses.append(result)

            except Exception as e:
                logger.error(f"Chunk processing failed: {e}")

            # Delay between chunks
            if i + chunk_size < len(analysis_tasks):
                await asyncio.sleep(1.0)

        # Sort by cluster_id for consistent output
        cluster_analyses.sort(key=lambda x: x.cluster_id)

        return cluster_analyses

    async def _analyze_single_cluster(self, cluster_id: int, messages: List[ClusteredMessage],
                                     total_clusters: int, total_respondents: int) -> Optional[ClusterAnalysis]:
        """Analyze a single cluster to generate theme"""

        if not messages:
            logger.warning(f"No messages in cluster {cluster_id}")
            return None

        # Calculate person-level metrics for this cluster
        person_metrics = self._calculate_person_level_metrics(messages, total_respondents)

        # Prepare example messages (up to max_example_messages) using random sampling
        if len(messages) > self.max_example_messages:
            # Randomly sample messages for better cluster representation
            sample_messages = random.sample(messages, self.max_example_messages)
        else:
            # Use all messages if we have max_example_messages or fewer
            sample_messages = messages

        # Use original_text (actual citizen words) not processed text for quotes
        example_texts = [msg.original_text for msg in sample_messages]

        # Create analysis prompt with person-level context
        prompt = self._create_analysis_prompt(cluster_id, example_texts, len(messages), total_clusters, person_metrics)

        try:
            # Generate theme analysis with structured output
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.llm_client.generate_structured_content(
                    prompt=prompt,
                    response_schema=ClusterAnalysisResponse,
                    system_instruction="You are an expert civic message analyst. Analyze citizen messages and identify themes, issues, and actionable items. For verbatim_quotes: extract SHORT snippets (max 15 words) that capture THIS cluster's theme. Remove dashes, quote marks, and extract only relevant portions from longer messages."
                )
            )

            if not response:
                logger.warning(f"Empty response for cluster {cluster_id}")
                return None

            # Debug logging: show parsed response
            logger.debug(f"Structured LLM response for cluster {cluster_id}: theme='{response.theme}', {len(response.verbatim_quotes)} quotes, {len(response.action_items)} actions")

            # Convert Pydantic response to ClusterTheme dataclass
            cluster_theme = self._convert_response_to_cluster_theme(response, cluster_id)

            # Debug logging: show parsed action items count
            logger.debug(f"Parsed {len(cluster_theme.action_items)} action items for cluster {cluster_id}: {cluster_theme.action_items}")

            # Verbatim quotes are already from the LLM that saw the actual messages
            # No need for a second LLM call to "verify" them - just use what we parsed
            if cluster_theme.verbatim_quotes:
                logger.info(f"Using {len(cluster_theme.verbatim_quotes)} verbatim quotes from cluster analysis for cluster {cluster_id}")

            # Validate action items are present
            cluster_theme = await self._validate_and_enhance_action_items(cluster_theme, cluster_id, sample_messages)

            # Create ClusterAnalysis object with person-level metrics
            cluster_analysis = ClusterAnalysis(
                cluster_id=cluster_id,
                size=len(messages),
                theme_analysis=cluster_theme,
                example_messages=[msg.text for msg in messages[:self.save_example_messages]],
                message_ids=[msg.id for msg in messages]
            )

            # Store person-level metrics as temporary attributes for orchestrator to access
            cluster_analysis.unique_respondents = person_metrics['unique_respondents']
            cluster_analysis.total_mentions = person_metrics['total_mentions']
            cluster_analysis.avg_mentions_per_respondent = person_metrics['avg_mentions_per_respondent']
            cluster_analysis.respondent_coverage_pct = person_metrics['respondent_coverage_pct']
            cluster_analysis.respondent_mention_distribution = person_metrics['respondent_mention_distribution']

            return cluster_analysis

        except Exception as e:
            logger.error(f"Failed to analyze cluster {cluster_id}: {e}")
            return None

    def _create_analysis_prompt(self, cluster_id: int, example_texts: List[str], cluster_size: int,
                              total_clusters: int, person_metrics: Dict[str, Any]) -> str:
        """Create analysis prompt for cluster theme generation with person-level context"""

        examples_text = "\n".join([f"- {text}" for text in example_texts[:10]])  # Limit to 10 examples in prompt

        # Include person-level context in the prompt
        unique_respondents = person_metrics.get('unique_respondents', cluster_size)
        avg_mentions = person_metrics.get('avg_mentions_per_respondent', 1.0)
        coverage_pct = person_metrics.get('respondent_coverage_pct', 0.0)

        prompt = f"""Analyze this cluster of civic engagement messages from political campaigns.

CLUSTER INFO:
- Cluster ID: {cluster_id}
- Total Messages: {cluster_size} messages
- Unique Citizens: {unique_respondents} different people
- Average Mentions per Citizen: {avg_mentions:.1f}
- Respondent Coverage: {coverage_pct:.1f}% of all survey respondents
- Part of {total_clusters} total clusters

ORIGINAL CITIZEN MESSAGES (verbatim responses from survey):
{examples_text}

Provide a comprehensive analysis of this cluster:

1. **Category**: Identify the high-level civic category (Infrastructure, Public Safety, Education, Healthcare, Housing, Transportation, Environment, Governance, Economic Development, Community Services, or Other)

2. **Theme**: Create a concise 2-4 word theme/label that captures the essence of this cluster

3. **Issues Summary**: Write 1 sentence describing the core issues or concerns people are expressing

4. **Detailed Analysis**: Write 2-3 paragraphs analyzing common concerns, patterns, underlying issues, and what citizens are experiencing. Focus on the problems, frustrations, or needs expressed.

5. **Verbatim Quotes** (CRITICAL): Extract 3-5 SHORT snippets (MAXIMUM 15 words each)
   - Extract ONLY the most relevant phrase from each message that captures THIS cluster's theme
   - If a message has multiple topics, extract ONLY the part about THIS specific theme
   - DO NOT include dashes, quote marks, or numbering - just the clean text
   - Example: Message "Taxes too high. Water bill insane. Roads bad" + Water cluster → ["water bill insane"]
   - Example: Message "Bad drivers speeding down residential streets ignoring stop signs" + Traffic cluster → ["drivers speeding down residential streets ignoring stop signs"]

6. **Key Topics**: List 5 key topics mentioned in this cluster

7. **Sentiment**: Identify overall sentiment (positive, negative, neutral, mixed, or concerned)

8. **Action Items** (MANDATORY): Provide at least 3 specific, actionable items
   - If citizens don't explicitly request actions, infer reasonable actions from their concerns
   - Example: Traffic complaints → ["Install traffic calming measures", "Increase traffic enforcement", "Add speed monitoring signs"]
   - Each action must be specific and actionable by local government or campaigns

9. **Civic Relevance**: Explain how this relates to local governance and community needs

10. **Confidence**: Rate your confidence (High, Medium, or Low) based on message coherence and theme clarity

Focus on:
- WHY citizens are contacting campaigns - what problems drive these messages
- What specific issues, frustrations, or concerns are expressed
- What citizens want done (MANDATORY action items)
- Direct citizen voices through clean verbatim quotes
- How this relates to local governance and community engagement"""

        return prompt

    def _calculate_person_level_metrics(self, clustered_messages: List[ClusteredMessage],
                                      total_respondents: int) -> Dict[str, Any]:
        """Calculate person-level metrics for cluster importance"""

        # Group messages by respondent (csv_row_index)
        respondent_groups = defaultdict(list)
        for message in clustered_messages:
            respondent_id = message.csv_row_index
            respondent_groups[respondent_id].append(message)

        unique_respondents = len(respondent_groups)
        total_mentions = len(clustered_messages)
        avg_mentions_per_respondent = total_mentions / unique_respondents if unique_respondents > 0 else 0
        respondent_coverage_pct = (unique_respondents / total_respondents * 100) if total_respondents > 0 else 0

        # Calculate distribution of mentions per respondent
        mentions_per_respondent = [len(messages) for messages in respondent_groups.values()]
        max_mentions = max(mentions_per_respondent) if mentions_per_respondent else 0

        logger.info(f"Person-level metrics: {unique_respondents} unique respondents, "
                   f"{total_mentions} total mentions, "
                   f"{avg_mentions_per_respondent:.2f} avg mentions/person, "
                   f"{respondent_coverage_pct:.1f}% coverage")

        return {
            'unique_respondents': unique_respondents,
            'total_mentions': total_mentions,
            'avg_mentions_per_respondent': round(avg_mentions_per_respondent, 2),
            'respondent_coverage_pct': round(respondent_coverage_pct, 2),
            'max_mentions_per_respondent': max_mentions,
            'respondent_mention_distribution': dict(respondent_groups),
            'mentions_per_respondent_counts': mentions_per_respondent
        }

    def _convert_response_to_cluster_theme(self, response: ClusterAnalysisResponse, cluster_id: int) -> ClusterTheme:
        """Convert Pydantic ClusterAnalysisResponse to ClusterTheme dataclass"""

        # Map confidence text to score
        confidence_score = 0.8  # Default
        if response.confidence.lower() == 'high':
            confidence_score = 0.9
        elif response.confidence.lower() == 'medium':
            confidence_score = 0.7
        elif response.confidence.lower() == 'low':
            confidence_score = 0.3

        # Create ClusterTheme object from structured response
        cluster_theme = ClusterTheme(
            category=response.category,
            theme=response.theme,
            summary=response.issues_summary,
            issues_summary=response.issues_summary,
            detailed_analysis=response.detailed_analysis,
            verbatim_quotes=response.verbatim_quotes,
            key_topics=response.key_topics,
            sentiment=response.sentiment.lower(),
            action_items=response.action_items,
            civic_relevance=response.civic_relevance,
            confidence_score=confidence_score
        )

        return cluster_theme

    async def _generate_action_items_with_llm(self, cluster_theme: ClusterTheme, sample_messages: List[ClusteredMessage], cluster_id: int) -> List[str]:
        """Generate action items using dedicated focused LLM call"""

        # Create focused prompt for action item generation using original citizen text
        message_texts = [msg.original_text for msg in sample_messages[:5]]
        messages_text = "\n".join([f"- {text}" for text in message_texts])

        prompt = f"""Generate 3-5 specific actionable items for local government or campaigns to address citizen concerns.

THEME: {cluster_theme.theme}
CATEGORY: {cluster_theme.category}
ORIGINAL CITIZEN CONCERNS (verbatim from survey):
{messages_text}

TASK: Generate 3-5 specific, actionable items that local government or campaigns can implement to address these citizen concerns.

REQUIREMENTS:
- Each action must be specific and concrete (not generic like "study the issue")
- Actions should directly address the concerns expressed in the messages
- Actions should be realistic for local government or campaign implementation
- If concerns are implicit (e.g., complaints about traffic), infer appropriate actions

FORMAT: Return one action per line, numbered:
1. [Specific action item]
2. [Specific action item]
3. [Specific action item]
4. [Specific action item] (optional)
5. [Specific action item] (optional)

Generate the action items now:"""

        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.llm_client.generate_content(
                    prompt=prompt
                )
            )

            if not response:
                logger.warning(f"Empty response from LLM for action items generation (cluster {cluster_id})")
                return []

            # Debug logging: show raw response from dedicated action items call
            logger.debug(f"Dedicated action items LLM response for cluster {cluster_id}: {response}")

            # Parse numbered action items from response
            import re
            action_items = []
            for line in response.strip().split('\n'):
                # Match numbered items like "1. Action" or "1) Action"
                match = re.match(r'^\s*\d+[\.)]\s*(.+)', line)
                if match:
                    action_items.append(match.group(1).strip())

            logger.info(f"Generated {len(action_items)} action items via dedicated LLM call for cluster {cluster_id}: {action_items}")
            return action_items[:5]  # Max 5 items

        except Exception as e:
            logger.error(f"Failed to generate action items via LLM for cluster {cluster_id}: {e}")
            return []

    async def _validate_and_enhance_action_items(self, cluster_theme: ClusterTheme, cluster_id: int, sample_messages: List[ClusteredMessage]) -> ClusterTheme:
        """Validate action items and use dedicated LLM call if insufficient"""

        # Check if action items are missing or too few
        if not cluster_theme.action_items or len(cluster_theme.action_items) < 3:
            logger.warning(f"Cluster {cluster_id} has insufficient action items ({len(cluster_theme.action_items)}). Making dedicated LLM call for action items.")

            # Make focused LLM call to generate action items
            generated_actions = await self._generate_action_items_with_llm(cluster_theme, sample_messages, cluster_id)

            if generated_actions and len(generated_actions) >= 3:
                cluster_theme.action_items = generated_actions
                logger.info(f"Successfully generated {len(generated_actions)} action items via LLM for cluster {cluster_id}")
            else:
                logger.error(f"Failed to generate sufficient action items for cluster {cluster_id} - keeping {len(cluster_theme.action_items)} existing items")

        return cluster_theme


    def get_usage_stats(self) -> Dict[str, Any]:
        """Get LLM usage statistics"""
        if hasattr(self.llm_client, 'get_usage_stats'):
            return self.llm_client.get_usage_stats()
        return {}


async def multi_cluster_analyzer_stage(multi_cluster_results: Dict[str, Dict], config: PipelineConfig) -> Dict[str, Any]:
    """
    Main entry point for multi-cluster analysis stage

    Args:
        multi_cluster_results: Dict with cluster count as key and clustered messages
        config: Pipeline configuration

    Returns:
        Dict with analyzed clusters and cost information
    """
    analyzer = MultiClusterAnalyzer(config)
    analyzed_results = await analyzer.analyze_multi_cluster(multi_cluster_results)

    # Get usage statistics
    usage_stats = analyzer.get_usage_stats()
    if usage_stats:
        logger.info(f"Multi-Cluster LLM Usage - Calls: {usage_stats.get('api_call_count', 0)}, "
                   f"Tokens: {usage_stats.get('total_tokens', 0):,}, "
                   f"Cost: ${usage_stats.get('total_cost', 0):.4f}")

    return {
        "analyses": analyzed_results,
        "cost": usage_stats.get('total_cost', 0) if usage_stats else 0,
        "usage_stats": usage_stats
    }