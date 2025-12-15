#!/usr/bin/env python3

import json
import numpy as np
from datetime import datetime
from shared.logger import get_logger
from ..stages.dendrogram_generator import dendrogram_generator_stage
from ..stages.visualization_generator import visualization_generator_stage
from .helpers import extract_coordinates

logger = get_logger(__name__)

async def generate_dendrograms_and_visualizations(consolidated_result, timestamp, config, output_paths):
    logger.info("< Generating multi-cluster dendrograms and visualizations...")

    cluster_results = consolidated_result.get('cluster_results', {})
    dataset_name = consolidated_result['dataset_name']

    for cluster_count_str in cluster_results.keys():
        result = cluster_results[cluster_count_str]
        # Use the actual cluster_count from result data (handles single-message case where key is "0" but count is 1)
        cluster_count = result.get('cluster_count', int(cluster_count_str))
        clustered_messages = result.get('clustered_messages', [])

        if not clustered_messages:
            logger.warning(f"No clustered messages found for {cluster_count} clusters")
            continue

        logger.info(f"= Generating dendrogram and visualization for {cluster_count} clusters...")

        embeddings = []
        for msg in clustered_messages:
            if hasattr(msg, 'embeddings') and hasattr(msg.embeddings, 'embedding_3072d'):
                embeddings.append(msg.embeddings.embedding_3072d)

        if not embeddings:
            logger.warning(f"No embeddings found for {cluster_count} clusters")
            continue

        embeddings_array = np.array(embeddings)

        dendrogram_config = getattr(config, 'dendrogram', {})
        if isinstance(dendrogram_config, dict):
            dendrogram_enabled = dendrogram_config.get('enabled', True)
        else:
            dendrogram_enabled = getattr(dendrogram_config, 'enabled', True)

        if dendrogram_enabled:
            try:
                hierarchical_config = getattr(config, 'hierarchical', {})
                if isinstance(hierarchical_config, dict):
                    original_n_clusters = hierarchical_config.get('n_clusters', 15)
                    hierarchical_config['n_clusters'] = cluster_count
                else:
                    original_n_clusters = getattr(hierarchical_config, 'n_clusters', 15)
                    hierarchical_config.n_clusters = cluster_count

                dendrogram_result = dendrogram_generator_stage(
                    clustered_messages=clustered_messages,
                    embeddings=embeddings_array,
                    config=config,
                    output_dir=str(output_paths['dendrograms'])
                )

                if isinstance(hierarchical_config, dict):
                    hierarchical_config['n_clusters'] = original_n_clusters
                else:
                    hierarchical_config.n_clusters = original_n_clusters

                logger.info(f" Dendrogram generated for {cluster_count} clusters")
            except Exception as e:
                logger.error(f"L Failed to generate dendrogram for {cluster_count} clusters: {e}")
                if isinstance(hierarchical_config, dict):
                    hierarchical_config['n_clusters'] = original_n_clusters
                else:
                    hierarchical_config.n_clusters = original_n_clusters

        try:
            analyzed_clusters = result.get('analyzed_clusters', [])
            merger_stats = result.get('merger_stats', {'pre_merge_count': 0, 'post_merge_count': 0})

            result_dict = {
                'pipeline_id': f"multi_cluster_{cluster_count}_{timestamp}",
                'data_source': dataset_name,
                'completion_time': datetime.now().isoformat(),
                'stages_completed': ['data_loading', 'filtering', 'processing', 'embedding', 'clustering', 'analysis'],
                'stage_durations': {},
                'total_duration': 0,
                'clustering_algorithm': 'hierarchical',
                'message_counts': {
                    'raw_messages': len(clustered_messages),
                    'filtered_messages': len(clustered_messages),
                    'processed_messages': len(clustered_messages),
                    'atomic_messages': len(clustered_messages),
                    'embedded_messages': len(clustered_messages),
                    'clustered_messages': len(clustered_messages)
                },
                'cluster_statistics': {
                    'total_clusters': cluster_count,
                    'pre_merge_count': merger_stats.get('pre_merge_count', cluster_count),
                    'post_merge_count': merger_stats.get('post_merge_count', len(analyzed_clusters)),
                    'noise_points': 0,
                    'analyzed_clusters': len(analyzed_clusters)
                },
                'cluster_analyses': [
                    {
                        'cluster_id': analysis.cluster_id,
                        'size': analysis.size,
                        'theme': analysis.theme_analysis.theme,
                        'summary': analysis.theme_analysis.summary,
                        'example_messages': analysis.example_messages[:3] if hasattr(analysis, 'example_messages') else []
                    }
                    for analysis in analyzed_clusters
                ],
                'clustered_messages': [
                    {
                        'id': msg.id,
                        'text': msg.text,
                        'cluster_id': msg.cluster_assignment.cluster_id,
                        'cluster_confidence': msg.cluster_assignment.cluster_confidence,
                        'is_noise': msg.cluster_assignment.is_noise,
                        'coordinates': extract_coordinates(msg)
                    }
                    for msg in clustered_messages
                ]
            }

            result_file = output_paths['checkpoints'] / f"temp_result_{cluster_count}clusters_{timestamp}.json"
            with open(result_file, 'w') as f:
                json.dump(result_dict, f, indent=2)

            viz_result = visualization_generator_stage(
                result_file=result_file,
                config=config,
                viz_dir=output_paths['visualizations']
            )

            if result_file.exists():
                result_file.unlink()

            logger.info(f" Visualization generated for {cluster_count} clusters: {viz_result}")
        except Exception as e:
            logger.error(f"L Failed to generate visualization for {cluster_count} clusters: {e}")
            import traceback
            logger.error(f"L Error details: {traceback.format_exc()}")

    logger.info("< Multi-cluster dendrograms and visualizations generation complete!")
