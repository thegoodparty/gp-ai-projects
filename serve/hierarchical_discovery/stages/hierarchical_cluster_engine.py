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

        self.linkage = self.hierarchical_config.get('linkage', 'ward')
        self.affinity = self.hierarchical_config.get('affinity', 'euclidean')
        self.distance_threshold = self.hierarchical_config.get('distance_threshold', None)
        self.n_clusters = self.hierarchical_config.get('n_clusters', 15)
        self.compute_distances = self.hierarchical_config.get('compute_distances', True)

        logger.info(f"HierarchicalClusterEngine initialized")
        logger.info(f"  linkage={self.linkage}, affinity={self.affinity}")
        logger.info(f"  n_clusters={self.n_clusters}")

    def cluster_messages(self, embedded_messages: List[EmbeddedMessage]) -> List[ClusteredMessage]:
        """Cluster messages using pre-reduced embeddings"""

        if not embedded_messages:
            logger.warning("No embedded messages provided for clustering")
            return []

        # Separate opt-out/non-substantive messages from clusterable messages
        opt_out_messages = [m for m in embedded_messages if getattr(m, 'is_opt_out', False)]
        clusterable_messages = [m for m in embedded_messages if not getattr(m, 'is_opt_out', False)]

        logger.info(f"Starting clustering on {len(clusterable_messages)} substantive messages "
                   f"({len(opt_out_messages)} non-substantive will pass through)")

        clustered_messages = []

        # Create pass-through ClusteredMessages for opt-out messages (no cluster assignment)
        for opt_out_msg in opt_out_messages:
            cluster_assignment = ClusterAssignment(
                cluster_id=-1,
                cluster_confidence=0.0,
                distance_to_centroid=0.0,
                is_noise=True,
                clustering_algorithm="none",
                clustering_parameters={"reason": "non_substantive_passthrough"}
            )

            clustered_message = ClusteredMessage(
                id=opt_out_msg.id,
                embedded_message_id=opt_out_msg.id,
                csv_file=opt_out_msg.csv_file,
                csv_row_index=opt_out_msg.csv_row_index,
                text=opt_out_msg.text,
                original_text=opt_out_msg.original_text,
                campaign_source=opt_out_msg.campaign_source,
                cluster_assignment=cluster_assignment,
                embeddings=opt_out_msg.embeddings,
                metadata=opt_out_msg.metadata,
                is_opt_out=True,
                created_at=datetime.now()
            )
            clustered_messages.append(clustered_message)

        # Cluster only substantive messages
        if not clusterable_messages:
            logger.warning("No substantive messages to cluster")
            return clustered_messages

        embeddings = self._extract_embeddings(clusterable_messages)

        cluster_labels = self._run_hierarchical_clustering(
            embeddings, self.linkage, self.affinity, self.distance_threshold, self.n_clusters
        )

        substantive_clustered = self._create_clustered_messages(
            clusterable_messages, cluster_labels, self.linkage, self.affinity, self.n_clusters
        )
        clustered_messages.extend(substantive_clustered)

        n_clusters_found = len(set(cluster_labels))
        logger.info(f"Clustering complete: {n_clusters_found} clusters found")

        cluster_sizes = [len([l for l in cluster_labels if l == i]) for i in range(n_clusters_found)]
        cluster_sizes.sort(reverse=True)
        logger.info(f"Top cluster sizes: {cluster_sizes[:5]}")
        logger.info(f"Average cluster size: {np.mean(cluster_sizes):.1f}")

        return clustered_messages


    def _extract_embeddings(self, embedded_messages: List[EmbeddedMessage]) -> np.ndarray:
        """Extract embeddings for clustering (UMAP 15d, fallback to PCA 50d, then 3072d full)"""

        embeddings = []
        embedding_type = None

        for msg in embedded_messages:
            if hasattr(msg.embeddings, 'embedding_umap') and msg.embeddings.embedding_umap is not None:
                embeddings.append(msg.embeddings.embedding_umap)
                embedding_type = 'UMAP 15d'
            elif hasattr(msg.embeddings, 'embedding_50d') and msg.embeddings.embedding_50d is not None:
                embeddings.append(msg.embeddings.embedding_50d)
                embedding_type = '50d PCA'
            elif hasattr(msg.embeddings, 'embedding_3072d') and msg.embeddings.embedding_3072d is not None:
                embeddings.append(msg.embeddings.embedding_3072d)
                embedding_type = '3072d full'
            else:
                raise ValueError(f"Message {msg.id} missing embeddings (tried UMAP 15d, 50d PCA, and 3072d full)")

        embeddings_array = np.array(embeddings)
        logger.info(f"Using {embedding_type} embeddings for clustering: {embeddings_array.shape}")

        return embeddings_array

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
                silhouette = silhouette_score(embeddings, labels, metric='cosine')
                calinski_harabasz = calinski_harabasz_score(embeddings, labels)
                davies_bouldin = davies_bouldin_score(embeddings, labels)

                logger.info(f"Clustering quality metrics:")
                logger.info(f"  Silhouette Score (cosine): {silhouette:.4f}")
                logger.info(f"  Calinski-Harabasz Score (euclidean): {calinski_harabasz:.4f}")
                logger.info(f"  Davies-Bouldin Score (euclidean): {davies_bouldin:.4f}")
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
                metadata=embedded_message.metadata,
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