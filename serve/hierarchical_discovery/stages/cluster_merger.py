#!/usr/bin/env python3

import numpy as np
from typing import List, Dict, Tuple, Set
from datetime import datetime
import asyncio
from concurrent.futures import ThreadPoolExecutor

from shared.logger import get_logger
from shared.llm_gemini import GeminiClient, GeminiModelType
from ..models import ClusteredMessage, ClusterTheme, PipelineConfig
from pydantic import BaseModel, Field

logger = get_logger(__name__)

class ClusterSimilarityResponse(BaseModel):
    should_merge: bool = Field(description="True if clusters should be merged, False otherwise")
    reasoning: str = Field(description="Brief explanation of why clusters should or should not be merged")
    merged_theme: str = Field(default="", description="If merging, the combined theme name (empty if not merging)")

class MergeGroup(BaseModel):
    cluster_ids: List[int] = Field(description="List of cluster IDs that should be merged together (single ID if no merge)")
    merged_theme: str = Field(description="The combined theme name for this merge group")
    reasoning: str = Field(description="Brief explanation of why these clusters were grouped together")

class ClusterMergeGroupsResponse(BaseModel):
    merge_groups: List[MergeGroup] = Field(description="List of merge groups. Each group contains cluster IDs that should be merged together.")

class ClusterMerger:
    """
    Post-processing stage that merges semantically similar clusters using LLM analysis.
    This allows hierarchical clustering to initially create fine-grained clusters,
    then intelligently merge duplicates or highly similar themes.
    """

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.merger_config = getattr(config, 'cluster_merger', {})

        self.enabled = self.merger_config.get('enabled', True)
        self.similarity_threshold = self.merger_config.get('similarity_threshold', 0.8)
        self.max_workers = self.merger_config.get('max_workers', 10)

        self.llm_client = GeminiClient(
            default_model=GeminiModelType.FLASH,
            default_temperature=0.0,
            thinking_budget=0,
            max_connections=self.max_workers,
            max_keepalive_connections=max(1, self.max_workers // 4)
        )

        self.thread_pool = ThreadPoolExecutor(max_workers=self.max_workers)

        logger.info(f"ClusterMerger initialized: enabled={self.enabled}, max_workers={self.max_workers}")

    async def merge_similar_clusters(
        self,
        clustered_messages: List[ClusteredMessage],
        cluster_themes: Dict[int, ClusterTheme]
    ) -> Tuple[List[ClusteredMessage], Dict[int, ClusterTheme]]:
        """
        Merge semantically similar clusters using hybrid embedding + LLM comparison.

        Args:
            clustered_messages: List of clustered messages
            cluster_themes: Dictionary mapping cluster_id to ClusterTheme

        Returns:
            Tuple of (updated messages, updated themes) after merging
        """
        if not self.enabled:
            logger.info("Cluster merging disabled, skipping")
            return clustered_messages, cluster_themes

        if len(cluster_themes) < 2:
            logger.info("Only 1 cluster, no merging needed")
            return clustered_messages, cluster_themes

        logger.info(f"Starting cluster merge analysis for {len(cluster_themes)} clusters")

        cluster_centroids = self._compute_cluster_centroids(clustered_messages)
        merge_map, merge_candidates = await self._identify_mergeable_clusters(cluster_themes, cluster_centroids)

        if not merge_map:
            logger.info("No clusters identified for merging")
            return clustered_messages, cluster_themes

        logger.info(f"Merging {len(merge_map)} cluster pairs")

        updated_messages = self._apply_merges(clustered_messages, merge_map)
        updated_themes = self._merge_cluster_themes(cluster_themes, merge_map, merge_candidates)

        final_cluster_count = len(set(msg.cluster_assignment.cluster_id for msg in updated_messages))
        logger.info(f"Cluster merging complete: {len(cluster_themes)} → {final_cluster_count} clusters")

        return updated_messages, updated_themes

    def _compute_cluster_centroids(self, clustered_messages: List[ClusteredMessage]) -> Dict[int, np.ndarray]:
        """Compute cluster centroids using 3072d embeddings (same as clustering)"""
        from collections import defaultdict

        cluster_embeddings = defaultdict(list)

        # Always use 3072d embeddings to match clustering dimensions
        for msg in clustered_messages:
            cluster_id = msg.cluster_assignment.cluster_id
            if hasattr(msg.embeddings, 'embedding_3072d') and msg.embeddings.embedding_3072d is not None:
                cluster_embeddings[cluster_id].append(msg.embeddings.embedding_3072d)

        cluster_centroids = {}
        for cluster_id, embeddings in cluster_embeddings.items():
            if embeddings:
                centroid = np.mean(embeddings, axis=0)
                centroid = centroid / np.linalg.norm(centroid)  # Normalize for cosine similarity
                cluster_centroids[cluster_id] = centroid

        logger.info(f"Computed 3072d centroids for {len(cluster_centroids)} clusters")
        return cluster_centroids

    async def _identify_mergeable_clusters(
        self,
        cluster_themes: Dict[int, ClusterTheme],
        cluster_centroids: Dict[int, np.ndarray]
    ) -> Tuple[Dict[int, int], List[Tuple[int, int, ClusterSimilarityResponse]]]:
        """
        Identify which clusters should be merged using all-at-once LLM analysis.
        Uses embedding pre-filter to identify similar clusters, then sends all to LLM at once.

        Returns:
            Tuple of (merge_map, merge_candidates) where merge_map is a dict mapping source_cluster_id → target_cluster_id
        """
        try:
            # Use embedding pre-filter to identify potentially similar clusters
            embedding_similarity_threshold = self.merger_config.get('embedding_similarity_threshold', 0.7)
            logger.info(f"Using embedding_similarity_threshold: {embedding_similarity_threshold}")

            candidate_cluster_ids = self._find_similar_cluster_candidates(
                cluster_themes, cluster_centroids, embedding_similarity_threshold
            )

            if not candidate_cluster_ids:
                logger.info("No similar clusters found in embedding pre-filter")
                return {}, []

            logger.info(f"Sending {len(candidate_cluster_ids)} similar clusters to LLM for group-based merging")

            # Get merge groups from LLM (all at once)
            merge_groups_response = await self._get_merge_groups_from_llm(cluster_themes, candidate_cluster_ids)

            if not merge_groups_response or not merge_groups_response.merge_groups:
                logger.warning("LLM returned no merge groups")
                return {}, []

            # Convert merge groups to merge_map
            merge_map, merge_candidates = self._convert_groups_to_merge_map(
                merge_groups_response, cluster_themes
            )

            logger.info(f"LLM identified {len(merge_groups_response.merge_groups)} groups, resulting in {len(merge_map)} merges")

            return merge_map, merge_candidates

        except Exception as e:
            logger.error(f"Cluster merger failed: {e}. Falling back to no merging.")
            return {}, []

    def _find_similar_cluster_candidates(
        self,
        cluster_themes: Dict[int, ClusterTheme],
        cluster_centroids: Dict[int, np.ndarray],
        embedding_threshold: float
    ) -> Set[int]:
        """
        Use embedding similarity to find clusters that might be similar.
        Returns set of cluster IDs that have at least one similar neighbor.
        """
        cluster_ids = sorted(cluster_themes.keys())
        similar_clusters = set()

        for i in range(len(cluster_ids)):
            for j in range(i + 1, len(cluster_ids)):
                cluster_a_id = cluster_ids[i]
                cluster_b_id = cluster_ids[j]

                if cluster_a_id in cluster_centroids and cluster_b_id in cluster_centroids:
                    cosine_sim = np.dot(cluster_centroids[cluster_a_id], cluster_centroids[cluster_b_id])

                    if cosine_sim >= embedding_threshold:
                        similar_clusters.add(cluster_a_id)
                        similar_clusters.add(cluster_b_id)
                        logger.debug(f"Clusters {cluster_a_id} and {cluster_b_id} are similar (cosine={cosine_sim:.3f})")

        logger.info(f"Found {len(similar_clusters)} clusters with similar neighbors (threshold={embedding_threshold})")
        return similar_clusters

    async def _get_merge_groups_from_llm(
        self,
        cluster_themes: Dict[int, ClusterTheme],
        candidate_cluster_ids: Set[int]
    ) -> ClusterMergeGroupsResponse:
        """
        Send all candidate clusters to LLM at once for group-based merge decision.
        """
        # Build cluster descriptions
        cluster_descriptions = []
        for cluster_id in sorted(candidate_cluster_ids):
            theme = cluster_themes[cluster_id]
            cluster_descriptions.append(
                f"Cluster {cluster_id}: Theme=\"{theme.theme}\", Category=\"{theme.category}\", Issues=\"{theme.issues_summary}\""
            )

        clusters_text = "\n".join(cluster_descriptions)

        prompt = f"""Analyze these citizen message clusters and identify which ones should be merged together.

CLUSTERS:
{clusters_text}

INSTRUCTIONS:
Your task is to group clusters that are NEAR-DUPLICATES or discuss the EXACT SAME specific issue. Each group will be merged into a single cluster.

✅ MERGE clusters into the same group ONLY if:
1. They are essentially SYNONYMS discussing the EXACT SAME issue
   - Example: "Property Taxes" + "Tax Burden" → SAME (both about high property taxes)
   - Example: "Rising Property Taxes" + "High Property Taxes" → SAME (both about property tax levels)
2. They would require the SAME action items and solutions
   - Example: "Pothole Repairs" + "Road Surface Quality" → SAME (both need road maintenance)

❌ DO NOT merge clusters if:
1. They are RELATED but require DIFFERENT action items
   - Example: "School Shootings" ≠ "Gang Activity" (different solutions: school security vs youth programs)
   - Example: "Domestic Violence" ≠ "General Crime" (different solutions: victim services vs policing)
   - Example: "Property Taxes" ≠ "School Funding" (related category but different issues)
2. One is a SUBCATEGORY or SUBSET of the other
   - Example: "Traffic on Main Street" ≠ "General Traffic Concerns" (keep specific separate)
   - Example: "Park Maintenance" ≠ "Recreation Facilities" (different scopes)
3. They are in the same CATEGORY but are distinct citizen concerns
   - Example: "Crime" category: Keep separate "School Safety", "Gang Activity", "Domestic Violence"
   - Example: "Infrastructure" category: Keep separate "Roads", "Water", "Sewage"

GUIDELINE: When in doubt, keep clusters SEPARATE. Preserve granularity for actionable campaign responses.

OUTPUT FORMAT:
- Create one group for each distinct issue/concern
- If a cluster shouldn't be merged with anything, put it in its own group with just that cluster ID
- Provide a merged theme name that captures all clusters in the group
- For each group, explain why those clusters belong together

Example output structure:
{{
  "merge_groups": [
    {{
      "cluster_ids": [0, 3],
      "merged_theme": "Property Tax Burden",
      "reasoning": "Both discuss high property taxes requiring same fiscal policy solutions"
    }},
    {{
      "cluster_ids": [1],
      "merged_theme": "School Safety Concerns",
      "reasoning": "Standalone - distinct from general crime, needs specific school security solutions"
    }},
    {{
      "cluster_ids": [2],
      "merged_theme": "Gang Activity",
      "reasoning": "Standalone - distinct from school safety, needs youth intervention programs"
    }}
  ]
}}"""

        response = await asyncio.get_event_loop().run_in_executor(
            self.thread_pool,
            lambda: self.llm_client.generate_structured_content(
                prompt=prompt,
                response_schema=ClusterMergeGroupsResponse,
                system_instruction="You are an expert at identifying semantically similar citizen concerns and grouping them intelligently."
            )
        )

        return response

    def _convert_groups_to_merge_map(
        self,
        merge_groups_response: ClusterMergeGroupsResponse,
        cluster_themes: Dict[int, ClusterTheme]
    ) -> Tuple[Dict[int, int], List[Tuple[int, int, ClusterSimilarityResponse]]]:
        """
        Convert LLM's merge groups into merge_map format.
        For each group with multiple clusters, merge smaller clusters into the largest one.
        """
        merge_map = {}
        merge_candidates = []

        for group in merge_groups_response.merge_groups:
            if len(group.cluster_ids) <= 1:
                # No merge needed for single-cluster groups
                continue

            # Find largest cluster in the group (by total_mentions)
            cluster_sizes = {
                cid: getattr(cluster_themes[cid], 'total_mentions', 0)
                for cid in group.cluster_ids
                if cid in cluster_themes
            }

            if not cluster_sizes:
                continue

            target_id = max(cluster_sizes.keys(), key=lambda cid: cluster_sizes[cid])

            # Merge all other clusters into the target
            for source_id in group.cluster_ids:
                if source_id != target_id:
                    merge_map[source_id] = target_id

                    # Create fake pairwise response for compatibility with existing code
                    fake_response = ClusterSimilarityResponse(
                        should_merge=True,
                        reasoning=group.reasoning,
                        merged_theme=group.merged_theme
                    )
                    merge_candidates.append((source_id, target_id, fake_response))

            logger.info(f"✓ Merge group: {group.cluster_ids} → {target_id} ('{group.merged_theme}')")
            logger.debug(f"  Reasoning: {group.reasoning}")

        return merge_map, merge_candidates

    def _resolve_merge_conflicts(
        self,
        merge_candidates: List[Tuple[int, int, ClusterSimilarityResponse]],
        cluster_themes: Dict[int, ClusterTheme]
    ) -> Dict[int, int]:
        """
        Resolve conflicts when multiple clusters want to merge with the same target.
        Creates a merge map where smaller clusters merge into larger ones.
        """
        cluster_sizes = {
            cluster_id: getattr(theme, 'total_mentions', 0)
            for cluster_id, theme in cluster_themes.items()
        }

        merge_groups: Dict[int, Set[int]] = {}

        for cluster_a_id, cluster_b_id, _ in merge_candidates:
            larger_id = cluster_a_id if cluster_sizes[cluster_a_id] >= cluster_sizes[cluster_b_id] else cluster_b_id
            smaller_id = cluster_b_id if larger_id == cluster_a_id else cluster_a_id

            if larger_id not in merge_groups:
                merge_groups[larger_id] = {larger_id}

            merge_groups[larger_id].add(smaller_id)

        merge_map = {}
        for target_id, source_ids in merge_groups.items():
            for source_id in source_ids:
                if source_id != target_id:
                    merge_map[source_id] = target_id

        return merge_map

    def _apply_merges(
        self,
        clustered_messages: List[ClusteredMessage],
        merge_map: Dict[int, int]
    ) -> List[ClusteredMessage]:
        """
        Apply cluster merges while preserving original cluster assignments.
        """
        merge_groups = self._build_merge_groups(merge_map)

        updated_messages = []

        for msg in clustered_messages:
            old_cluster_id = msg.cluster_assignment.cluster_id

            if msg.cluster_assignment.original_cluster_id is None:
                msg.cluster_assignment.original_cluster_id = old_cluster_id

            new_cluster_id = old_cluster_id
            while new_cluster_id in merge_map:
                new_cluster_id = merge_map[new_cluster_id]

            if new_cluster_id != old_cluster_id:
                msg.cluster_assignment.merged_cluster_id = new_cluster_id
                msg.cluster_assignment.merge_source_clusters = list(merge_groups.get(new_cluster_id, {new_cluster_id}))
                msg.cluster_assignment.cluster_id = new_cluster_id

            updated_messages.append(msg)

        return updated_messages

    def _build_merge_groups(self, merge_map: Dict[int, int]) -> Dict[int, Set[int]]:
        merge_groups = {}

        for source_id, target_id in merge_map.items():
            if target_id not in merge_groups:
                merge_groups[target_id] = {target_id}
            merge_groups[target_id].add(source_id)

        return merge_groups

    def _merge_cluster_themes(
        self,
        cluster_themes: Dict[int, ClusterTheme],
        merge_map: Dict[int, int],
        merge_candidates: List[Tuple[int, int, ClusterSimilarityResponse]]
    ) -> Dict[int, ClusterTheme]:
        """
        Merge cluster themes, combining statistics from merged clusters.
        Uses LLM-generated merged theme names instead of keeping original names.
        """
        logger.debug(f"_merge_cluster_themes: Starting with {len(cluster_themes)} themes and {len(merge_map)} merges")

        # Build map of target_id -> LLM-generated merged theme name
        merged_theme_names = {}
        for cluster_a_id, cluster_b_id, similarity_response in merge_candidates:
            if similarity_response.should_merge and similarity_response.merged_theme:
                # Determine which cluster is the target (larger one)
                cluster_sizes = {
                    cluster_a_id: getattr(cluster_themes[cluster_a_id], 'total_mentions', 0),
                    cluster_b_id: getattr(cluster_themes[cluster_b_id], 'total_mentions', 0)
                }
                target_id = cluster_a_id if cluster_sizes[cluster_a_id] >= cluster_sizes[cluster_b_id] else cluster_b_id

                # Store the LLM-generated merged theme name
                if target_id not in merged_theme_names:
                    merged_theme_names[target_id] = similarity_response.merged_theme
                    logger.info(f"Using LLM-generated merged theme for cluster {target_id}: '{similarity_response.merged_theme}'")

        merged_themes = {}

        for cluster_id, theme in cluster_themes.items():
            logger.debug(f"Processing cluster {cluster_id}, theme='{theme.theme}'")

            target_id = cluster_id
            while target_id in merge_map:
                target_id = merge_map[target_id]

            logger.debug(f"Cluster {cluster_id} maps to target {target_id}")

            if target_id not in merged_themes:
                if target_id == cluster_id:
                    logger.debug(f"Cluster {cluster_id} is a target, using theme directly")
                    # Check if this target has an LLM-generated merged theme name
                    if target_id in merged_theme_names:
                        # Create new theme with LLM-generated name
                        logger.debug(f"Updating cluster {cluster_id} theme to LLM-generated: '{merged_theme_names[target_id]}'")
                        merged_themes[target_id] = ClusterTheme(
                            theme=merged_theme_names[target_id],  # Use LLM-generated merged theme
                            summary=theme.summary,
                            cluster_id=target_id,
                            category=theme.category,
                            sentiment=theme.sentiment,
                            civic_relevance=theme.civic_relevance,
                            confidence_score=theme.confidence_score,
                            unique_respondents=theme.unique_respondents,
                            total_mentions=theme.total_mentions,
                            avg_mentions_per_respondent=theme.avg_mentions_per_respondent,
                            respondent_coverage_pct=theme.respondent_coverage_pct,
                            issues_summary=theme.issues_summary,
                            detailed_analysis=theme.detailed_analysis,
                            verbatim_quotes=theme.verbatim_quotes.copy() if theme.verbatim_quotes else [],
                            action_items=theme.action_items.copy() if theme.action_items else [],
                            key_topics=theme.key_topics.copy() if theme.key_topics else []
                        )
                    else:
                        merged_themes[target_id] = theme
                else:
                    logger.debug(f"Cluster {cluster_id} merges into {target_id}, creating new merged theme")
                    base_theme = cluster_themes[target_id]

                    logger.debug(f"base_theme attributes: theme={base_theme.theme}, summary={base_theme.summary}, "
                               f"unique_respondents={base_theme.unique_respondents}, total_mentions={base_theme.total_mentions}")

                    try:
                        # Use LLM-generated merged theme name if available
                        theme_name = merged_theme_names.get(target_id, base_theme.theme)

                        merged_themes[target_id] = ClusterTheme(
                            theme=theme_name,  # Use LLM-generated merged theme or fallback to base
                            summary=base_theme.summary,
                            cluster_id=target_id,
                            category=base_theme.category,
                            sentiment=base_theme.sentiment,
                            civic_relevance=base_theme.civic_relevance,
                            confidence_score=base_theme.confidence_score,
                            unique_respondents=base_theme.unique_respondents,
                            total_mentions=base_theme.total_mentions,
                            avg_mentions_per_respondent=base_theme.avg_mentions_per_respondent,
                            respondent_coverage_pct=base_theme.respondent_coverage_pct,
                            issues_summary=base_theme.issues_summary,
                            detailed_analysis=base_theme.detailed_analysis,
                            verbatim_quotes=base_theme.verbatim_quotes.copy() if base_theme.verbatim_quotes else [],
                            action_items=base_theme.action_items.copy() if base_theme.action_items else [],
                            key_topics=base_theme.key_topics.copy() if base_theme.key_topics else []
                        )
                        logger.debug(f"Successfully created merged theme for cluster {target_id} with name: '{theme_name}'")
                    except Exception as e:
                        logger.error(f"Failed to create ClusterTheme for target {target_id}: {e}")
                        logger.error(f"base_theme type: {type(base_theme)}, attributes: {dir(base_theme)}")
                        raise
            else:
                logger.debug(f"Target {target_id} already exists, merging stats from cluster {cluster_id}")
                merged_themes[target_id].total_mentions += theme.total_mentions
                merged_themes[target_id].unique_respondents += theme.unique_respondents
                if theme.verbatim_quotes:
                    merged_themes[target_id].verbatim_quotes.extend(theme.verbatim_quotes)
                if theme.key_topics:
                    merged_themes[target_id].key_topics.extend(theme.key_topics)

        logger.debug(f"Finalizing {len(merged_themes)} merged themes")
        for theme in merged_themes.values():
            if theme.verbatim_quotes:
                theme.verbatim_quotes = list(dict.fromkeys(theme.verbatim_quotes))[:5]
            if theme.key_topics:
                theme.key_topics = list(dict.fromkeys(theme.key_topics))[:10]

            unique_resp = theme.unique_respondents
            total_ment = theme.total_mentions
            if unique_resp > 0 and total_ment > 0:
                theme.avg_mentions_per_respondent = total_ment / unique_resp

        logger.info(f"Merge complete: {len(merged_themes)} final themes")
        return merged_themes


async def cluster_merger_stage(
    clustered_messages: List[ClusteredMessage],
    cluster_themes: Dict[int, ClusterTheme],
    config: PipelineConfig
) -> Tuple[List[ClusteredMessage], Dict[int, ClusterTheme]]:
    """
    Cluster merger stage function

    Args:
        clustered_messages: Messages with cluster assignments
        cluster_themes: Cluster theme analysis results
        config: Pipeline configuration

    Returns:
        Tuple of (updated messages, updated themes)
    """
    merger = ClusterMerger(config)
    return await merger.merge_similar_clusters(clustered_messages, cluster_themes)
