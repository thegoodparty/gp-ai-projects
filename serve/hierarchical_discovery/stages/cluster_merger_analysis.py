#!/usr/bin/env python3

import numpy as np
from typing import List, Dict, Tuple
from collections import defaultdict
from datetime import datetime

from shared.logger import get_logger
from ..models import ClusteredMessage, ClusterTheme

logger = get_logger(__name__)

class ClusterMergerAnalysis:

    def __init__(self):
        pass

    def analyze_merger_results(
        self,
        clustered_messages: List[ClusteredMessage],
        cluster_themes: Dict[int, ClusterTheme],
        pre_merge_count: int
    ) -> Dict:

        logger.info("=" * 80)
        logger.info("CLUSTER MERGER ANALYSIS")
        logger.info("=" * 80)

        merge_stats = self._compute_merge_statistics(clustered_messages)

        original_cluster_count = pre_merge_count
        final_cluster_count = len(cluster_themes)
        reduction_pct = ((original_cluster_count - final_cluster_count) / original_cluster_count * 100) if original_cluster_count > 0 else 0

        logger.info(f"\n{'MERGER SUMMARY':^80}")
        logger.info("-" * 80)
        logger.info(f"Original clusters: {original_cluster_count}")
        logger.info(f"Final clusters:    {final_cluster_count}")
        logger.info(f"Reduction:         {original_cluster_count - final_cluster_count} clusters ({reduction_pct:.1f}%)")
        logger.info(f"Merge groups:      {merge_stats['merge_groups_count']}")

        if merge_stats['merge_groups_count'] > 0:
            logger.info(f"\n{'MERGE GROUPS DETAIL':^80}")
            logger.info("-" * 80)

            for target_id, source_info in merge_stats['merge_groups'].items():
                source_ids = source_info['source_clusters']
                message_count = source_info['message_count']

                theme = cluster_themes.get(target_id)
                theme_name = theme.theme if theme else "Unknown"

                logger.info(f"\nCluster {target_id}: '{theme_name}'")
                logger.info(f"  Messages: {message_count}")
                logger.info(f"  Merged from: {sorted(source_ids)}")
                logger.info(f"  Total source clusters: {len(source_ids)}")

        logger.info(f"\n{'UNMERGED CLUSTERS':^80}")
        logger.info("-" * 80)
        unmerged_count = original_cluster_count - sum(len(info['source_clusters']) for info in merge_stats['merge_groups'].values())
        logger.info(f"Clusters that remained independent: {unmerged_count}")

        self._analyze_size_distribution(clustered_messages, cluster_themes)

        self._analyze_merge_patterns(clustered_messages, merge_stats)

        logger.info("=" * 80)

        return {
            'original_cluster_count': original_cluster_count,
            'final_cluster_count': final_cluster_count,
            'reduction_count': original_cluster_count - final_cluster_count,
            'reduction_pct': reduction_pct,
            'merge_groups': merge_stats['merge_groups'],
            'merge_groups_count': merge_stats['merge_groups_count'],
            'unmerged_count': unmerged_count
        }

    def _compute_merge_statistics(self, clustered_messages: List[ClusteredMessage]) -> Dict:

        merge_groups = defaultdict(lambda: {'source_clusters': set(), 'message_count': 0})

        for msg in clustered_messages:
            assignment = msg.cluster_assignment

            if assignment.merge_source_clusters and len(assignment.merge_source_clusters) > 1:
                target_id = assignment.cluster_id
                merge_groups[target_id]['source_clusters'].update(assignment.merge_source_clusters)
                merge_groups[target_id]['message_count'] += 1

        return {
            'merge_groups': dict(merge_groups),
            'merge_groups_count': len(merge_groups)
        }

    def _analyze_size_distribution(
        self,
        clustered_messages: List[ClusteredMessage],
        cluster_themes: Dict[int, ClusterTheme]
    ):

        cluster_sizes = defaultdict(int)
        for msg in clustered_messages:
            cluster_sizes[msg.cluster_assignment.cluster_id] += 1

        logger.info(f"\n{'CLUSTER SIZE DISTRIBUTION':^80}")
        logger.info("-" * 80)

        sorted_clusters = sorted(cluster_sizes.items(), key=lambda x: x[1], reverse=True)

        for cluster_id, size in sorted_clusters[:10]:
            theme = cluster_themes.get(cluster_id)
            theme_name = theme.theme if theme else "Unknown"
            logger.info(f"Cluster {cluster_id:2d}: {size:4d} messages | '{theme_name}'")

        if len(sorted_clusters) > 10:
            logger.info(f"... and {len(sorted_clusters) - 10} more clusters")

        total_messages = sum(cluster_sizes.values())
        avg_size = total_messages / len(cluster_sizes) if cluster_sizes else 0
        logger.info(f"\nAverage cluster size: {avg_size:.1f} messages")

    def _analyze_merge_patterns(
        self,
        clustered_messages: List[ClusteredMessage],
        merge_stats: Dict
    ):

        logger.info(f"\n{'MERGE PATTERNS':^80}")
        logger.info("-" * 80)

        if merge_stats['merge_groups_count'] == 0:
            logger.info("No merges occurred")
            return

        merge_sizes = [len(info['source_clusters']) for info in merge_stats['merge_groups'].values()]
        max_merge_size = max(merge_sizes)
        avg_merge_size = sum(merge_sizes) / len(merge_sizes)

        logger.info(f"Largest merge: {max_merge_size} clusters combined into 1")
        logger.info(f"Average merge size: {avg_merge_size:.1f} clusters per merge group")

        merge_size_distribution = defaultdict(int)
        for size in merge_sizes:
            merge_size_distribution[size] += 1

        logger.info(f"\nMerge size distribution:")
        for size in sorted(merge_size_distribution.keys()):
            count = merge_size_distribution[size]
            logger.info(f"  {size} clusters merged: {count} occurrence(s)")


def analyze_cluster_merger(
    clustered_messages: List[ClusteredMessage],
    cluster_themes: Dict[int, ClusterTheme],
    pre_merge_count: int
) -> Dict:

    analyzer = ClusterMergerAnalysis()
    return analyzer.analyze_merger_results(clustered_messages, cluster_themes, pre_merge_count)
