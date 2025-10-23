#!/usr/bin/env python3

import numpy as np
from typing import List, Dict, Any
from datetime import datetime
from sklearn.decomposition import PCA
import umap

from shared.logger import get_logger
from ..models import EmbeddedMessage, PipelineConfig

logger = get_logger(__name__)

class DimensionalityReducer:
    """Reduces embedding dimensions using PCA and UMAP"""

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.reduction_config = getattr(config, 'dimensionality_reduction', {})

        self.pca_enabled = self.reduction_config.get('pca_enabled', True)
        self.pca_dimensions = self.reduction_config.get('pca_dimensions', 50)

        self.umap_enabled = self.reduction_config.get('umap_enabled', True)
        self.umap_dimensions = self.reduction_config.get('umap_dimensions', 8)
        self.umap_n_neighbors = self.reduction_config.get('umap_n_neighbors', 15)
        self.umap_min_dist = self.reduction_config.get('umap_min_dist', 0.1)
        self.umap_metric = self.reduction_config.get('umap_metric', 'euclidean')
        self.umap_3d_visualization = self.reduction_config.get('umap_3d_visualization', False)

        logger.info(f"DimensionalityReducer initialized")
        logger.info(f"  PCA: {self.pca_enabled} ({self.pca_dimensions}d)")
        logger.info(f"  UMAP: {self.umap_enabled} ({self.umap_dimensions}d for clustering)")
        if self.umap_3d_visualization:
            logger.info(f"  UMAP 3D visualization: enabled")

    def reduce_embeddings(self, embedded_messages: List[EmbeddedMessage]) -> List[EmbeddedMessage]:
        """Apply dimensionality reduction to embedded messages"""

        if not embedded_messages:
            logger.warning("No embedded messages to reduce")
            return []

        embeddings_3072d = []
        for msg in embedded_messages:
            if hasattr(msg.embeddings, 'embedding_3072d') and msg.embeddings.embedding_3072d is not None:
                embeddings_3072d.append(msg.embeddings.embedding_3072d)
            else:
                raise ValueError(f"Message {msg.id} missing 3072d embedding")

        embeddings_array = np.array(embeddings_3072d)
        logger.info(f"Starting reduction on {embeddings_array.shape[0]} embeddings (3072d)")

        embeddings_pca = None
        if self.pca_enabled:
            embeddings_pca = self._apply_pca(embeddings_array)
        else:
            embeddings_pca = embeddings_array

        embeddings_umap = None
        embeddings_umap_3d = None

        if self.umap_enabled:
            embeddings_umap = self._apply_umap(embeddings_pca, len(embedded_messages), target_dims=self.umap_dimensions)

        if self.umap_3d_visualization:
            embeddings_umap_3d = self._apply_umap(embeddings_pca, len(embedded_messages), target_dims=3)

        for i, msg in enumerate(embedded_messages):
            if embeddings_pca is not None and self.pca_dimensions == 50:
                msg.embeddings.embedding_50d = embeddings_pca[i]

            if embeddings_umap is not None:
                msg.embeddings.embedding_umap = embeddings_umap[i]

            if embeddings_umap_3d is not None:
                msg.embeddings.embedding_3d = embeddings_umap_3d[i]

        logger.info(f"Dimensionality reduction complete")
        return embedded_messages

    def _apply_pca(self, embeddings: np.ndarray) -> np.ndarray:
        """Apply PCA reduction"""
        n_samples = embeddings.shape[0]
        n_features = embeddings.shape[1]
        max_components = min(n_samples, n_features)

        pca_dims = self.pca_dimensions
        if pca_dims > max_components:
            logger.warning(f"PCA dims ({pca_dims}) exceed max ({max_components}). Using {max_components - 1}")
            pca_dims = max_components - 1

        logger.info(f"  Applying PCA: {embeddings.shape[1]}d → {pca_dims}d")

        pca = PCA(n_components=pca_dims, random_state=42)
        reduced = pca.fit_transform(embeddings)

        variance = sum(pca.explained_variance_ratio_) * 100
        logger.info(f"  PCA complete: {reduced.shape}, {variance:.1f}% variance preserved")

        return reduced

    def _apply_umap(self, embeddings: np.ndarray, dataset_size: int, target_dims: int = None) -> np.ndarray:
        """Apply UMAP reduction with L2 normalization for cosine distance"""
        if dataset_size < 10:
            logger.warning(f"Dataset too small for UMAP ({dataset_size} < 10). Skipping UMAP.")
            return None

        if target_dims is None:
            target_dims = self.umap_dimensions

        # Adjust n_neighbors for small datasets (must be < dataset_size)
        n_neighbors = min(self.umap_n_neighbors, dataset_size - 1)
        if n_neighbors < self.umap_n_neighbors:
            logger.info(f"  Adjusting UMAP n_neighbors: {self.umap_n_neighbors} → {n_neighbors}")

        # For small datasets (< 15), cap output dimensions safely
        # UMAP spectral initialization fails when n_components is too close to n_samples
        # Use max(n//2, 8) to ensure stable initialization
        if dataset_size < 15:
            max_dims = max(dataset_size // 2, 8)
            if target_dims > max_dims:
                logger.info(f"  Adjusting UMAP dimensions for small dataset: {target_dims}d → {max_dims}d")
                target_dims = max_dims

        logger.info(f"  Applying UMAP: {embeddings.shape[1]}d → {target_dims}d")

        # For very small datasets, use random initialization instead of spectral
        # to avoid scipy.linalg.eigh errors when k >= N
        init_method = 'spectral'
        if dataset_size < 15:
            init_method = 'random'
            logger.info(f"  Using random initialization for small dataset")

        umap_model = umap.UMAP(
            n_components=target_dims,
            n_neighbors=n_neighbors,
            min_dist=self.umap_min_dist,
            metric=self.umap_metric,
            init=init_method,
            verbose=False,
            n_jobs=4
        )

        reduced = umap_model.fit_transform(embeddings)

        logger.info(f"  UMAP complete: {reduced.shape}")

        return reduced

def dimensionality_reduction_stage(embedded_messages: List[EmbeddedMessage],
                                   config: PipelineConfig) -> List[EmbeddedMessage]:
    """Main entry point for dimensionality reduction stage"""
    logger.info("=== DIMENSIONALITY REDUCTION STAGE ===")

    reducer = DimensionalityReducer(config)
    return reducer.reduce_embeddings(embedded_messages)
