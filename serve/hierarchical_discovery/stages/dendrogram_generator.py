#!/usr/bin/env python3

import numpy as np
import matplotlib.pyplot as plt
import scipy.cluster.hierarchy as sch
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from pathlib import Path
import seaborn as sns
from sklearn.cluster import AgglomerativeClustering

from shared.logger import get_logger
from ..models import ClusteredMessage, PipelineConfig

logger = get_logger(__name__)

class DendrogramGenerator:
    """Generate dendrogram visualizations for hierarchical clustering results"""

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.dendrogram_config = getattr(config, 'dendrogram', {})
        self.output_config = config.output

        # Set matplotlib style
        plt.style.use('default')
        sns.set_palette("husl")

        logger.info("DendrogramGenerator initialized")

    def generate_dendrogram(self,
                          embeddings: np.ndarray,
                          clustered_messages: List[ClusteredMessage],
                          linkage_matrix: Optional[np.ndarray] = None,
                          output_dir: str = "output/dendrograms") -> Dict[str, Any]:
        """
        Generate dendrogram visualization for hierarchical clustering

        Args:
            embeddings: Full-dimensional embeddings used for clustering
            clustered_messages: Messages with cluster assignments
            linkage_matrix: Pre-computed linkage matrix (optional)
            output_dir: Output directory for dendrogram files

        Returns:
            Dictionary with dendrogram metadata and file paths
        """
        if not self.dendrogram_config.get("enabled", True):
            logger.info("Dendrogram generation disabled")
            return {}

        logger.info("=== GENERATING DENDROGRAM VISUALIZATION ===")

        try:
            # Create output directory
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)

            # Compute linkage matrix if not provided
            if linkage_matrix is None:
                logger.info("Computing linkage matrix for dendrogram")
                linkage_matrix = self._compute_linkage_matrix(embeddings, clustered_messages)

            # Generate basic dendrogram
            basic_dendrogram_path = self._generate_basic_dendrogram(
                linkage_matrix, clustered_messages, output_path
            )

            # Generate colored dendrogram
            colored_dendrogram_path = self._generate_colored_dendrogram(
                linkage_matrix, clustered_messages, output_path
            )

            # Generate cluster size dendrogram
            cluster_size_dendrogram_path = self._generate_cluster_size_dendrogram(
                linkage_matrix, clustered_messages, output_path
            )

            # Calculate dendrogram statistics
            stats = self._calculate_dendrogram_stats(linkage_matrix, clustered_messages)

            logger.info("Dendrogram generation complete")

            return {
                "basic_dendrogram": str(basic_dendrogram_path),
                "colored_dendrogram": str(colored_dendrogram_path),
                "cluster_size_dendrogram": str(cluster_size_dendrogram_path),
                "statistics": stats,
                "generation_time": datetime.now().isoformat(),
                "config_used": self.dendrogram_config
            }

        except Exception as e:
            logger.error(f"Failed to generate dendrogram: {e}")
            return {"error": str(e)}

    def _compute_linkage_matrix(self, embeddings: np.ndarray,
                               clustered_messages: List[ClusteredMessage]) -> np.ndarray:
        """Compute linkage matrix for dendrogram generation"""

        # Get clustering parameters from first message
        if not clustered_messages:
            raise ValueError("No clustered messages provided")

        cluster_params = clustered_messages[0].cluster_assignment.clustering_parameters
        linkage = cluster_params.get('linkage', 'ward')
        affinity = cluster_params.get('affinity', 'euclidean')

        logger.info(f"Computing linkage matrix: linkage={linkage}, affinity={affinity}")

        # Ensure compatibility between linkage and affinity
        if linkage == "ward" and affinity != "euclidean":
            logger.warning(f"Ward linkage requires euclidean metric, changing from {affinity}")
            affinity = "euclidean"

        # Use scipy for linkage computation
        if linkage == "ward":
            linkage_matrix = sch.linkage(embeddings, method='ward')
        else:
            linkage_matrix = sch.linkage(embeddings, method=linkage, metric=affinity)

        logger.info(f"Linkage matrix computed: shape={linkage_matrix.shape}")
        return linkage_matrix

    def _generate_basic_dendrogram(self, linkage_matrix: np.ndarray,
                                  clustered_messages: List[ClusteredMessage],
                                  output_path: Path) -> Path:
        """Generate basic dendrogram without colors"""

        figsize = tuple(self.dendrogram_config.get("figsize", [15, 10]))
        plt.figure(figsize=figsize)

        # Generate dendrogram
        sch.dendrogram(
            linkage_matrix,
            orientation=self.dendrogram_config.get("orientation", "top"),
            leaf_rotation=self.dendrogram_config.get("leaf_rotation", 90),
            leaf_font_size=self.dendrogram_config.get("leaf_font_size", 10),
            truncate_mode=self.dendrogram_config.get("truncate_mode", "level"),
            p=self.dendrogram_config.get("p", 30),
            show_leaf_counts=self.dendrogram_config.get("show_leaf_counts", True),
            distance_sort=self.dendrogram_config.get("distance_sort", "descending")
        )

        plt.title("Hierarchical Clustering Dendrogram", fontsize=16, fontweight='bold')
        plt.xlabel("Sample Index or (Cluster Size)", fontsize=12)
        plt.ylabel("Distance", fontsize=12)

        # Add metadata
        n_messages = len(clustered_messages)
        n_clusters = len(set(msg.cluster_assignment.cluster_id for msg in clustered_messages))
        plt.figtext(0.02, 0.02, f"Messages: {n_messages} | Clusters: {n_clusters}",
                   fontsize=10, style='italic')

        plt.tight_layout()

        # Save file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"dendrogram_basic_{timestamp}.png"
        file_path = output_path / filename

        plt.savefig(file_path, dpi=300, bbox_inches='tight')
        plt.close()

        logger.info(f"Basic dendrogram saved: {file_path}")
        return file_path

    def _generate_colored_dendrogram(self, linkage_matrix: np.ndarray,
                                   clustered_messages: List[ClusteredMessage],
                                   output_path: Path) -> Path:
        """Generate colored dendrogram showing cluster assignments"""

        figsize = tuple(self.dendrogram_config.get("figsize", [15, 10]))
        plt.figure(figsize=figsize)

        # Determine color threshold
        color_threshold = self.dendrogram_config.get("color_threshold")
        if color_threshold is None:
            # Auto-determine based on cluster count
            n_clusters = len(set(msg.cluster_assignment.cluster_id for msg in clustered_messages))
            color_threshold = self._calculate_color_threshold(linkage_matrix, n_clusters)

        # Generate colored dendrogram
        sch.dendrogram(
            linkage_matrix,
            orientation=self.dendrogram_config.get("orientation", "top"),
            leaf_rotation=self.dendrogram_config.get("leaf_rotation", 90),
            leaf_font_size=self.dendrogram_config.get("leaf_font_size", 10),
            truncate_mode=self.dendrogram_config.get("truncate_mode", "level"),
            p=self.dendrogram_config.get("p", 30),
            show_leaf_counts=self.dendrogram_config.get("show_leaf_counts", True),
            distance_sort=self.dendrogram_config.get("distance_sort", "descending"),
            color_threshold=color_threshold,
            above_threshold_color='gray'
        )

        plt.title("Hierarchical Clustering Dendrogram (Colored by Clusters)",
                 fontsize=16, fontweight='bold')
        plt.xlabel("Sample Index or (Cluster Size)", fontsize=12)
        plt.ylabel("Distance", fontsize=12)

        # Add metadata and color threshold info
        n_messages = len(clustered_messages)
        n_clusters = len(set(msg.cluster_assignment.cluster_id for msg in clustered_messages))
        plt.figtext(0.02, 0.02,
                   f"Messages: {n_messages} | Clusters: {n_clusters} | Color Threshold: {color_threshold:.3f}",
                   fontsize=10, style='italic')

        plt.tight_layout()

        # Save file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"dendrogram_colored_{timestamp}.png"
        file_path = output_path / filename

        plt.savefig(file_path, dpi=300, bbox_inches='tight')
        plt.close()

        logger.info(f"Colored dendrogram saved: {file_path}")
        return file_path

    def _generate_cluster_size_dendrogram(self, linkage_matrix: np.ndarray,
                                        clustered_messages: List[ClusteredMessage],
                                        output_path: Path) -> Path:
        """Generate dendrogram with cluster size annotations"""

        figsize = tuple(self.dendrogram_config.get("figsize", [15, 10]))
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(figsize[0], figsize[1] + 4))

        # Main dendrogram
        dend = sch.dendrogram(
            linkage_matrix,
            ax=ax1,
            orientation=self.dendrogram_config.get("orientation", "top"),
            leaf_rotation=self.dendrogram_config.get("leaf_rotation", 90),
            leaf_font_size=self.dendrogram_config.get("leaf_font_size", 10),
            truncate_mode=self.dendrogram_config.get("truncate_mode", "level"),
            p=self.dendrogram_config.get("p", 30),
            show_leaf_counts=self.dendrogram_config.get("show_leaf_counts", True),
            distance_sort=self.dendrogram_config.get("distance_sort", "descending")
        )

        ax1.set_title("Hierarchical Clustering Dendrogram with Cluster Size Analysis",
                     fontsize=16, fontweight='bold')
        ax1.set_xlabel("Sample Index or (Cluster Size)", fontsize=12)
        ax1.set_ylabel("Distance", fontsize=12)

        # Cluster size distribution
        cluster_sizes = {}
        for msg in clustered_messages:
            cluster_id = msg.cluster_assignment.cluster_id
            cluster_sizes[cluster_id] = cluster_sizes.get(cluster_id, 0) + 1

        cluster_ids = list(cluster_sizes.keys())
        sizes = list(cluster_sizes.values())

        bars = ax2.bar(cluster_ids, sizes, alpha=0.7, color='skyblue', edgecolor='navy')
        ax2.set_title("Cluster Size Distribution", fontsize=14, fontweight='bold')
        ax2.set_xlabel("Cluster ID", fontsize=12)
        ax2.set_ylabel("Number of Messages", fontsize=12)
        ax2.grid(True, alpha=0.3)

        # Add value labels on bars
        for bar, size in zip(bars, sizes):
            height = bar.get_height()
            ax2.text(bar.get_x() + bar.get_width()/2., height + 0.1,
                    f'{size}', ha='center', va='bottom', fontsize=9)

        # Add summary statistics
        n_messages = len(clustered_messages)
        n_clusters = len(cluster_sizes)
        avg_size = np.mean(sizes)
        max_size = max(sizes)
        min_size = min(sizes)

        stats_text = (f"Total Messages: {n_messages} | Clusters: {n_clusters}\n"
                     f"Avg Size: {avg_size:.1f} | Max: {max_size} | Min: {min_size}")
        fig.text(0.02, 0.02, stats_text, fontsize=10, style='italic')

        plt.tight_layout()

        # Save file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"dendrogram_cluster_sizes_{timestamp}.png"
        file_path = output_path / filename

        plt.savefig(file_path, dpi=300, bbox_inches='tight')
        plt.close()

        logger.info(f"Cluster size dendrogram saved: {file_path}")
        return file_path

    def _calculate_color_threshold(self, linkage_matrix: np.ndarray, n_clusters: int) -> float:
        """Calculate appropriate color threshold for dendrogram coloring"""

        if n_clusters <= 1:
            return linkage_matrix[-1, 2] / 2

        # Get the distance at which we would have n_clusters
        if n_clusters > len(linkage_matrix):
            return 0

        # The linkage matrix is sorted by merge distance
        # To get n_clusters, we need the (n-n_clusters)th merge distance
        merge_index = len(linkage_matrix) - n_clusters
        threshold = linkage_matrix[merge_index, 2]

        # Add small buffer to ensure proper coloring
        threshold *= 1.01

        logger.info(f"Calculated color threshold: {threshold:.4f} for {n_clusters} clusters")
        return threshold

    def _calculate_dendrogram_stats(self, linkage_matrix: np.ndarray,
                                  clustered_messages: List[ClusteredMessage]) -> Dict[str, Any]:
        """Calculate statistics about the dendrogram and clustering"""

        cluster_sizes = {}
        for msg in clustered_messages:
            cluster_id = msg.cluster_assignment.cluster_id
            cluster_sizes[cluster_id] = cluster_sizes.get(cluster_id, 0) + 1

        sizes = list(cluster_sizes.values())

        # Calculate cophenetic correlation coefficient
        from scipy.spatial.distance import pdist
        from scipy.stats import pearsonr

        # This would require the original distance matrix, so we'll skip for now
        # cophenetic_corr = sch.cophenet(linkage_matrix, pdist(embeddings))[0]

        stats = {
            "n_messages": len(clustered_messages),
            "n_clusters": len(cluster_sizes),
            "cluster_sizes": {
                "mean": float(np.mean(sizes)),
                "std": float(np.std(sizes)),
                "min": int(min(sizes)),
                "max": int(max(sizes)),
                "median": float(np.median(sizes))
            },
            "dendrogram_info": {
                "n_merges": len(linkage_matrix),
                "max_distance": float(linkage_matrix[-1, 2]),
                "min_distance": float(linkage_matrix[0, 2]),
                "distance_range": float(linkage_matrix[-1, 2] - linkage_matrix[0, 2])
            }
        }

        return stats


def dendrogram_generator_stage(clustered_messages: List[ClusteredMessage],
                              embeddings: np.ndarray,
                              config: PipelineConfig,
                              output_dir: str = "output/dendrograms") -> Dict[str, Any]:
    """
    Stage function for generating dendrogram visualizations

    Args:
        clustered_messages: Messages with cluster assignments
        embeddings: Full-dimensional embeddings used for clustering
        config: Pipeline configuration
        output_dir: Output directory for dendrogram files

    Returns:
        Dictionary with dendrogram metadata and file paths
    """
    logger.info("=== DENDROGRAM GENERATION STAGE ===")

    if not clustered_messages:
        logger.warning("No clustered messages provided for dendrogram generation")
        return {"error": "No clustered messages"}

    # Create dendrogram generator and generate visualizations
    generator = DendrogramGenerator(config)
    result = generator.generate_dendrogram(
        embeddings=embeddings,
        clustered_messages=clustered_messages,
        output_dir=output_dir
    )

    logger.info("=== DENDROGRAM GENERATION STAGE COMPLETED ===")

    return result