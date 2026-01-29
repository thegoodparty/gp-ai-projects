#!/usr/bin/env python3

"""
Multi-cluster analyzer stage for hierarchical clustering pipeline.
Handles cluster analysis for multiple cluster counts efficiently.
"""

import asyncio
import os
import random
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from pydantic import BaseModel, Field

from shared.logger import get_logger
from shared.llm_gemini_3 import Gemini3Client, GeminiModelType, ThinkingLevel
from shared.braintrust import init_braintrust, flush_logs, load_prompt_from_braintrust
from ..models import PipelineConfig, ClusterAnalysis, ClusterTheme, ClusteredMessage
from .cluster_merger import cluster_merger_stage
from .cluster_merger_analysis import analyze_cluster_merger

logger = get_logger(__name__)

class ClusterAnalysisResponse(BaseModel):
    category: str = Field(..., description="High-level civic category from: Infrastructure, Public Safety, Education, Healthcare, Housing, Transportation, Environment, Governance, Economic Development, Community Services, Other")
    theme: str = Field(..., description="2-4 word concise theme/label for this cluster")
    issues_summary: str = Field(..., description="1 sentence describing the core issues or concerns")
    detailed_analysis: str = Field(..., description="2-3 paragraphs analyzing common concerns, patterns, underlying issues")
    key_topics: List[str] = Field(default_factory=list, description="5 key topics mentioned in this cluster")
    sentiment: str = Field(..., description="Overall sentiment: positive, negative, neutral, mixed, or concerned")
    action_items: List[str] = Field(default_factory=list, description="At least 3 specific, actionable items. If citizens don't explicitly request actions, infer reasonable actions from their concerns")
    civic_relevance: str = Field(..., description="How this relates to local governance and community needs")
    confidence: str = Field(..., description="Confidence level: High, Medium, or Low")

class VerbatimQuotesResponse(BaseModel):
    quotes: List[str] = Field(default_factory=list, description="3-5 verbatim quotes (max 15 words each) directly from the messages that represent the cluster theme")

class ActionItemsResponse(BaseModel):
    action_items: List[str] = Field(default_factory=list, description="3-5 specific, actionable items that local government or campaigns can implement")

