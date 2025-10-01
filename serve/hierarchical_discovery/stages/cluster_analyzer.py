#!/usr/bin/env python3

import asyncio
import uuid
import random
from typing import List, Dict, Any, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict

from shared.llm_gemini import GeminiClient, GeminiModelType
from shared.logger import get_logger
from ..models import ClusteredMessage, ClusterAnalysis, ClusterTheme, PipelineConfig

logger = get_logger(__name__)

class ParallelClusterAnalyzer:
    """High-throughput parallel cluster analysis using Gemini"""

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.analysis_config = config.analysis

        # High-throughput LLM configuration
        max_workers = self.analysis_config.get('max_workers', 50)
        llm_config = self.analysis_config.get('llm_config', {})

        target_concurrency = min(max_workers * 2, llm_config.get('max_connections', 100))

        self.llm_client = GeminiClient(
            default_model=GeminiModelType.FLASH,
            default_temperature=llm_config.get('temperature', 0.0),
            thinking_budget=llm_config.get('thinking_budget', 0),  # Cost efficiency
            max_connections=target_concurrency,
            max_keepalive_connections=llm_config.get('max_keepalive_connections', target_concurrency // 4)
        )

        # ThreadPoolExecutor for async operations
        self.thread_pool = ThreadPoolExecutor(max_workers=max_workers)
        self.max_workers = max_workers

        logger.info(f"ParallelClusterAnalyzer initialized with {target_concurrency} connections, {max_workers} workers")

    def group_messages_by_cluster(self, clustered_messages: List[ClusteredMessage]) -> Dict[int, List[ClusteredMessage]]:
        """Group messages by cluster ID"""
        clusters = defaultdict(list)

        for message in clustered_messages:
            if not message.cluster_assignment.is_noise:  # Skip noise points
                cluster_id = message.cluster_assignment.cluster_id
                clusters[cluster_id].append(message)

        # Convert to regular dict and sort by cluster size
        clusters_dict = dict(clusters)
        sorted_clusters = {
            cluster_id: messages
            for cluster_id, messages in sorted(clusters_dict.items(), key=lambda x: len(x[1]), reverse=True)
        }

        logger.info(f"Grouped messages into {len(sorted_clusters)} clusters")
        return sorted_clusters

    def prepare_cluster_for_analysis(self, cluster_id: int, messages: List[ClusteredMessage]) -> Dict[str, Any]:
        """Prepare cluster data for analysis"""
        max_examples = self.analysis_config.get('max_example_messages', 50)
        save_examples = self.analysis_config.get('save_example_messages', 5)

        # Get representative messages (up to max_examples) using random sampling for better representation
        if len(messages) > max_examples:
            # Randomly sample messages for better cluster representation
            sample_messages = random.sample(messages, max_examples)
        else:
            # Use all messages if we have max_examples or fewer
            sample_messages = messages

        # Extract texts and metadata
        sample_texts = [msg.text for msg in sample_messages]

        # Get example messages closest to centroid for better representation
        example_texts = self._get_centroid_examples(messages, save_examples)

        # Campaign distribution
        campaign_counts = defaultdict(int)
        for msg in messages:
            campaign_counts[msg.campaign_source] += 1

        # Message IDs for traceability
        message_ids = [msg.id for msg in messages]

        return {
            'cluster_id': cluster_id,
            'size': len(messages),
            'sample_texts': sample_texts,
            'example_texts': example_texts,
            'message_ids': message_ids,
            'campaign_distribution': dict(campaign_counts),
            'avg_confidence': sum(msg.cluster_assignment.cluster_confidence for msg in messages) / len(messages)
        }

    def _get_centroid_examples(self, messages: List[ClusteredMessage], num_examples: int) -> List[str]:
        """Get example messages closest to cluster centroid for better representation"""
        import numpy as np
        from sklearn.metrics.pairwise import cosine_similarity

        if len(messages) <= num_examples:
            return [msg.text for msg in messages]

        try:
            # Extract embeddings from messages
            embeddings = []
            valid_messages = []

            for msg in messages:
                if hasattr(msg, 'embedding') and msg.embedding is not None:
                    embeddings.append(msg.embedding)
                    valid_messages.append(msg)

            if len(embeddings) < num_examples:
                # Fallback to first N messages if embeddings not available
                return [msg.text for msg in messages[:num_examples]]

            # Convert to numpy array
            embeddings_array = np.array(embeddings)

            # Calculate centroid
            centroid = np.mean(embeddings_array, axis=0)

            # Calculate distances to centroid
            similarities = cosine_similarity([centroid], embeddings_array)[0]

            # Get indices of messages closest to centroid
            closest_indices = np.argsort(similarities)[-num_examples:]

            # Return texts of most representative messages
            return [valid_messages[i].text for i in closest_indices]

        except Exception as e:
            logger.warning(f"Failed to compute centroid examples: {e}, falling back to first {num_examples}")
            return [msg.text for msg in messages[:num_examples]]

    async def analyze_cluster_async(self, cluster_data: Dict[str, Any]) -> ClusterAnalysis:
        """Analyze a single cluster asynchronously"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.thread_pool,
            self.analyze_cluster_with_gemini,
            cluster_data
        )

    def analyze_cluster_with_gemini(self, cluster_data: Dict[str, Any]) -> ClusterAnalysis:
        """Analyze a cluster using Gemini AI"""
        cluster_id = cluster_data['cluster_id']
        size = cluster_data['size']
        sample_texts = cluster_data['sample_texts']

        if not sample_texts:
            return self._create_empty_cluster_analysis(cluster_id, size)

        # Create analysis prompt
        prompt = self._create_analysis_prompt(cluster_data)

        try:
            # Get analysis from Gemini
            response = self.llm_client.generate_content(prompt)

            # Parse the response
            theme_analysis = self._parse_gemini_response(response)

            # Create cluster analysis object
            cluster_analysis = ClusterAnalysis(
                cluster_id=cluster_id,
                size=size,
                theme_analysis=theme_analysis,
                example_messages=cluster_data['example_texts'],
                message_ids=cluster_data['message_ids'][:100],  # Limit for storage efficiency
                analysis_model="gemini-flash",
                analysis_timestamp=datetime.now(),
                cost_estimate=0.0  # Will be updated by usage tracking
            )

            return cluster_analysis

        except Exception as e:
            logger.error(f"Failed to analyze cluster {cluster_id}: {e}")
            return self._create_error_cluster_analysis(cluster_id, size, str(e))

    def _create_analysis_prompt(self, cluster_data: Dict[str, Any]) -> str:
        """Create a detailed analysis prompt for Gemini"""
        cluster_id = cluster_data['cluster_id']
        size = cluster_data['size']
        sample_texts = cluster_data['sample_texts']
        campaign_dist = cluster_data['campaign_distribution']

        # Format sample messages
        sample_list = '\n'.join([f"{i+1}. {text}" for i, text in enumerate(sample_texts)])

        # Format campaign distribution
        campaign_info = ', '.join([f"{campaign}: {count}" for campaign, count in campaign_dist.items()])

        prompt = f"""Analyze this cluster of civic engagement messages from political campaigns.

CLUSTER {cluster_id} DETAILS:
- Total Messages: {size}
- Campaign Sources: {campaign_info}
- Average Confidence: {cluster_data.get('avg_confidence', 0.0):.3f}

REPRESENTATIVE MESSAGES ({len(sample_texts)} of {size}):
{sample_list}

Please provide a comprehensive analysis in this EXACT format:

CATEGORY: [High-level civic category: Infrastructure/Public Safety/Education/Healthcare/Housing/Transportation/Environment/Governance/Economic Development/Community Services/Other]
THEME: [2-4 word concise theme/label]
ISSUES_SUMMARY: [1 sentence describing the core issues or concerns people are expressing]
DETAILED_ANALYSIS: [2-3 paragraphs analyzing common concerns, patterns, underlying issues, and what citizens are experiencing. Focus on the problems, frustrations, or needs expressed.]
VERBATIM_QUOTES: [3-5 most representative direct quotes from the messages that capture the essence of the concerns. Use exact text from the messages above.]
KEY_TOPICS: [topic1, topic2, topic3, topic4, topic5]
SENTIMENT: [positive/negative/neutral/mixed/concerned]
ACTION_ITEMS: [Specific actions, changes, or solutions citizens are requesting or would address their concerns]
CIVIC_RELEVANCE: [How this relates to local governance and community needs - be specific about the civic domain]
CONFIDENCE: [High/Medium/Low - based on message coherence and theme clarity]

Focus on:
1. WHY citizens are contacting campaigns - what problems or needs drive these messages
2. What specific issues, frustrations, or concerns are expressed
3. What citizens want done about these issues (action items)
4. Direct citizen voices through verbatim quotes
5. How this cluster relates to local governance and community engagement

Be detailed and insightful. Preserve authentic citizen voices in quotes."""

        return prompt

    def _parse_gemini_response(self, response: str) -> ClusterTheme:
        """Parse Gemini's response into a ClusterTheme object"""
        lines = response.strip().split('\n')

        # Default values
        category = "Other"
        theme = "Unspecified"
        issues_summary = "Analysis unavailable"
        summary = "Analysis unavailable"  # Keep for backward compatibility
        detailed_analysis = ""
        verbatim_quotes = []
        key_topics = []
        sentiment = "neutral"
        action_items = []
        civic_relevance = "General civic engagement"
        confidence_score = 0.5

        # Parse response line by line, handling multi-line fields
        i = 0
        while i < len(lines):
            line = lines[i].strip()

            if line.startswith('CATEGORY:'):
                category = line.replace('CATEGORY:', '').strip()
            elif line.startswith('THEME:'):
                theme = line.replace('THEME:', '').strip()
            elif line.startswith('ISSUES_SUMMARY:'):
                issues_summary = line.replace('ISSUES_SUMMARY:', '').strip()
                # Use issues_summary as summary for backward compatibility
                summary = issues_summary
            elif line.startswith('DETAILED_ANALYSIS:'):
                # Simplified multi-line detailed analysis parsing
                analysis_parts = [line.replace('DETAILED_ANALYSIS:', '').strip()]
                # Look ahead for continuation lines
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
                # Simplified multi-line quotes parsing
                quotes_parts = [line.replace('VERBATIM_QUOTES:', '').strip()]
                # Look ahead for continuation lines
                j = i + 1
                while j < len(lines):
                    next_line = lines[j].strip()
                    if not next_line or any(next_line.startswith(prefix) for prefix in
                        ['KEY_TOPICS:', 'SENTIMENT:', 'ACTION_ITEMS:', 'CIVIC_RELEVANCE:', 'CONFIDENCE:']):
                        break
                    quotes_parts.append(next_line)
                    j += 1
                # Parse quotes more robustly
                quotes_text = ' '.join([p for p in quotes_parts if p]).strip()
                if quotes_text:
                    # Simple approach: split on common patterns and clean up
                    import re
                    # Split by line breaks first, then by numbered patterns
                    potential_quotes = []
                    for part in quotes_text.split('\n'):
                        # Try splitting by numbered patterns like "1.", "2.", etc.
                        subparts = re.split(r'\d+\.\s*', part)
                        potential_quotes.extend(subparts)
                    # Clean and filter quotes
                    verbatim_quotes = []
                    for q in potential_quotes:
                        cleaned = q.strip(' "\'').strip()
                        if cleaned and len(cleaned) > 5:  # More lenient length check
                            verbatim_quotes.append(cleaned)
                    # Limit to reasonable number of quotes
                    verbatim_quotes = verbatim_quotes[:5]
            elif line.startswith('KEY_TOPICS:'):
                topics_str = line.replace('KEY_TOPICS:', '').strip()
                # Parse comma-separated topics
                key_topics = [topic.strip() for topic in topics_str.split(',') if topic.strip()]
            elif line.startswith('SENTIMENT:'):
                sentiment = line.replace('SENTIMENT:', '').strip().lower()
            elif line.startswith('ACTION_ITEMS:'):
                actions_str = line.replace('ACTION_ITEMS:', '').strip()
                # Parse comma-separated or line-separated action items
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

    def _create_empty_cluster_analysis(self, cluster_id: int, size: int) -> ClusterAnalysis:
        """Create analysis for empty cluster"""
        empty_theme = ClusterTheme(
            category="Other",
            theme="Empty Cluster",
            summary="No messages to analyze",
            issues_summary="No messages to analyze",
            detailed_analysis="",
            verbatim_quotes=[],
            key_topics=[],
            sentiment="neutral",
            action_items=[],
            civic_relevance="N/A",
            confidence_score=0.0
        )

        return ClusterAnalysis(
            cluster_id=cluster_id,
            size=size,
            theme_analysis=empty_theme,
            example_messages=[],
            message_ids=[],
            analysis_model="gemini-flash",
            analysis_timestamp=datetime.now(),
            cost_estimate=0.0
        )

    def _create_error_cluster_analysis(self, cluster_id: int, size: int, error_msg: str) -> ClusterAnalysis:
        """Create analysis for failed cluster"""
        error_theme = ClusterTheme(
            category="Other",
            theme="Analysis Failed",
            summary=f"Analysis failed: {error_msg}",
            issues_summary=f"Analysis failed: {error_msg}",
            detailed_analysis="",
            verbatim_quotes=[],
            key_topics=[],
            sentiment="neutral",
            action_items=[],
            civic_relevance="Analysis unavailable",
            confidence_score=0.0
        )

        return ClusterAnalysis(
            cluster_id=cluster_id,
            size=size,
            theme_analysis=error_theme,
            example_messages=[],
            message_ids=[],
            analysis_model="gemini-flash",
            analysis_timestamp=datetime.now(),
            cost_estimate=0.0
        )

    async def analyze_all_clusters_parallel(self, clustered_messages: List[ClusteredMessage]) -> List[ClusterAnalysis]:
        """Analyze all clusters in parallel"""
        if not clustered_messages:
            return []

        # Group messages by cluster
        clusters = self.group_messages_by_cluster(clustered_messages)

        if not clusters:
            logger.warning("No clusters found for analysis")
            return []

        # Prepare cluster data
        cluster_data_list = []
        for cluster_id, messages in clusters.items():
            cluster_data = self.prepare_cluster_for_analysis(cluster_id, messages)
            cluster_data_list.append(cluster_data)

        # Create analysis tasks
        logger.info(f"Creating analysis tasks for {len(cluster_data_list)} clusters...")
        tasks = [self.analyze_cluster_async(cluster_data) for cluster_data in cluster_data_list]

        # Execute all analyses in parallel
        logger.info(f"🚀 Executing {len(tasks)} cluster analyses in parallel...")
        start_time = asyncio.get_event_loop().time()

        cluster_analyses = await asyncio.gather(*tasks, return_exceptions=True)

        duration = asyncio.get_event_loop().time() - start_time
        logger.info(f"✅ Completed all analyses in {duration:.2f} seconds")

        # Handle exceptions and create final list
        successful_analyses = []
        for i, result in enumerate(cluster_analyses):
            if isinstance(result, Exception):
                cluster_id = cluster_data_list[i]['cluster_id']
                size = cluster_data_list[i]['size']
                logger.error(f"Analysis failed for cluster {cluster_id}: {result}")
                error_analysis = self._create_error_cluster_analysis(cluster_id, size, str(result))
                successful_analyses.append(error_analysis)
            else:
                successful_analyses.append(result)

        # Sort by cluster size (descending)
        successful_analyses.sort(key=lambda x: x.size, reverse=True)

        return successful_analyses

    def analyze_clusters_sync(self, clustered_messages: List[ClusteredMessage]) -> List[ClusterAnalysis]:
        """Synchronous wrapper for cluster analysis"""
        if not self.analysis_config.get('enabled', True):
            logger.info("Cluster analysis is disabled")
            return []

        if not self.analysis_config.get('parallel_analysis', True):
            logger.info("Parallel analysis is disabled, falling back to sequential")
            return self._analyze_clusters_sequential(clustered_messages)

        try:
            loop = asyncio.get_running_loop()
            # Already in event loop, run in separate thread
            import concurrent.futures

            def run_in_new_loop():
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    return new_loop.run_until_complete(self.analyze_all_clusters_parallel(clustered_messages))
                finally:
                    new_loop.close()

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_in_new_loop)
                return future.result()

        except RuntimeError:
            # No event loop running
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(self.analyze_all_clusters_parallel(clustered_messages))
            finally:
                loop.close()

    def _analyze_clusters_sequential(self, clustered_messages: List[ClusteredMessage]) -> List[ClusterAnalysis]:
        """Sequential cluster analysis fallback"""
        clusters = self.group_messages_by_cluster(clustered_messages)
        analyses = []

        for cluster_id, messages in clusters.items():
            cluster_data = self.prepare_cluster_for_analysis(cluster_id, messages)
            analysis = self.analyze_cluster_with_gemini(cluster_data)
            analyses.append(analysis)

        return sorted(analyses, key=lambda x: x.size, reverse=True)

    def generate_analysis_report(self, cluster_analyses: List[ClusterAnalysis]) -> str:
        """Generate comprehensive analysis report"""
        if not cluster_analyses:
            return "Cluster Analysis Report: No clusters analyzed"

        total_clusters = len(cluster_analyses)
        total_messages = sum(analysis.size for analysis in cluster_analyses)

        # Theme distribution
        themes = [analysis.theme_analysis.theme for analysis in cluster_analyses]
        theme_counts = {}
        for theme in themes:
            theme_counts[theme] = theme_counts.get(theme, 0) + 1

        # Sentiment distribution
        sentiments = [analysis.theme_analysis.sentiment for analysis in cluster_analyses]
        sentiment_counts = {}
        for sentiment in sentiments:
            sentiment_counts[sentiment] = sentiment_counts.get(sentiment, 0) + 1

        # Confidence distribution
        confidence_scores = [analysis.theme_analysis.confidence_score for analysis in cluster_analyses]
        avg_confidence = sum(confidence_scores) / len(confidence_scores)

        report_lines = [
            "Parallel Cluster Analysis Report:",
            f"  Total Clusters Analyzed: {total_clusters}",
            f"  Total Messages Analyzed: {total_messages:,}",
            f"  Average Confidence Score: {avg_confidence:.3f}",
            "",
            "Top 10 Clusters by Size:"
        ]

        for i, analysis in enumerate(cluster_analyses[:10]):
            theme = analysis.theme_analysis.theme
            sentiment = analysis.theme_analysis.sentiment
            confidence = analysis.theme_analysis.confidence_score
            report_lines.append(f"  {i+1:2d}. Cluster {analysis.cluster_id}: {analysis.size:3d} msgs - {theme} ({sentiment}, {confidence:.2f})")

        report_lines.extend([
            "",
            "Sentiment Distribution:"
        ])

        for sentiment, count in sorted(sentiment_counts.items(), key=lambda x: x[1], reverse=True):
            percentage = (count / total_clusters) * 100
            report_lines.append(f"  {sentiment}: {count} ({percentage:.1f}%)")

        report_lines.extend([
            "",
            "Key Civic Topics Identified:"
        ])

        # Aggregate all key topics
        all_topics = []
        for analysis in cluster_analyses:
            all_topics.extend(analysis.theme_analysis.key_topics)

        # Count topic frequency
        topic_counts = {}
        for topic in all_topics:
            topic_counts[topic] = topic_counts.get(topic, 0) + 1

        # Show top topics
        for topic, count in sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)[:10]:
            report_lines.append(f"  {topic}: mentioned in {count} clusters")

        return "\n".join(report_lines)

    def analyze_sub_clusters(self, clustered_messages: List[ClusteredMessage],
                           main_cluster_analyses: List[ClusterAnalysis]) -> List[ClusterAnalysis]:
        """Analyze sub-clusters and integrate into main cluster analyses"""
        from ..models import SubClusterAnalysis

        # Group sub-clustered messages by parent cluster
        parent_clusters = defaultdict(list)
        sub_clusters = defaultdict(list)

        for msg in clustered_messages:
            cluster_id = msg.cluster_assignment.cluster_id
            if cluster_id >= 1000:  # Sub-cluster ID format: parent_id * 1000 + sub_id
                parent_id = cluster_id // 1000
                parent_clusters[parent_id].append(msg)
                sub_clusters[cluster_id].append(msg)

        if not sub_clusters:
            logger.info("No sub-clusters found for analysis")
            return main_cluster_analyses

        logger.info(f"Analyzing {len(sub_clusters)} sub-clusters across {len(parent_clusters)} parent clusters")

        # Analyze each sub-cluster
        sub_cluster_analyses = {}
        for sub_cluster_id, sub_messages in sub_clusters.items():
            if len(sub_messages) < 3:  # Skip very small sub-clusters
                continue

            parent_id = sub_cluster_id // 1000
            sub_id = sub_cluster_id % 1000

            # Find parent cluster theme for context
            parent_theme = "Unknown"
            for analysis in main_cluster_analyses:
                if analysis.cluster_id == parent_id:
                    parent_theme = analysis.theme_analysis.theme
                    break

            # Prepare sub-cluster data
            sub_cluster_data = self.prepare_cluster_for_analysis(sub_cluster_id, sub_messages)
            sub_cluster_data['parent_theme'] = parent_theme
            sub_cluster_data['parent_id'] = parent_id

            # Analyze sub-cluster
            try:
                theme_analysis = self._analyze_sub_cluster_with_gemini(sub_cluster_data)

                sub_analysis = SubClusterAnalysis(
                    sub_cluster_id=sub_cluster_id,
                    parent_cluster_id=parent_id,
                    size=len(sub_messages),
                    theme_analysis=theme_analysis,
                    example_messages=sub_cluster_data['example_texts'],
                    message_ids=sub_cluster_data['message_ids'][:50],
                    analysis_model="gemini-flash",
                    analysis_timestamp=datetime.now(),
                    cost_estimate=0.0
                )

                sub_cluster_analyses[sub_cluster_id] = sub_analysis
                logger.info(f"Sub-cluster {sub_cluster_id}: {theme_analysis.theme} ({len(sub_messages)} messages)")

            except Exception as e:
                logger.error(f"Failed to analyze sub-cluster {sub_cluster_id}: {e}")

        # Integrate sub-cluster analyses into main cluster analyses
        updated_analyses = []
        for main_analysis in main_cluster_analyses:
            cluster_id = main_analysis.cluster_id

            # Find sub-clusters for this main cluster
            cluster_sub_analyses = [
                sub_analysis for sub_analysis in sub_cluster_analyses.values()
                if sub_analysis.parent_cluster_id == cluster_id
            ]

            if cluster_sub_analyses:
                # Sort sub-clusters by size
                cluster_sub_analyses.sort(key=lambda x: x.size, reverse=True)
                main_analysis.sub_clusters = cluster_sub_analyses
                main_analysis.has_sub_clusters = True
                logger.info(f"Cluster {cluster_id}: Added {len(cluster_sub_analyses)} sub-clusters")

            updated_analyses.append(main_analysis)

        return updated_analyses

    def _analyze_sub_cluster_with_gemini(self, sub_cluster_data: Dict[str, Any]) -> ClusterTheme:
        """Analyze a sub-cluster using focused prompts"""
        prompt = self._create_sub_cluster_analysis_prompt(sub_cluster_data)

        response = self.llm_client.generate_content(prompt)
        return self._parse_gemini_response(response)

    def _create_sub_cluster_analysis_prompt(self, sub_cluster_data: Dict[str, Any]) -> str:
        """Create focused analysis prompt for sub-clusters"""
        sub_cluster_id = sub_cluster_data['cluster_id']
        parent_id = sub_cluster_data['parent_id']
        parent_theme = sub_cluster_data['parent_theme']
        size = sub_cluster_data['size']
        sample_texts = sub_cluster_data['sample_texts']
        campaign_dist = sub_cluster_data['campaign_distribution']

        sample_list = '\n'.join([f"{i+1}. {text}" for i, text in enumerate(sample_texts)])
        campaign_info = ', '.join([f"{campaign}: {count}" for campaign, count in campaign_dist.items()])

        prompt = f"""Analyze this SUB-CLUSTER of civic engagement messages that are part of a larger theme.

CONTEXT:
- Parent Cluster {parent_id}: "{parent_theme}"
- Sub-cluster {sub_cluster_id}: {size} messages within the "{parent_theme}" theme
- Campaign Sources: {campaign_info}

This sub-cluster represents a more specific aspect within the broader "{parent_theme}" category.

REPRESENTATIVE MESSAGES ({len(sample_texts)} of {size}):
{sample_list}

Please provide a focused analysis in this EXACT format:

THEME: [2-3 word specific sub-theme within "{parent_theme}"]
SUMMARY: [1 sentence describing this specific aspect of "{parent_theme}"]
KEY_TOPICS: [topic1, topic2, topic3]
SENTIMENT: [positive/negative/neutral/mixed/concerned]
CIVIC_RELEVANCE: [How this specific sub-aspect relates to "{parent_theme}" and civic engagement]
CONFIDENCE: [High/Medium/Low - based on message coherence and sub-theme clarity]

Focus on:
1. What makes this sub-group distinct within "{parent_theme}"
2. The specific concerns or aspects that differentiate it from other parts of "{parent_theme}"
3. Concrete, actionable civic issues within this narrower focus

Be precise and specific - avoid repeating the parent theme."""

        return prompt

    def get_usage_stats(self) -> Dict[str, Any]:
        """Get LLM usage statistics"""
        if hasattr(self.llm_client, 'get_usage_stats'):
            return self.llm_client.get_usage_stats()
        return {}

