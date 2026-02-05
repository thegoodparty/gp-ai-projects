import sys
from pathlib import Path
from typing import List, Dict
from collections import defaultdict
from pydantic import BaseModel, Field
from difflib import SequenceMatcher

sys.path.append(str(Path(__file__).parent.parent.parent.parent))
from shared.logger import get_logger
from shared.llm_gemini_3 import Gemini3Client, GeminiModelType, ThinkingLevel
from shared.llm_gemini import GeminiEmbeddingClient
from serve.analyze_texts.models import (
    ClassifiedMessage,
    CategorySummary,
    ClusterAnalysis,
    RefinedCategorySummary
)

logger = get_logger(__name__)


class ClusterAnalysisResponse(BaseModel):
    theme: str = Field(description="2-4 word theme for this cluster")
    summary: str = Field(description="2-3 sentence summary of cluster concerns")
    analysis: str = Field(description="Detailed analysis of what citizens are saying in this cluster")
    verbatim_quotes: List[str] = Field(description="3-5 direct quotes from cluster messages")
    dominant_sentiment: str = Field(description="Overall sentiment: positive, negative, neutral, or requesting")


class CategoryReanalysisResponse(BaseModel):
    refined_theme: str = Field(description="Refined 2-4 word theme based on cluster analysis")
    refined_summary: str = Field(description="Comprehensive 3-4 sentence summary incorporating all cluster insights")
    refined_analysis: str = Field(description="Deep analysis of what this category reveals about citizen concerns")
    key_quotes: List[str] = Field(description="5-7 most representative quotes across all clusters")