class MultiClusterAnalyzer:
    """Efficient multi-cluster analysis with proper API management"""

    def __init__(self, config: PipelineConfig):
        self.config = config
        analysis_config = getattr(config, 'analysis', {})
        llm_config = analysis_config.get('llm_config', {})

        self.llm_client = Gemini3Client(
            default_model=GeminiModelType.FLASH_3,
            default_temperature=llm_config.get('temperature', 0.0),
            thinking_level=ThinkingLevel.MINIMAL,
            max_connections=llm_config.get('max_connections', 25),
            max_keepalive_connections=llm_config.get('max_keepalive_connections', 10)
        )

        environment = os.getenv("ENVIRONMENT", "local")
        logger.info(f"Braintrust environment: {environment}")
        init_braintrust(project="hierarchical-discovery")

        self.max_example_messages = analysis_config.get('max_example_messages', 30)
        self.save_example_messages = analysis_config.get('save_example_messages', 5)

        self.chunk_size = 5
        self.delay_between_clusters = 0.2
        self.delay_between_chunks = 1.0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
        return False

    def cleanup(self):
        flush_logs()
        if hasattr(self, 'llm_client') and hasattr(self.llm_client, 'close'):
            try:
                self.llm_client.close()
            except Exception as e:
                logger.warning(f"Error closing LLM client: {e}")

    def __del__(self):
        self.cleanup()

    async def analyze_multi_cluster(self, multi_cluster_results: Dict[str, Dict]) -> Dict[str, Any]:
        """
        Analyze themes for multiple cluster configurations

        Args:
            multi_cluster_results: Dict with cluster count as key, containing 'clustered_messages'

        Returns:
            Dict with 'analyses' and 'merger_stats' keys
        """
        logger.info(f"Starting multi-cluster theme analysis for {len(multi_cluster_results)} configurations")

        analyzed_results = {}
        merger_stats = {}

        for i, (cluster_count_str, result) in enumerate(multi_cluster_results.items()):
            cluster_count = int(cluster_count_str)
            clustered_messages = result['clustered_messages']

            logger.info(f"Analyzing themes for {cluster_count} clusters ({len(clustered_messages)} messages)...")

            try:
                # Analyze this cluster configuration (returns tuple with merger stats)
                cluster_analyses, config_merger_stats = await self._analyze_single_configuration(clustered_messages, cluster_count)
                analyzed_results[cluster_count_str] = cluster_analyses
                merger_stats[cluster_count_str] = config_merger_stats

                if cluster_analyses:
                    logger.info(f"Successfully analyzed {len(cluster_analyses)} clusters for {cluster_count}-cluster configuration")
                    # Log first theme as example
                    first_cluster = cluster_analyses[0]
                    logger.debug(f"Example theme: Cluster {first_cluster.cluster_id} -> '{first_cluster.theme_analysis.theme}'")
                else:
                    logger.warning(f"No themes generated for {cluster_count} clusters")

            except Exception as e:
                logger.error(f"Failed to analyze {cluster_count} clusters: {e}")
                analyzed_results[cluster_count_str] = []
                merger_stats[cluster_count_str] = {'pre_merge_count': 0, 'post_merge_count': 0}

        logger.info(f"Multi-cluster analysis complete. Generated themes for {sum(1 for v in analyzed_results.values() if v)} configurations")
        return {
            'analyses': analyzed_results,
            'merger_stats': merger_stats
        }

    async def _analyze_single_configuration(self, clustered_messages: List[ClusteredMessage], cluster_count: int):
        """Analyze all clusters for a single cluster count configuration

        Returns:
            Tuple of (merged_analyses, merger_stats, original_analyses, merged_messages)
        """

        # Calculate total unique respondents for this dataset
        total_respondents = len(set(msg.csv_row_index for msg in clustered_messages))
        logger.debug(f"Total unique respondents in dataset: {total_respondents}")

        # Group messages by cluster
        clusters_dict = {}
        for message in clustered_messages:
            cluster_id = message.cluster_assignment.cluster_id
            if cluster_id not in clusters_dict:
                clusters_dict[cluster_id] = []
            clusters_dict[cluster_id].append(message)

        logger.debug(f"Found {len(clusters_dict)} unique clusters for {cluster_count}-cluster configuration")

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
        failed_clusters = []

        for i in range(0, len(analysis_tasks), self.chunk_size):
            chunk = analysis_tasks[i:i + self.chunk_size]
            chunk_start_idx = i
            logger.debug(f"Processing cluster analysis chunk {i//self.chunk_size + 1}/{(len(analysis_tasks) + self.chunk_size - 1)//self.chunk_size}")

            try:
                chunk_results = await asyncio.gather(*chunk, return_exceptions=True)

                for idx, result in enumerate(chunk_results):
                    global_idx = chunk_start_idx + idx
                    cluster_id = list(clusters_dict.keys())[global_idx] if global_idx < len(clusters_dict) else f"unknown_{global_idx}"

                    if isinstance(result, Exception):
                        logger.error(f"Cluster {cluster_id} analysis failed: {result}", exc_info=True)
                        failed_clusters.append({'cluster_id': cluster_id, 'error': str(result)})
                    elif result:
                        cluster_analyses.append(result)

            except Exception as e:
                logger.error(f"Chunk processing failed: {e}", exc_info=True)

            # Delay between chunks
            if i + self.chunk_size < len(analysis_tasks):
                await asyncio.sleep(self.delay_between_chunks)

        # Log summary of failures
        if failed_clusters:
            logger.warning(f"Analysis failed for {len(failed_clusters)}/{len(clusters_dict)} clusters: {[f['cluster_id'] for f in failed_clusters]}")
        else:
            logger.info(f"Successfully analyzed all {len(cluster_analyses)} clusters")

        # Sort by cluster_id for consistent output
        cluster_analyses.sort(key=lambda x: x.cluster_id)

        # Apply cluster merging to reduce duplicate themes
        cluster_themes_dict = {ca.cluster_id: ca.theme_analysis for ca in cluster_analyses}
        pre_merge_count = len(cluster_themes_dict)
        merged_messages, merged_themes = await cluster_merger_stage(clustered_messages, cluster_themes_dict, self.config)

        # Analyze merger results
        analyze_cluster_merger(merged_messages, merged_themes, pre_merge_count)

        # Rebuild cluster analyses with merged results
        if len(merged_themes) < len(cluster_themes_dict):
            logger.info(f"Cluster merger reduced {len(cluster_themes_dict)} → {len(merged_themes)} clusters")
            logger.info(f"Reanalyzing {len(merged_themes)} merged clusters with fresh LLM calls...")

            merged_clusters_dict = {}
            for msg in merged_messages:
                cluster_id = msg.cluster_assignment.cluster_id
                if cluster_id not in merged_clusters_dict:
                    merged_clusters_dict[cluster_id] = []
                merged_clusters_dict[cluster_id].append(msg)

            merged_cluster_ids = set()
            for msg in merged_messages:
                if msg.cluster_assignment.merged_cluster_id is not None:
                    merged_cluster_ids.add(msg.cluster_assignment.cluster_id)

            logger.debug(f"Identified {len(merged_cluster_ids)} clusters that need reanalysis")

            reanalysis_tasks = []
            for cluster_id in merged_cluster_ids:
                cluster_msgs = merged_clusters_dict.get(cluster_id, [])
                if cluster_msgs:
                    task = self._analyze_single_cluster(cluster_id, cluster_msgs, cluster_count, total_respondents)
                    reanalysis_tasks.append((cluster_id, task))

            merged_analyses = []
            for original_analysis in cluster_analyses:
                cluster_id = original_analysis.cluster_id

                if cluster_id not in merged_cluster_ids and cluster_id in merged_clusters_dict:
                    cluster_msgs = merged_clusters_dict[cluster_id]
                    merged_analysis = ClusterAnalysis(
                        cluster_id=cluster_id,
                        size=len(cluster_msgs),
                        theme_analysis=merged_themes[cluster_id],
                        example_messages=[msg.text for msg in cluster_msgs[:self.save_example_messages]],
                        message_ids=[msg.id for msg in cluster_msgs]
                    )

                    theme = merged_themes[cluster_id]
                    merged_analysis.unique_respondents = theme.unique_respondents
                    merged_analysis.total_mentions = theme.total_mentions
                    merged_analysis.avg_mentions_per_respondent = theme.avg_mentions_per_respondent
                    merged_analysis.respondent_coverage_pct = theme.respondent_coverage_pct

                    merged_analyses.append(merged_analysis)

            if reanalysis_tasks:
                logger.info(f"Reanalyzing {len(reanalysis_tasks)} merged clusters...")
                reanalyzed_results = await asyncio.gather(*[task for _, task in reanalysis_tasks], return_exceptions=True)

                for (cluster_id, _), result in zip(reanalysis_tasks, reanalyzed_results):
                    if isinstance(result, Exception):
                        logger.error(f"Reanalysis failed for cluster {cluster_id}: {result}")
                        cluster_msgs = merged_clusters_dict.get(cluster_id, [])
                        theme = merged_themes[cluster_id]
                        merged_analysis = ClusterAnalysis(
                            cluster_id=cluster_id,
                            size=len(cluster_msgs),
                            theme_analysis=theme,
                            example_messages=[msg.text for msg in cluster_msgs[:self.save_example_messages]],
                            message_ids=[msg.id for msg in cluster_msgs]
                        )
                        merged_analysis.unique_respondents = theme.unique_respondents
                        merged_analysis.total_mentions = theme.total_mentions
                        merged_analysis.avg_mentions_per_respondent = theme.avg_mentions_per_respondent
                        merged_analysis.respondent_coverage_pct = theme.respondent_coverage_pct
                        merged_analyses.append(merged_analysis)
                    elif result:
                        logger.debug(f"Reanalyzed cluster {cluster_id}: '{result.theme_analysis.theme}'")
                        merged_analyses.append(result)

            merged_analyses.sort(key=lambda x: x.cluster_id)
            return merged_analyses, {
                'pre_merge_count': pre_merge_count,
                'post_merge_count': len(merged_themes)
            }

        return cluster_analyses, {
            'pre_merge_count': len(cluster_themes_dict),
            'post_merge_count': len(cluster_themes_dict)
        }

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

        # Use atomic text (what was actually clustered) for theme analysis
        example_texts = [msg.text for msg in sample_messages]

        # Create analysis prompt with person-level context
        prompt = self._create_analysis_prompt(cluster_id, example_texts, len(messages), total_clusters, person_metrics)

        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.llm_client.generate_structured_content(
                    prompt=prompt,
                    response_schema=ClusterAnalysisResponse,
                    system_instruction="You are an expert civic message analyst. Analyze citizen messages and identify themes, issues, and actionable items.",
                    trace_name="cluster_analysis"
                )
            )

            if not response:
                logger.warning(f"Empty response for cluster {cluster_id}")
                return None

            logger.debug(f"Structured LLM response for cluster {cluster_id}: theme='{response.theme}', {len(response.action_items)} actions")

            cluster_theme = self._convert_response_to_cluster_theme(response, cluster_id)

            # Debug logging: show parsed action items count
            logger.debug(f"Parsed {len(cluster_theme.action_items)} action items for cluster {cluster_id}: {cluster_theme.action_items}")

            # Populate person-level metrics on ClusterTheme
            cluster_theme.cluster_id = cluster_id
            cluster_theme.unique_respondents = person_metrics['unique_respondents']
            cluster_theme.total_mentions = person_metrics['total_mentions']
            cluster_theme.avg_mentions_per_respondent = person_metrics['avg_mentions_per_respondent']
            cluster_theme.respondent_coverage_pct = person_metrics['respondent_coverage_pct']

            # Generate verbatim quotes from original full messages
            cluster_theme = await self._generate_verbatim_quotes(cluster_theme, cluster_id, sample_messages)

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

        examples_text = "\n".join([f"- {text}" for text in example_texts[:10]])

        unique_respondents = person_metrics.get('unique_respondents', cluster_size)
        avg_mentions = person_metrics.get('avg_mentions_per_respondent', 1.0)
        coverage_pct = person_metrics.get('respondent_coverage_pct', 0.0)

        variables = {
            "cluster_id": cluster_id,
            "cluster_size": cluster_size,
            "unique_respondents": unique_respondents,
            "avg_mentions": f"{avg_mentions:.1f}",
            "coverage_pct": f"{coverage_pct:.1f}",
            "total_clusters": total_clusters,
            "examples_text": examples_text
        }

        fallback_prompt = """Analyze this cluster of civic engagement messages from political campaigns.

CLUSTER INFO:
- Cluster ID: {cluster_id}
- Total Messages: {cluster_size} messages
- Unique Citizens: {unique_respondents} different people
- Average Mentions per Citizen: {avg_mentions}
- Respondent Coverage: {coverage_pct}% of all survey respondents
- Part of {total_clusters} total clusters

ATOMIC MESSAGES (focused civic concerns after preprocessing and splitting):
{examples_text}

Provide a comprehensive analysis of this cluster:

1. **Category**: Identify the high-level civic category (Infrastructure, Public Safety, Education, Healthcare, Housing, Transportation, Environment, Governance, Economic Development, Community Services, or Other)

2. **Theme**: Create a concise 2-4 word theme/label that captures the essence of this cluster

3. **Issues Summary**: Write 1 sentence describing the core issues or concerns people are expressing

4. **Detailed Analysis**: Write 2-3 paragraphs analyzing common concerns, patterns, underlying issues, and what citizens are experiencing. Focus on the problems, frustrations, or needs expressed.

5. **Key Topics**: List 5 key topics mentioned in this cluster

6. **Sentiment**: Identify overall sentiment (positive, negative, neutral, mixed, or concerned)

7. **Action Items** (MANDATORY): Provide at least 3 specific, actionable items
   - If citizens don't explicitly request actions, infer reasonable actions from their concerns
   - Example: Traffic complaints → ["Install traffic calming measures", "Increase traffic enforcement", "Add speed monitoring signs"]
   - Each action must be specific and actionable by local government or campaigns

8. **Civic Relevance**: Explain how this relates to local governance and community needs

9. **Confidence**: Rate your confidence (High, Medium, or Low) based on message coherence and theme clarity

Focus on:
- WHY citizens are contacting campaigns - what problems drive these messages
- What specific issues, frustrations, or concerns are expressed
- What citizens want done (MANDATORY action items)
- Direct citizen voices through clean verbatim quotes
- How this relates to local governance and community engagement"""

        return load_prompt_from_braintrust(
            prompt_name="cluster-analysis",
            fallback_prompt=fallback_prompt,
            variables=variables
        )

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

        logger.debug(f"Person-level metrics: {unique_respondents} unique respondents, "
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
        # verbatim_quotes will be populated by separate LLM call
        # Use theme as fallback if issues_summary is empty
        summary = response.issues_summary if response.issues_summary.strip() else response.theme

        cluster_theme = ClusterTheme(
            category=response.category,
            theme=response.theme,
            summary=summary,
            issues_summary=summary,
            detailed_analysis=response.detailed_analysis,
            verbatim_quotes=[],  # Will be populated by _generate_verbatim_quotes
            key_topics=response.key_topics,
            sentiment=response.sentiment.lower(),
            action_items=response.action_items,
            civic_relevance=response.civic_relevance,
            confidence_score=confidence_score
        )

        return cluster_theme

    async def _generate_verbatim_quotes(self, cluster_theme: ClusterTheme, cluster_id: int, sample_messages: List[ClusteredMessage]) -> ClusterTheme:
        """Generate verbatim quotes WITH phone attribution from original full messages"""

        messages_with_metadata = []
        for msg in sample_messages[:10]:
            phone = msg.metadata.get('Contact Phone Number', '') if hasattr(msg, 'metadata') else ''
            messages_with_metadata.append({
                'original_text': msg.original_text,
                'atomic_text': msg.text,
                'phone_number': phone,
                'message': msg
            })

        messages_text = "\n".join([f"{i+1}. {m['original_text']}" for i, m in enumerate(messages_with_metadata)])

        prompt = f"""Extract 3-5 verbatim quotes that best represent this cluster's theme.

CLUSTER THEME: {cluster_theme.theme}
CLUSTER CATEGORY: {cluster_theme.category}

ORIGINAL CITIZEN MESSAGES:
{messages_text}

INSTRUCTIONS:
- Extract SHORT snippets (MAXIMUM 15 words each) from the messages above
- Select quotes from DIFFERENT messages - do NOT extract multiple quotes from the same message
- Choose quotes that directly relate to the theme: "{cluster_theme.theme}"
- Extract ONLY the relevant portion about this theme if a message covers multiple topics
- Remove any dashes, quote marks, or numbering - just the clean text
- Use the actual words from the messages above
- Prioritize diversity - pick 1 quote per unique message to get 5 different perspectives

Example for "High Property Taxes" theme:
Message 1: "Time to vote everyone out. Ridiculous property taxes. No direction."
Message 2: "Can't afford these tax increases anymore."
Message 3: "Property taxes are killing families."
Extract 1 quote from each message: ["Ridiculous property taxes", "Can't afford these tax increases", "Property taxes are killing families"]"""

        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.llm_client.generate_structured_content(
                    prompt=prompt,
                    response_schema=VerbatimQuotesResponse,
                    system_instruction="Extract verbatim quotes from citizen messages that represent the cluster theme. Keep quotes SHORT (max 15 words) and directly related to the theme.",
                    trace_name="verbatim_quote_extraction"
                )
            )

            if not response or not response.quotes:
                logger.warning(f"Empty response for verbatim quotes (cluster {cluster_id})")
                return cluster_theme

            quotes_with_attribution = []
            seen_phone_numbers = set()
            for quote_text in response.quotes[:5]:
                matched_msg = self._match_quote_to_message(quote_text, messages_with_metadata)
                if matched_msg and matched_msg['phone_number'] not in seen_phone_numbers:
                    quotes_with_attribution.append({
                        'quote': matched_msg['original_text'],
                        'phone_number': matched_msg['phone_number']
                    })
                    seen_phone_numbers.add(matched_msg['phone_number'])

            cluster_theme.quotes = quotes_with_attribution
            cluster_theme.verbatim_quotes = [q['quote'] for q in quotes_with_attribution]
            logger.debug(f"Generated {len(cluster_theme.verbatim_quotes)} verbatim quotes with attribution for cluster {cluster_id}")

            return cluster_theme

        except Exception as e:
            logger.error(f"Failed to generate verbatim quotes for cluster {cluster_id}: {e}")
            return cluster_theme

    def _match_quote_to_message(self, quote: str, messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Match extracted quote back to source message using fuzzy matching"""
        from difflib import SequenceMatcher

        best_match = None
        best_score = 0.0

        quote_lower = quote.lower().strip()

        for msg in messages:
            original_lower = msg['original_text'].lower()

            if quote_lower in original_lower:
                return msg

            score = SequenceMatcher(None, quote_lower, original_lower).ratio()
            if score > best_score:
                best_score = score
                best_match = msg

        if best_score > 0.6:
            return best_match

        return messages[0] if messages else None

    async def _generate_action_items_with_llm(self, cluster_theme: ClusterTheme, sample_messages: List[ClusteredMessage], cluster_id: int) -> List[str]:
        """Generate action items using structured output"""

        message_texts = [msg.text for msg in sample_messages[:5]]
        messages_text = "\n".join([f"- {text}" for text in message_texts])

        prompt = f"""Generate 3-5 specific actionable items for local government or campaigns to address citizen concerns.

THEME: {cluster_theme.theme}
CATEGORY: {cluster_theme.category}
CITIZEN CONCERNS (atomic civic messages):
{messages_text}

REQUIREMENTS:
- Each action must be specific and concrete (not generic like "study the issue")
- Actions should directly address the concerns expressed in the messages
- Actions should be realistic for local government or campaign implementation
- If concerns are implicit (e.g., complaints about traffic), infer appropriate actions"""

        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.llm_client.generate_structured_content(
                    prompt=prompt,
                    response_schema=ActionItemsResponse,
                    system_instruction="Generate specific, actionable items that local government or campaigns can implement to address citizen concerns.",
                    trace_name="action_items_generation"
                )
            )

            if not response or not response.action_items:
                logger.warning(f"Empty response from LLM for action items generation (cluster {cluster_id})")
                return []

            logger.debug(f"Generated {len(response.action_items)} action items via structured output for cluster {cluster_id}")
            return response.action_items[:5]

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
                logger.debug(f"Successfully generated {len(generated_actions)} action items via LLM for cluster {cluster_id}")
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
        Dict with analyzed clusters, merger stats, and cost information
    """
    analyzer = MultiClusterAnalyzer(config)
    analysis_result = await analyzer.analyze_multi_cluster(multi_cluster_results)

    # Get usage statistics
    usage_stats = analyzer.get_usage_stats()
    if usage_stats:
        logger.info(f"Multi-Cluster LLM Usage - Calls: {usage_stats.get('api_call_count', 0)}, "
                   f"Tokens: {usage_stats.get('total_tokens', 0):,}, "
                   f"Cost: ${usage_stats.get('total_cost', 0):.4f}")

    return {
        "analyses": analysis_result['analyses'],
        "merger_stats": analysis_result['merger_stats'],
        "cost": usage_stats.get('total_cost', 0) if usage_stats else 0,
        "usage_stats": usage_stats
    }