def cluster_analyzer_stage(clustered_messages: List[ClusteredMessage], config: PipelineConfig,
                         sub_clustered_messages: Optional[List[ClusteredMessage]] = None) -> List[ClusterAnalysis]:
    """Main entry point for cluster analysis stage with optional sub-cluster analysis"""
    logger.info("=== CLUSTER ANALYSIS STAGE ===")

    if not clustered_messages:
        logger.warning("No clustered messages to analyze")
        return []

    try:
        analyzer = ParallelClusterAnalyzer(config)

        # Analyze main clusters (filter out sub-cluster messages)
        main_cluster_messages = [
            msg for msg in clustered_messages
            if msg.cluster_assignment.cluster_id < 1000 or msg.cluster_assignment.is_noise
        ]

        # Analyze main clusters
        cluster_analyses = analyzer.analyze_clusters_sync(main_cluster_messages)

        # If sub-clustered messages are provided and sub-clustering is enabled, analyze sub-clusters
        if (sub_clustered_messages and
            getattr(config, 'sub_clustering', {}).get('enabled', False)):

            logger.info("=== SUB-CLUSTER ANALYSIS ===")
            cluster_analyses = analyzer.analyze_sub_clusters(sub_clustered_messages, cluster_analyses)

        # Generate and log report
        report = analyzer.generate_analysis_report(cluster_analyses)
        logger.info(f"\n{report}")

        # Log hierarchical structure if sub-clusters exist
        total_sub_clusters = sum(len(analysis.sub_clusters) for analysis in cluster_analyses)
        if total_sub_clusters > 0:
            logger.info(f"Hierarchical Structure: {len(cluster_analyses)} main clusters with {total_sub_clusters} sub-clusters")
            for analysis in cluster_analyses:
                if analysis.has_sub_clusters:
                    logger.info(f"  Cluster {analysis.cluster_id} ({analysis.theme_analysis.theme}): {len(analysis.sub_clusters)} sub-clusters")

        # Log usage statistics
        usage_stats = analyzer.get_usage_stats()
        if usage_stats:
            logger.info(f"LLM Usage - Calls: {usage_stats.get('api_call_count', 0)}, "
                       f"Tokens: {usage_stats.get('total_tokens', 0):,}, "
                       f"Cost: ${usage_stats.get('total_cost', 0):.4f}")

        # Return results with cost information
        return {
            "analyses": cluster_analyses,
            "cost": usage_stats.get('total_cost', 0) if usage_stats else 0,
            "usage_stats": usage_stats or {}
        }

    except Exception as e:
        logger.error(f"Cluster analysis failed: {e}")
        raise