class HierarchicalReanalyzer:
    def __init__(self, min_messages_for_clustering: int = 10, clustering_config: dict = None, llm_config: dict = None):
        self.min_messages_for_clustering = min_messages_for_clustering
        self.clustering_config = clustering_config or {}
        llm_config = llm_config or {}

        self.llm_client = Gemini3Client(
            default_model=GeminiModelType.FLASH_3,
            default_temperature=llm_config.get("temperature", 0.0),
            thinking_level=ThinkingLevel.MINIMAL,
            max_connections=llm_config.get("max_workers", 50) * 2,
            max_keepalive_connections=llm_config.get("max_workers", 50) // 2
        )

        self.embedding_client = GeminiEmbeddingClient()

        logger.info(f"HierarchicalReanalyzer initialized (min clustering threshold: {min_messages_for_clustering})")

    def match_quotes_to_phones(self, quotes: List[str], messages: List[ClassifiedMessage]) -> List[Dict[str, str]]:
        attributed_quotes = []

        for quote in quotes:
            best_match = None
            best_ratio = 0.0

            for msg in messages:
                ratio = SequenceMatcher(None, quote.lower(), msg.message.message_text.lower()).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = msg

            if best_match and best_ratio > 0.6:
                attributed_quotes.append({
                    "quote": quote,
                    "phone_number": best_match.message.phone_number
                })
            else:
                attributed_quotes.append({
                    "quote": quote,
                    "phone_number": "Unknown"
                })

        return attributed_quotes

    def group_by_category(self, classified_messages: List[ClassifiedMessage]) -> Dict[tuple, List[ClassifiedMessage]]:
        categories = defaultdict(list)

        for msg in classified_messages:
            key = (msg.classification.primary_category, msg.classification.secondary_category)
            categories[key].append(msg)

        logger.info(f"Grouped messages into {len(categories)} categories for reanalysis")

        return dict(categories)

    def cluster_category_messages(self, messages: List[ClassifiedMessage]) -> Dict[int, List[ClassifiedMessage]]:
        texts = [msg.message.message_text for msg in messages]

        logger.info(f"Generating embeddings for {len(texts)} messages")
        embeddings = self.embedding_client.create_embeddings(
            texts,
            parallel=True,
            batch_size=self.clustering_config.get("batch_size", 100),
            max_concurrent_batches=self.clustering_config.get("max_concurrent_batches", 2)
        )

        logger.info(f"Clustering {len(texts)} messages...")
        import hdbscan

        min_cluster_size = max(3, self.clustering_config.get("min_cluster_size", 3))
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=min_cluster_size,
            min_samples=2,
            metric='euclidean'
        )

        cluster_labels = clusterer.fit_predict(embeddings)

        clusters = defaultdict(list)
        for idx, label in enumerate(cluster_labels):
            if label != -1:
                clusters[label].append(messages[idx])

        logger.info(f"Found {len(clusters)} clusters")

        return dict(clusters)

    def analyze_cluster(self, cluster_id: int, cluster_messages: List[ClassifiedMessage]) -> ClusterAnalysis:
        texts = [msg.message.message_text for msg in cluster_messages]
        sample_texts = texts[:10] if len(texts) > 10 else texts

        prompt = f"""Analyze this cluster of similar civic messages from citizens.

CLUSTER MESSAGES ({len(texts)} total, showing {len(sample_texts)}):
{chr(10).join([f"- {text}" for text in sample_texts])}

Provide a comprehensive analysis of this specific cluster, including:
- A clear theme that captures what this cluster is about
- A summary of the key concerns
- Detailed analysis of what citizens are expressing
- Direct quotes from the messages
- The dominant sentiment"""

        try:
            response: ClusterAnalysisResponse = self.llm_client.generate_structured_content(
                prompt=prompt,
                response_schema=ClusterAnalysisResponse
            )

            quotes_with_attribution = self.match_quotes_to_phones(response.verbatim_quotes, cluster_messages)

            return ClusterAnalysis(
                cluster_id=cluster_id,
                theme=response.theme,
                summary=response.summary,
                analysis=response.analysis,
                quotes=quotes_with_attribution,
                sentiment=response.dominant_sentiment,
                message_count=len(cluster_messages),
                example_messages=texts[:5]
            )

        except Exception as e:
            logger.error(f"Cluster {cluster_id} analysis failed: {e}")
            return ClusterAnalysis(
                cluster_id=cluster_id,
                theme="Analysis Failed",
                summary=f"Failed to analyze cluster of {len(cluster_messages)} messages",
                analysis="",
                quotes=[],
                sentiment="neutral",
                message_count=len(cluster_messages),
                example_messages=texts[:5]
            )

    def reanalyze_category(self, category_key: tuple, original_summary: CategorySummary, cluster_analyses: List[ClusterAnalysis], all_messages: List[ClassifiedMessage]) -> RefinedCategorySummary:
        primary, secondary = category_key

        cluster_summaries_text = "\n\n".join([
            f"Cluster {i+1} ({ca.message_count} messages):\n"
            f"Theme: {ca.theme}\n"
            f"Summary: {ca.summary}\n"
            f"Analysis: {ca.analysis}\n"
            f"Sentiment: {ca.sentiment}"
            for i, ca in enumerate(cluster_analyses)
        ])

        prompt = f"""Re-analyze this category based on bottom-up cluster analysis.

CATEGORY: {primary} / {secondary}
TOTAL MESSAGES: {len(all_messages)}

CLUSTER-LEVEL ANALYSES:
{cluster_summaries_text}

Based on these cluster-level insights, provide a refined understanding of this category:
- What is the overarching theme when you look at all clusters together?
- What is the comprehensive summary that incorporates all cluster insights?
- What deep analysis emerges from understanding these sub-themes?
- What are the most important quotes across all clusters?

Your analysis should synthesize the cluster-level insights into a coherent category-level understanding."""

        try:
            response: CategoryReanalysisResponse = self.llm_client.generate_structured_content(
                prompt=prompt,
                response_schema=CategoryReanalysisResponse
            )

            quotes_with_attribution = self.match_quotes_to_phones(response.key_quotes, all_messages)

            stance_counts = defaultdict(int)
            for msg in all_messages:
                stance_counts[msg.classification.stance] += 1

            unique_phones = len(set(msg.message.phone_number for msg in all_messages))

            return RefinedCategorySummary(
                primary_category=primary,
                secondary_category=secondary,
                refined_theme=response.refined_theme,
                refined_summary=response.refined_summary,
                refined_analysis=response.refined_analysis,
                refined_quotes=quotes_with_attribution,
                cluster_analyses=cluster_analyses,
                message_count=len(all_messages),
                unique_respondents=unique_phones,
                sentiment_distribution=dict(stance_counts)
            )

        except Exception as e:
            logger.error(f"Category reanalysis failed for {primary}/{secondary}: {e}")
            return self._create_fallback_refined_summary(category_key, original_summary, cluster_analyses, all_messages)

    def _create_fallback_refined_summary(self, category_key: tuple, original_summary: CategorySummary, cluster_analyses: List[ClusterAnalysis], all_messages: List[ClassifiedMessage]) -> RefinedCategorySummary:
        primary, secondary = category_key

        stance_counts = defaultdict(int)
        for msg in all_messages:
            stance_counts[msg.classification.stance] += 1

        unique_phones = len(set(msg.message.phone_number for msg in all_messages))

        all_quotes = []
        for ca in cluster_analyses:
            all_quotes.extend(ca.quotes)

        return RefinedCategorySummary(
            primary_category=primary,
            secondary_category=secondary,
            refined_theme=', '.join([ca.theme for ca in cluster_analyses[:3]]),
            refined_summary=original_summary.summary,
            refined_analysis="Category reanalysis failed, using original analysis",
            refined_quotes=all_quotes[:7],
            cluster_analyses=cluster_analyses,
            message_count=len(all_messages),
            unique_respondents=unique_phones,
            sentiment_distribution=dict(stance_counts)
        )

    def reanalyze_all_categories(self, classified_messages: List[ClassifiedMessage], original_summaries: List[CategorySummary]) -> List[RefinedCategorySummary]:
        logger.info("Starting hierarchical re-analysis of all categories...")

        categories = self.group_by_category(classified_messages)

        original_summary_map = {
            (s.primary_category, s.secondary_category): s
            for s in original_summaries
        }

        refined_summaries = []

        for category_key, messages in categories.items():
            primary, secondary = category_key
            original_summary = original_summary_map.get(category_key)

            if not original_summary:
                logger.warning(f"No original summary found for {primary}/{secondary}, skipping")
                continue

            logger.info(f"Re-analyzing {primary}/{secondary} ({len(messages)} messages)")

            if len(messages) < self.min_messages_for_clustering:
                logger.info(f"Category too small for clustering ({len(messages)} < {self.min_messages_for_clustering}), creating single-cluster analysis")

                cluster_analysis = self.analyze_cluster(0, messages)

                stance_counts = defaultdict(int)
                for msg in messages:
                    stance_counts[msg.classification.stance] += 1

                refined_summary = RefinedCategorySummary(
                    primary_category=primary,
                    secondary_category=secondary,
                    refined_theme=cluster_analysis.theme,
                    refined_summary=cluster_analysis.summary,
                    refined_analysis=cluster_analysis.analysis,
                    refined_quotes=cluster_analysis.quotes,
                    cluster_analyses=[cluster_analysis],
                    message_count=len(messages),
                    unique_respondents=len(set(msg.message.phone_number for msg in messages)),
                    sentiment_distribution=dict(stance_counts)
                )

            else:
                clusters = self.cluster_category_messages(messages)

                cluster_analyses = []
                for cluster_id, cluster_messages in clusters.items():
                    if len(cluster_messages) >= 3:
                        cluster_analysis = self.analyze_cluster(cluster_id, cluster_messages)
                        cluster_analyses.append(cluster_analysis)

                if not cluster_analyses:
                    logger.warning(f"No valid clusters found for {primary}/{secondary}, using original summary")
                    cluster_analysis = self.analyze_cluster(0, messages)
                    cluster_analyses = [cluster_analysis]

                refined_summary = self.reanalyze_category(category_key, original_summary, cluster_analyses, messages)

            refined_summaries.append(refined_summary)
            logger.info(f"Completed re-analysis of {primary}/{secondary} - Theme: {refined_summary.refined_theme}")

        refined_summaries.sort(key=lambda x: x.message_count, reverse=True)

        logger.info(f"Completed hierarchical re-analysis of {len(refined_summaries)} categories")

        return refined_summaries

    def get_usage_stats(self):
        llm_stats = self.llm_client.get_usage_stats() if hasattr(self.llm_client, 'get_usage_stats') else {}
        embed_stats = self.embedding_client.get_cost_stats() if hasattr(self.embedding_client, 'get_cost_stats') else {}

        return {
            "llm": llm_stats,
            "embedding": embed_stats
        }


