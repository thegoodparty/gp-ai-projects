#!/usr/bin/env python3

import numpy as np
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from sklearn.cluster import AgglomerativeClustering
from sklearn.metrics import silhouette_score, calinski_harabasz_score, davies_bouldin_score
import scipy.cluster.hierarchy as shc
from scipy.spatial.distance import pdist

from shared.logger import get_logger
from ..models import EmbeddedMessage, ClusteredMessage, ClusterAssignment, PipelineConfig

logger = get_logger(__name__)

class HierarchicalClusterEngine:
    """Hierarchical (Agglomerative) clustering engine with automatic optimal cluster detection"""

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.hierarchical_config = getattr(config, 'hierarchical', {})

        # Fixed deterministic parameters from configuration
        self.linkage = self.hierarchical_config.get('linkage', 'ward')
        self.affinity = self.hierarchical_config.get('affinity', 'euclidean')
        self.distance_threshold = self.hierarchical_config.get('distance_threshold', None)
        self.n_clusters = self.hierarchical_config.get('n_clusters', 15)
        self.compute_distances = self.hierarchical_config.get('compute_distances', True)

        # Dimensionality reduction (disabled per user request)
        self.use_reduced_embeddings = self.hierarchical_config.get('use_reduced_embeddings', False)

        logger.info(f"HierarchicalClusterEngine initialized: linkage={self.linkage}, affinity={self.affinity}")
        logger.info(f"n_clusters={self.n_clusters}, distance_threshold={self.distance_threshold}")
        logger.info(f"Using full 3072d embeddings (no dimensionality reduction)")

    def cluster_messages(self, embedded_messages: List[EmbeddedMessage]) -> List[ClusteredMessage]:
        """
        Cluster messages using Hierarchical Agglomerative clustering

        Args:
            embedded_messages: Messages with embeddings
            optimized_params: Optional optimized parameters

        Returns:
            List of clustered messages
        """
        if not embedded_messages:
            logger.warning("No embedded messages provided for clustering")
            return []

        logger.info(f"Starting deterministic Hierarchical clustering on {len(embedded_messages)} messages")

        # Extract full 3072d embeddings
        embeddings = self._extract_full_embeddings(embedded_messages)

        # Run deterministic hierarchical clustering
        cluster_labels = self._run_hierarchical_clustering(
            embeddings, self.linkage, self.affinity, self.distance_threshold, self.n_clusters
        )

        clustered_messages = self._create_clustered_messages(
            embedded_messages, cluster_labels, self.linkage, self.affinity, self.n_clusters
        )

        n_clusters_found = len(set(cluster_labels))
        logger.info(f"Hierarchical clustering complete: {n_clusters_found} clusters found")

        cluster_sizes = [len([l for l in cluster_labels if l == i]) for i in range(n_clusters_found)]
        cluster_sizes.sort(reverse=True)
        logger.info(f"Top cluster sizes: {cluster_sizes[:5]}")
        logger.info(f"Average cluster size: {np.mean(cluster_sizes):.1f}")

        return clustered_messages


    def _extract_full_embeddings(self, embedded_messages: List[EmbeddedMessage]) -> np.ndarray:
        """Extract full-dimensional embeddings (3072d from Gemini)"""

        full_embeddings = []
        missing_count = 0

        for msg in embedded_messages:
            if hasattr(msg.embeddings, 'embedding_3072d') and msg.embeddings.embedding_3072d is not None:
                full_embeddings.append(msg.embeddings.embedding_3072d)
            else:
                missing_count += 1
                logger.error(f"Message {msg.id} missing full 3072d embeddings")

        if missing_count > 0:
            raise ValueError(f"Found {missing_count} messages without full 3072d embeddings. "
                           f"Hierarchical clustering requires full embeddings for optimal performance.")

        embeddings = np.array(full_embeddings)
        logger.info(f"Extracted full embeddings: {embeddings.shape} (all 3072d)")
        return embeddings


    def _run_hierarchical_clustering(self, embeddings: np.ndarray, linkage: str,
                                   affinity: str, distance_threshold: Optional[float],
                                   n_clusters: Optional[int]) -> np.ndarray:
        """Run hierarchical clustering with specified parameters"""

        try:
            logger.info(f"Running AgglomerativeClustering: linkage={linkage}, affinity={affinity}")

            clustering_params = {
                "n_clusters": n_clusters,
                "distance_threshold": distance_threshold,
                "linkage": linkage,
                "compute_distances": self.compute_distances
            }

            # Handle ward linkage special case (only works with euclidean)
            if linkage == "ward":
                if affinity != "euclidean":
                    logger.warning(f"Ward linkage requires euclidean metric, ignoring affinity={affinity}")
                # Ward linkage doesn't need affinity/metric parameter
            else:
                clustering_params["metric"] = affinity  # Use 'metric' instead of 'affinity'

            clustering = AgglomerativeClustering(**clustering_params)
            labels = clustering.fit_predict(embeddings)

            self._log_cluster_metrics(embeddings, labels)

            return labels

        except Exception as e:
            logger.error(f"Hierarchical clustering failed: {e}")
            logger.warning("Falling back to single cluster assignment")
            return np.zeros(len(embeddings), dtype=int)

    def _log_cluster_metrics(self, embeddings: np.ndarray, labels: np.ndarray):
        """Calculate and log clustering quality metrics"""

        try:
            unique_labels = set(labels)
            n_clusters = len(unique_labels)

            if n_clusters > 1:
                silhouette = silhouette_score(embeddings, labels)
                calinski_harabasz = calinski_harabasz_score(embeddings, labels)
                davies_bouldin = davies_bouldin_score(embeddings, labels)

                logger.info(f"Clustering quality metrics:")
                logger.info(f"  Silhouette Score: {silhouette:.4f}")
                logger.info(f"  Calinski-Harabasz Score: {calinski_harabasz:.4f}")
                logger.info(f"  Davies-Bouldin Score: {davies_bouldin:.4f}")
            else:
                logger.info("Single cluster found - no quality metrics to calculate")

        except Exception as e:
            logger.warning(f"Failed to calculate clustering metrics: {e}")

    def _create_clustered_messages(self, embedded_messages: List[EmbeddedMessage],
                                 cluster_labels: np.ndarray, linkage: str, affinity: str, n_clusters: int) -> List[ClusteredMessage]:
        """Convert embedded messages to clustered messages with assignments"""

        clustered_messages = []

        for i, embedded_message in enumerate(embedded_messages):
            cluster_id = int(cluster_labels[i])

            cluster_assignment = ClusterAssignment(
                cluster_id=cluster_id,
                cluster_confidence=1.0,  # Hierarchical clustering is deterministic
                distance_to_centroid=0.0,  # Not applicable for hierarchical clustering
                is_noise=False,  # All points are assigned to clusters
                clustering_algorithm="hierarchical",
                clustering_parameters={
                    "linkage": self.linkage,
                    "affinity": self.affinity,
                    "n_clusters": self.n_clusters or "auto"
                }
            )

            clustered_message = ClusteredMessage(
                id=embedded_message.id,
                embedded_message_id=embedded_message.id,
                csv_file=embedded_message.csv_file,
                csv_row_index=embedded_message.csv_row_index,
                text=embedded_message.text,
                original_text=embedded_message.original_text,
                campaign_source=embedded_message.campaign_source,
                cluster_assignment=cluster_assignment,
                embeddings=embedded_message.embeddings,
                created_at=datetime.now()
            )

            clustered_messages.append(clustered_message)

        return clustered_messages

def hierarchical_cluster_engine_stage(embedded_messages: List[EmbeddedMessage],
                                    config: PipelineConfig) -> List[ClusteredMessage]:
    """
    Hierarchical clustering stage function

    Args:
        embedded_messages: Messages with embeddings
        config: Pipeline configuration

    Returns:
        List of clustered messages
    """
    engine = HierarchicalClusterEngine(config)
    return engine.cluster_messages(embedded_messages)