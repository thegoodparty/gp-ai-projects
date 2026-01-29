#!/usr/bin/env python3

import asyncio
import copy
import json
import numpy as np
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
import traceback

from shared.logger import get_logger
from .models import PipelineState, PipelineResult
from .stages.data_loader import load_data_stage
from .stages.content_filter import content_filter_stage
from .stages.ai_message_processor import ai_message_processor_stage
from .stages.embedding_generator import embedding_generator_stage_sync
from .stages.dimensionality_reducer import dimensionality_reduction_stage
from .stages.hierarchical_cluster_engine import hierarchical_cluster_engine_stage
from .stages.multi_cluster_analyzer import multi_cluster_analyzer_stage
from .stages.visualization_generator import visualization_generator_stage

from .utils import (
    extract_coordinates,
    load_config,
    setup_output_directories,
    generate_cost_summary,
    analyze_single_message,
    determine_optimal_k,
    compute_fallback_k,
    create_multi_cluster_output,
    create_single_message_output,
    export_multi_cluster_results,
    generate_dendrograms_and_visualizations,
    generate_multi_cluster_report,
    generate_summary_report
)

logger = get_logger(__name__)

class HierarchicalDiscoveryOrchestrator:
    """Main orchestrator for the hierarchical clustering civic message discovery pipeline"""

    def __init__(self, config_path: str = "serve/hierarchical_discovery/config.yaml", data_source_override: Optional[str] = None):
        self.config_path = Path(config_path)
        self.config = load_config(self.config_path)

        if data_source_override:
            logger.info(f"Overriding config data source '{self.config.data_source}' with '{data_source_override}'")
            self.config.data_source = data_source_override

        self.pipeline_state = PipelineState(config=self.config)
        self.optimization_results = None

        discovery_dir = Path(__file__).parent
        self.output_paths = setup_output_directories(self.config, discovery_dir)

        self.pipeline_state.output_dir = str(self.output_paths['base'])
        self.pipeline_state.checkpoint_dir = str(self.output_paths['checkpoints'])

        logger.info(f"HierarchicalDiscoveryOrchestrator initialized")
        logger.info(f"Data source: {self.config.data_source}")
        logger.info(f"Pipeline ID: {self.pipeline_state.pipeline_id}")


    def _update_pipeline_state(self, stage_name: str, duration: float, **kwargs):
        """Update pipeline state after stage completion"""
        self.pipeline_state.stages_completed.append(stage_name)
        self.pipeline_state.stage_durations[stage_name] = duration
        self.pipeline_state.current_stage = ""

        # Update counts if provided
        for key, value in kwargs.items():
            if hasattr(self.pipeline_state, key):
                setattr(self.pipeline_state, key, value)

        logger.info(f"Stage '{stage_name}' completed in {duration:.2f}s")

    def _handle_stage_error(self, stage_name: str, error: Exception):
        """Handle stage execution errors"""
        error_msg = f"Stage '{stage_name}' failed: {str(error)}"
        self.pipeline_state.errors.append(error_msg)
        self.pipeline_state.current_stage = f"error_{stage_name}"

        logger.error(error_msg)
        logger.error(f"Traceback: {traceback.format_exc()}")

        # Save error checkpoint
        self._save_checkpoint(f"error_{stage_name}")

    def _save_checkpoint(self, stage_name: str):
        """Save pipeline checkpoint"""
        checkpoints_config = getattr(self.config, 'checkpoints', {'enabled': True})
        if not checkpoints_config.get('enabled', True):
            return

        try:
            checkpoint_file = self.output_paths['checkpoints'] / f"checkpoint_{stage_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

            checkpoint_data = {
                'pipeline_id': self.pipeline_state.pipeline_id,
                'stage': stage_name,
                'config': self.config.__dict__,
                'state': self.pipeline_state.__dict__,
                'timestamp': datetime.now().isoformat()
            }

            with open(checkpoint_file, 'w') as f:
                json.dump(checkpoint_data, f, indent=2, default=str)

            logger.info(f"Checkpoint saved: {checkpoint_file}")

        except Exception as e:
            logger.warning(f"Failed to save checkpoint: {e}")

    async def run_pipeline(self, disable_optimization: bool = False, return_data: bool = False, in_memory_messages: Optional[List] = None) -> Dict[str, Any]:
        """Run the hierarchical clustering discovery pipeline

        Args:
            disable_optimization: Whether to disable optimizations
            return_data: Whether to return data objects instead of writing files
            in_memory_messages: Optional list of RawMessage objects to process (skips CSV loading)
        """
        logger.debug("Entering run_pipeline method")

        logger.info(f"🚀 STARTING HIERARCHICAL DISCOVERY PIPELINE")
        logger.info("=" * 60)

        # Run common pipeline stages once (data loading through embedding)
        embedded_messages = await self._run_common_stages(in_memory_messages)

        # Check if clustering was skipped due to insufficient messages
        if not embedded_messages:
            logger.warning("Clustering skipped - no atomic messages")
            logger.info("Returning empty multi-cluster results")
            return {
                'messages': [],
                'cluster_results': {},
                'pipeline_state': self.pipeline_state,
                'dataset_name': self.config.data_source,
                'total_messages': 0
            }

        if len(embedded_messages) == 1 and hasattr(embedded_messages[0], 'single_message_theme'):
            logger.info("Processing single-message analysis result")
            single_msg_result = create_single_message_output(embedded_messages[0], self.pipeline_state, self.config.data_source)

            if not return_data:
                logger.info("Generating single-message exports and visualizations...")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

                await export_multi_cluster_results(single_msg_result, self.output_paths)
                await generate_dendrograms_and_visualizations(single_msg_result, timestamp, self.config, self.output_paths)
                await generate_multi_cluster_report(single_msg_result, timestamp, self.output_paths)

                logger.info("Single-message exports completed")

            return single_msg_result


        # Extract best available embeddings (UMAP → PCA → Full)
        def extract_best_embedding(msg):
            if not hasattr(msg, 'embeddings'):
                logger.warning(f"Message {msg.id} missing embeddings attribute")
                return None

            emb = msg.embeddings
            if hasattr(emb, 'embedding_umap') and emb.embedding_umap is not None:
                return ('UMAP 15d', emb.embedding_umap)
            elif hasattr(emb, 'embedding_50d') and emb.embedding_50d is not None:
                return ('PCA 50d', emb.embedding_50d)
            elif hasattr(emb, 'embedding_3072d') and emb.embedding_3072d is not None:
                return ('3072d full', emb.embedding_3072d)
            else:
                logger.warning(f"Message {msg.id} missing all embeddings")
                return None

        results = [extract_best_embedding(msg) for msg in embedded_messages]
        results = [r for r in results if r is not None]

        if not results:
            embeddings_array = None
            embedding_source = None
        else:
            embedding_source = results[0][0]
            embeddings_list = [r[1] for r in results]
            embeddings_array = np.array(embeddings_list)

        if embeddings_array is not None:
            logger.info(f"Using {embedding_source} embeddings for optimal k selection: {embeddings_array.shape}")

        # Count substantive messages (those with embeddings) for optimal k
        # Opt-out messages don't participate in clustering, so don't count them
        substantive_count = len([m for m in embedded_messages if not getattr(m, 'is_opt_out', False)])
        opt_out_count = len(embedded_messages) - substantive_count
        if opt_out_count > 0:
            logger.info(f"Dataset: {substantive_count} substantive messages + {opt_out_count} opt-out (pass-through)")

        # Handle case where ALL messages are opt-outs
        if substantive_count == 0:
            logger.warning("All messages are non-substantive (opt-out) - skipping clustering")
            # Return all messages as pass-through with no cluster assignments
            consolidated_result = create_multi_cluster_output(
                {},  # No cluster results
                embedded_messages,
                self.pipeline_state,
                self.config.data_source
            )
            return consolidated_result

        # Determine optimal number of clusters
        dataset_size = substantive_count
        try:
            n_clusters = determine_optimal_k(dataset_size, embeddings_array, self.config, self.output_paths)
        except Exception as e:
            logger.error(f"Optimal k selection failed: {e}")
            n_clusters = compute_fallback_k(dataset_size)
            logger.warning(f"Using fallback k={n_clusters}")

        logger.info(f"Selected cluster count: {n_clusters}")
        logger.info("=" * 60)

        # Run hierarchical clustering
        logger.info(f"🔄 Running hierarchical clustering with {n_clusters} clusters...")
        temp_config = copy.deepcopy(self.config)
        temp_config.hierarchical['n_clusters'] = n_clusters
        temp_config.hierarchical['distance_threshold'] = None

        clustered_messages = hierarchical_cluster_engine_stage(embedded_messages, temp_config)
        actual_cluster_count = len(set(msg.cluster_assignment.cluster_id for msg in clustered_messages))

        cluster_results = {
            str(n_clusters): {
                'clustered_messages': clustered_messages,
                'n_clusters': n_clusters,
                'cluster_count': actual_cluster_count
            }
        }

        # Run cluster theme analysis
        logger.info(f"🎯 Running cluster theme analysis...")
        multi_analysis_result = await multi_cluster_analyzer_stage(cluster_results, self.config)

        # Extract cost data and merger stats from cluster analysis
        merger_stats_dict = {}
        if isinstance(multi_analysis_result, dict) and 'cost' in multi_analysis_result:
            stage_cost = multi_analysis_result.get("cost", 0)
            usage_stats = multi_analysis_result.get("usage_stats", {})
            merger_stats_dict = multi_analysis_result.get("merger_stats", {})
            self.pipeline_state.total_cost += stage_cost
            self.pipeline_state.stage_costs["cluster_analysis"] = stage_cost
            self.pipeline_state.gemini_usage["cluster_analysis"] = usage_stats

            logger.info(f"💰 Cluster Analysis Cost: ${stage_cost:.4f} (Total: ${self.pipeline_state.total_cost:.4f})")
            analyzed_results = multi_analysis_result["analyses"]
        else:
            analyzed_results = multi_analysis_result

        # Combine clustering and analysis results
        cluster_key = str(n_clusters)
        final_results = {
            cluster_key: {
                'clustered_messages': clustered_messages,
                'analyzed_clusters': analyzed_results.get(cluster_key, []),
                'n_clusters': n_clusters,
                'cluster_count': actual_cluster_count,
                'merger_stats': merger_stats_dict.get(cluster_key, {'pre_merge_count': 0, 'post_merge_count': 0})
            }
        }

        consolidated_result = create_multi_cluster_output(
            final_results, embedded_messages, self.pipeline_state, self.config.data_source
        )

        if not return_data:
            logger.info("🚀 Exporting results...")

            await export_multi_cluster_results(consolidated_result, self.output_paths)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            await generate_dendrograms_and_visualizations(
                consolidated_result, timestamp, self.config, self.output_paths
            )
            await generate_multi_cluster_report(consolidated_result, timestamp, self.output_paths)

            logger.info("✅ Completed exports")
        else:
            logger.info("🔄 Skipping CSV export and visualizations (return_data=True)")

        logger.info("🎯 Pipeline completed successfully")

        return consolidated_result

    async def _run_common_stages(self, in_memory_messages: Optional[List] = None):
        """Run data loading, filtering, AI processing, and embedding stages

        Args:
            in_memory_messages: Optional list of RawMessage objects to process (skips CSV loading)
        """

        # Stage 1: Data Loading
        start_time = datetime.now()
        self.pipeline_state.current_stage = "data_loading"

        if in_memory_messages:
            logger.info("Using in-memory messages (skipping CSV data loading)")
            raw_messages = in_memory_messages
        else:
            raw_messages = load_data_stage(self.config)

        duration = (datetime.now() - start_time).total_seconds()
        self._update_pipeline_state("data_loading", duration, raw_messages_count=len(raw_messages))

        if not raw_messages:
            raise ValueError("No messages loaded from data sources")

        # Stage 2: Content Filtering
        start_time = datetime.now()
        self.pipeline_state.current_stage = "filtering"

        filtered_messages = content_filter_stage(raw_messages, self.config)
        passed_messages = [msg for msg in filtered_messages if msg.filter_result.passed]

        duration = (datetime.now() - start_time).total_seconds()
        self._update_pipeline_state("filtering", duration, filtered_messages_count=len(passed_messages))

        # Stage 3: AI Message Processing (with automatic location normalization)
        start_time = datetime.now()
        self.pipeline_state.current_stage = "ai_processing"

        atomic_result = await ai_message_processor_stage(filtered_messages, self.config)

        if isinstance(atomic_result, dict):
            atomic_messages = atomic_result["messages"]
            stage_cost = atomic_result.get("cost", 0)
            usage_stats = atomic_result.get("usage_stats", {})
            self.pipeline_state.total_cost += stage_cost
            self.pipeline_state.stage_costs["ai_processing"] = stage_cost
            self.pipeline_state.gemini_usage["ai_processing"] = usage_stats
        else:
            atomic_messages = atomic_result

        duration = (datetime.now() - start_time).total_seconds()
        self._update_pipeline_state("ai_processing", duration, atomic_messages_count=len(atomic_messages))

        # Check if we have zero atomic messages
        if len(atomic_messages) < 1:
            logger.warning("No atomic messages after AI processing - returning empty results")
            return []

        # Handle single message case AFTER embedding generation
        single_message_detected = len(atomic_messages) == 1

        # Stage 4: Embedding Generation
        start_time = datetime.now()
        self.pipeline_state.current_stage = "embedding"

        embedding_result = embedding_generator_stage_sync(atomic_messages, self.config)

        if isinstance(embedding_result, dict):
            embedded_messages = embedding_result["messages"]
            stage_cost = embedding_result.get("cost", 0)
            usage_stats = embedding_result.get("usage_stats", {})
            self.pipeline_state.total_cost += stage_cost
            self.pipeline_state.stage_costs["embedding"] = stage_cost
            self.pipeline_state.gemini_usage["embedding"] = usage_stats
        else:
            embedded_messages = embedding_result

        duration = (datetime.now() - start_time).total_seconds()
        self._update_pipeline_state("embedding", duration, embedded_messages_count=len(embedded_messages))

        # Stage 4.5: Dimensionality Reduction (NEW STAGE)
        start_time = datetime.now()
        self.pipeline_state.current_stage = "dimensionality_reduction"

        embedded_messages = dimensionality_reduction_stage(embedded_messages, self.config)

        duration = (datetime.now() - start_time).total_seconds()
        self._update_pipeline_state("dimensionality_reduction", duration)

        # Handle single message case - analyze and mark it
        if single_message_detected:
            logger.info("Single message detected - performing LLM-based analysis")
            analyzed_message = await analyze_single_message(embedded_messages[0], self.pipeline_state)
            # Copy embeddings from the embedded message to the analyzed message
            analyzed_message.embeddings = embedded_messages[0].embeddings
            return [analyzed_message]

        return embedded_messages


