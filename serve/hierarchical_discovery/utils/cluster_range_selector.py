#!/usr/bin/env python3

from typing import List, Optional
import numpy as np
from shared.logger import get_logger

logger = get_logger(__name__)

def determine_optimal_k(
    dataset_size: int,
    embeddings: np.ndarray,
    config,
    output_paths: dict
) -> int:
    """Determine the optimal number of clusters using data-driven selection."""

    if embeddings is None:
        raise ValueError("Embeddings required for optimal k selection.")

    logger.info("=" * 60)
    logger.info("OPTIMAL K SELECTION")
    logger.info("=" * 60)

    from serve.hierarchical_discovery.find_optimal_k import OptimalKFinder

    hierarchical_config = getattr(config, 'hierarchical', {})
    optimal_k_config = hierarchical_config.get('optimal_k_config', {})

    # Handle edge cases for very tiny datasets
    if dataset_size <= 1:
        logger.warning(f"Dataset too small ({dataset_size} messages) - returning single cluster")
        return 1
    elif dataset_size == 2:
        logger.warning(f"Dataset too small ({dataset_size} messages) - returning 2 clusters")
        return 2
    elif dataset_size == 3:
        logger.warning(f"Dataset very small ({dataset_size} messages) - returning 2 clusters")
        return 2
    elif dataset_size < 15:
        min_k = 2
        max_k = min(dataset_size - 1, 8)
    elif dataset_size < 50:
        min_k = 3
        max_k = min(dataset_size // 2, 15)
    else:
        min_k = optimal_k_config.get('min_k', 5)
        max_k = optimal_k_config.get('max_k', 50)

    logger.info(f"Dataset size: {dataset_size} messages")
    logger.info(f"Adaptive k range: [{min_k}, {max_k}]")

    max_bw_ratio = optimal_k_config.get('max_bw_ratio', 1000.0)
    zero_epsilon = optimal_k_config.get('zero_epsilon', 1.0e-10)

    finder = OptimalKFinder(
        embeddings,
        min_k=min_k,
        max_k=max_k,
        max_bw_ratio=max_bw_ratio,
        zero_epsilon=zero_epsilon
    )
    finder.compute_linkage(method='complete', metric='cosine')
    finder.test_k_values()

    if dataset_size < 15:
        max_cv = 2.0
        min_silhouette = 0.05
        min_bw_ratio = 0.05
        logger.info("Using highly relaxed constraints for very small dataset")
    elif dataset_size < 50:
        max_cv = 1.5
        min_silhouette = 0.2
        min_bw_ratio = 0.3
        logger.info("Using moderate constraints for small dataset")
    else:
        max_cv = optimal_k_config.get('max_cv', 1.0)
        min_silhouette = 0.3
        min_bw_ratio = 0.5
        logger.info("Using standard constraints for normal dataset")

    logger.info(f"Constraints: max_cv={max_cv}, min_silhouette={min_silhouette}, min_bw_ratio={min_bw_ratio}")

    valid_results = finder.apply_constraints(
        max_cv=max_cv,
        min_silhouette=min_silhouette,
        min_bw_ratio=min_bw_ratio
    )

    optimal_k, reasoning = finder.recommend_optimal_k(valid_results)

    if not optimal_k:
        logger.error("Optimal k finder failed to find valid k value!")
        # Fallback to a reasonable default
        optimal_k = min(15, max(5, dataset_size // 10))
        logger.warning(f"Using fallback k={optimal_k}")

    logger.info("=" * 60)
    logger.info(f"OPTIMAL K SELECTED: {optimal_k}")
    logger.info("=" * 60)
    logger.info(f"\n{reasoning}\n")

    dataset_name = config.data_source
    finder.plot_results(output_paths['reports'], valid_results, dataset_name=dataset_name,
                      gap_results=finder.gap_results, stability_scores=finder.stability_scores)
    finder.save_report(output_paths['reports'], optimal_k, reasoning, dataset_name=dataset_name)

    logger.info(f"Optimal k analysis saved to: {output_paths['reports']}")
    logger.info("=" * 60)

    return optimal_k