def hierarchical_reanalysis_stage(classified_messages: List[ClassifiedMessage], original_summaries: List[CategorySummary], config: dict) -> List[RefinedCategorySummary]:
    logger.info("=== STAGE 6: HIERARCHICAL RE-ANALYSIS ===")

    reanalysis_config = config.get("hierarchical_reanalysis", {})

    if not reanalysis_config.get("enabled", True):
        logger.info("Hierarchical re-analysis disabled, skipping")
        return []

    reanalyzer = HierarchicalReanalyzer(
        min_messages_for_clustering=reanalysis_config.get("min_messages_for_clustering", 10),
        clustering_config=reanalysis_config.get("clustering", {}),
        llm_config=reanalysis_config.get("llm_config", {})
    )

    refined_summaries = reanalyzer.reanalyze_all_categories(classified_messages, original_summaries)

    usage_stats = reanalyzer.get_usage_stats()
    if usage_stats.get("llm"):
        logger.info(f"Re-analysis LLM Usage - Calls: {usage_stats['llm'].get('api_call_count', 0)}, "
                   f"Cost: ${usage_stats['llm'].get('total_cost', 0):.4f}")
    if usage_stats.get("embedding"):
        logger.info(f"Embedding Usage - Count: {usage_stats['embedding'].get('total_embeddings_created', 0)}, "
                   f"Cost: ${usage_stats['embedding'].get('total_cost', 0):.4f}")

    return refined_summaries
