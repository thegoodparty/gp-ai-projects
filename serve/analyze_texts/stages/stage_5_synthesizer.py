import sys
from pathlib import Path
from typing import List, Dict
from collections import defaultdict
import numpy as np
from difflib import SequenceMatcher
from pydantic import BaseModel, Field

sys.path.append(str(Path(__file__).parent.parent.parent.parent))
from shared.logger import get_logger
from shared.llm_gemini import GeminiClient, GeminiEmbeddingClient, GeminiModelType
from serve.analyze_texts.models import ClassifiedMessage, CategorySummary, ClusterInfo

logger = get_logger(__name__)


class AISummaryResponse(BaseModel):
    summary: str = Field(description="2-3 sentences summarizing the key concerns and patterns")
    key_themes: List[str] = Field(description="List of 3-5 key themes (clean strings, no brackets or markdown)")
    verbatim_quotes: List[str] = Field(description="List of 3-5 direct quotes that capture the essence")
    action_items: List[str] = Field(description="List of specific actions or solutions citizens are requesting")


class ClusterThemeResponse(BaseModel):
    theme: str = Field(description="A 2-word theme label for this cluster")
    summary: str = Field(description="A 1-sentence summary of this cluster")


class CategorySynthesizer:
    def __init__(self, small_threshold: int = 20, clustering_config: dict = None, llm_config: dict = None):
        self.small_threshold = small_threshold
        self.clustering_config = clustering_config or {}
        llm_config = llm_config or {}

        self.llm_client = GeminiClient(
            default_model=GeminiModelType.FLASH,
            default_temperature=llm_config.get("temperature", 0.0),
            thinking_budget=llm_config.get("thinking_budget", 0),
            max_connections=llm_config.get("max_workers", 50) * 2,
            max_keepalive_connections=llm_config.get("max_workers", 50) // 2
        )

        self.embedding_client = GeminiEmbeddingClient()

        logger.info(f"CategorySynthesizer initialized (threshold: {small_threshold})")

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

        logger.info(f"Grouped messages into {len(categories)} categories")

        return dict(categories)

    def ai_summarize(self, category_key: tuple, messages: List[ClassifiedMessage]) -> CategorySummary:
        primary, secondary = category_key

        message_texts = [msg.message.message_text for msg in messages]
        sample_texts = message_texts[:50] if len(message_texts) > 50 else message_texts

        sample_list = '\n'.join([f"{i+1}. {text}" for i, text in enumerate(sample_texts)])

        prompt = f"""Summarize these civic messages from the {primary}/{secondary} category.

MESSAGES ({len(messages)} total, showing {len(sample_texts)}):
{sample_list}

Provide a comprehensive analysis of the key concerns, themes, representative quotes, and action items requested by citizens."""

        try:
            response: AISummaryResponse = self.llm_client.generate_structured_content(
                prompt=prompt,
                response_schema=AISummaryResponse
            )

            stance_counts = defaultdict(int)
            for msg in messages:
                stance_counts[msg.classification.stance] += 1

            unique_phones = len(set(msg.message.phone_number for msg in messages))

            quotes_with_attribution = self.match_quotes_to_phones(response.verbatim_quotes, messages) if response.verbatim_quotes else []

            return CategorySummary(
                primary_category=primary,
                secondary_category=secondary,
                message_count=len(messages),
                unique_respondents=unique_phones,
                method="ai_summary",
                summary=response.summary,
                key_themes=response.key_themes,
                verbatim_quotes=response.verbatim_quotes,
                verbatim_quotes_with_attribution=quotes_with_attribution,
                action_items=response.action_items,
                sentiment_distribution=dict(stance_counts)
            )

        except Exception as e:
            logger.error(f"AI summarization failed for {primary}/{secondary}: {e}")
            return self._create_fallback_summary(category_key, messages)

    def cluster_and_summarize(self, category_key: tuple, messages: List[ClassifiedMessage]) -> CategorySummary:
        primary, secondary = category_key

        try:
            texts = [msg.message.message_text for msg in messages]

            logger.info(f"Generating embeddings for {len(texts)} messages in {primary}/{secondary}")
            embeddings = self.embedding_client.create_embeddings(
                texts,
                parallel=True,
                batch_size=self.clustering_config.get("batch_size", 100),
                max_concurrent_batches=self.clustering_config.get("max_concurrent_batches", 2)
            )

            logger.info(f"Clustering {len(texts)} messages...")
            import hdbscan

            min_cluster_size = max(3, self.clustering_config.get("min_cluster_size", 5))
            clusterer = hdbscan.HDBSCAN(
                min_cluster_size=min_cluster_size,
                min_samples=2,
                metric='euclidean'
            )

            cluster_labels = clusterer.fit_predict(embeddings)

            clusters = defaultdict(list)
            for idx, label in enumerate(cluster_labels):
                if label != -1:
                    clusters[label].append((messages[idx], texts[idx]))

            logger.info(f"Found {len(clusters)} clusters for {primary}/{secondary}")

            cluster_summaries = []
            for cluster_id, cluster_messages in clusters.items():
                if len(cluster_messages) >= 3:
                    cluster_texts = [text for _, text in cluster_messages]
                    sample = cluster_texts[:10]

                    cluster_prompt = f"""Briefly summarize this cluster of similar civic messages:

{chr(10).join([f"- {text}" for text in sample])}

Provide a 2-word theme label and a 1-sentence summary."""

                    try:
                        cluster_response: ClusterThemeResponse = self.llm_client.generate_structured_content(
                            prompt=cluster_prompt,
                            response_schema=ClusterThemeResponse
                        )

                        cluster_info = ClusterInfo(
                            cluster_id=cluster_id,
                            size=len(cluster_messages),
                            theme=cluster_response.theme,
                            summary=cluster_response.summary,
                            example_messages=cluster_texts[:5]
                        )

                        cluster_summaries.append(cluster_info)

                    except Exception as e:
                        logger.warning(f"Cluster {cluster_id} summarization failed: {e}")

            all_themes = [c.theme for c in cluster_summaries]
            all_quotes = []
            for c in cluster_summaries:
                all_quotes.extend(c.example_messages[:2])

            combined_summary = f"Found {len(clusters)} distinct themes across {len(messages)} messages. " + \
                             " | ".join([f"{c.theme} ({c.size} msgs)" for c in cluster_summaries[:5]])

            stance_counts = defaultdict(int)
            for msg in messages:
                stance_counts[msg.classification.stance] += 1

            unique_phones = len(set(msg.message.phone_number for msg in messages))

            quotes_with_attribution = self.match_quotes_to_phones(all_quotes[:10], messages) if all_quotes else []

            return CategorySummary(
                primary_category=primary,
                secondary_category=secondary,
                message_count=len(messages),
                unique_respondents=unique_phones,
                method="cluster_summary",
                summary=combined_summary,
                key_themes=all_themes[:10],
                verbatim_quotes=all_quotes[:10],
                verbatim_quotes_with_attribution=quotes_with_attribution,
                action_items=[],
                sentiment_distribution=dict(stance_counts),
                clusters=cluster_summaries
            )

        except Exception as e:
            logger.error(f"Clustering failed for {primary}/{secondary}: {e}, falling back to AI summary")
            return self.ai_summarize(category_key, messages)

    def _create_fallback_summary(self, category_key: tuple, messages: List[ClassifiedMessage]) -> CategorySummary:
        primary, secondary = category_key

        stance_counts = defaultdict(int)
        for msg in messages:
            stance_counts[msg.classification.stance] += 1

        unique_phones = len(set(msg.message.phone_number for msg in messages))

        return CategorySummary(
            primary_category=primary,
            secondary_category=secondary,
            message_count=len(messages),
            unique_respondents=unique_phones,
            method="fallback",
            summary=f"{len(messages)} messages in {primary}/{secondary}",
            key_themes=[],
            verbatim_quotes=[],
            action_items=[],
            sentiment_distribution=dict(stance_counts)
        )

    def synthesize_categories(self, classified_messages: List[ClassifiedMessage]) -> List[CategorySummary]:
        logger.info("Synthesizing category summaries...")

        categories = self.group_by_category(classified_messages)

        summaries = []

        for category_key, messages in categories.items():
            primary, secondary = category_key

            if len(messages) < self.small_threshold:
                logger.info(f"Category {primary}/{secondary}: {len(messages)} messages - using AI summary")
                summary = self.ai_summarize(category_key, messages)
            else:
                logger.info(f"Category {primary}/{secondary}: {len(messages)} messages - using clustering")
                summary = self.cluster_and_summarize(category_key, messages)

            summaries.append(summary)

        summaries.sort(key=lambda x: x.message_count, reverse=True)

        logger.info(f"Generated {len(summaries)} category summaries")

        return summaries

    def get_usage_stats(self):
        llm_stats = self.llm_client.get_usage_stats() if hasattr(self.llm_client, 'get_usage_stats') else {}
        embed_stats = self.embedding_client.get_cost_stats() if hasattr(self.embedding_client, 'get_cost_stats') else {}

        return {
            "llm": llm_stats,
            "embedding": embed_stats
        }


def synthesize_data_stage(classified_messages: List[ClassifiedMessage], config: dict) -> List[CategorySummary]:
    logger.info("=== STAGE 5: CATEGORY SYNTHESIS ===")

    synthesizer_config = config.get("synthesizer", {})
    synthesizer = CategorySynthesizer(
        small_threshold=synthesizer_config.get("small_category_threshold", 20),
        clustering_config=synthesizer_config.get("clustering", {}),
        llm_config=synthesizer_config.get("llm_config", {})
    )

    summaries = synthesizer.synthesize_categories(classified_messages)

    usage_stats = synthesizer.get_usage_stats()
    if usage_stats.get("llm"):
        logger.info(f"Synthesis LLM Usage - Calls: {usage_stats['llm'].get('api_call_count', 0)}, "
                   f"Cost: ${usage_stats['llm'].get('total_cost', 0):.4f}")
    if usage_stats.get("embedding"):
        logger.info(f"Embedding Usage - Count: {usage_stats['embedding'].get('total_embeddings_created', 0)}, "
                   f"Cost: ${usage_stats['embedding'].get('total_cost', 0):.4f}")

    return summaries