def run_hierarchical_discovery_pipeline(config_path: str = "serve/hierarchical_discovery/config.yaml", data_source: Optional[str] = None, disable_optimization: bool = False, return_data: bool = False) -> PipelineResult:
    """Main entry point for running the hierarchical discovery pipeline"""
    logger.debug(f"Entering run_hierarchical_discovery_pipeline with config_path: {config_path}")

    orchestrator = HierarchicalDiscoveryOrchestrator(config_path, data_source_override=data_source)

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    return loop.run_until_complete(orchestrator.run_pipeline(disable_optimization=disable_optimization, return_data=return_data))

if __name__ == "__main__":
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else "serve/hierarchical_discovery/config.yaml"

    logger.info(f"Starting Hierarchical Clustering Civic Message Discovery Pipeline with config: {config_path}")

    try:
        result = run_hierarchical_discovery_pipeline(config_path)
        logger.info(f"Pipeline completed successfully! Pipeline ID: {result.pipeline_state.pipeline_id}")

        # Print key statistics
        print(f"\n{'='*60}")
        print(f"HIERARCHICAL CLUSTERING CIVIC MESSAGE DISCOVERY PIPELINE - RESULTS SUMMARY")
        print(f"{'='*60}")
        print(f"Pipeline ID: {result.pipeline_state.pipeline_id}")
        print(f"Data Source: {result.pipeline_state.config.data_source}")
        print(f"Total Messages Processed: {len(result.raw_messages):,}")
        print(f"Clusters Discovered: {result.pipeline_state.total_clusters}")
        print(f"Total Duration: {sum(result.pipeline_state.stage_durations.values()):.1f} seconds")
        print(f"Output Directory: {result.pipeline_state.output_dir}")
        print(f"{'='*60}")

    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        sys.exit(1)