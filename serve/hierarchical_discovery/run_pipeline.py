#!/usr/bin/env python3

"""
Simple runner script for the Hierarchical Clustering Civic Message Discovery Pipeline
"""

import sys
import argparse
import warnings
from pathlib import Path

# Suppress specific sklearn deprecation warnings that are out of our control
warnings.filterwarnings("ignore", message=".*force_all_finite.*", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*n_jobs value.*overridden.*", category=UserWarning)

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent.parent))

from serve.hierarchical_discovery.orchestrator import run_hierarchical_discovery_pipeline
from shared.logger import get_logger

logger = get_logger(__name__)

def main():
    parser = argparse.ArgumentParser(description="Run Hierarchical Clustering Civic Message Discovery Pipeline")
    parser.add_argument(
        "--config",
        type=str,
        default=str(Path(__file__).parent / "config.yaml"),
        help="Path to configuration file"
    )
    parser.add_argument(
        "--data-source",
        type=str,
        choices=["josh", "cara", "berkeley", "heather", "japjeet", "joanna", "jonathan", "all"],
        help="Override data source from config"
    )
    parser.add_argument(
        "--disable-optimization",
        action="store_true",
        help="Disable Optuna parameter optimization"
    )
    parser.add_argument(
        "--quick-test",
        action="store_true",
        help="Run with reduced settings for testing"
    )
    parser.add_argument(
        "--anonymize-keywords",
        type=str,
        nargs="*",
        help="Keywords to anonymize during AI summarization (e.g. --anonymize-keywords Minooka 'Kendall County')"
    )

    args = parser.parse_args()

    # Validate config file exists
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    # Modify config for quick test
    if args.quick_test:
        logger.info("Running in quick test mode")
        # You could modify config here for faster testing

    logger.info(f"Starting hierarchical clustering pipeline with config: {config_path}")

    try:
        result = run_hierarchical_discovery_pipeline(str(config_path), data_source=args.data_source, anonymize_keywords=args.anonymize_keywords, disable_optimization=args.disable_optimization)

        # Print summary
        print(f"\n{'='*60}")
        print(f"HIERARCHICAL CLUSTERING PIPELINE COMPLETED SUCCESSFULLY!")
        print(f"{'='*60}")

        # Handle different return types (PipelineResult vs multi-cluster dict)
        if isinstance(result, dict):
            # Multi-cluster mode
            print(f"Multi-cluster analysis completed")
            print(f"Dataset: {result['dataset_name']}")
            print(f"Total Messages: {result['total_messages']:,}")
            print(f"Cluster Ranges: {', '.join(result['cluster_ranges'])}")
            print(f"Pipeline State: Available in cluster_results")
        else:
            # Single cluster mode (PipelineResult object)
            print(f"Pipeline ID: {result.pipeline_state.pipeline_id}")
            print(f"Data Source: {result.pipeline_state.config.data_source}")
            print(f"Raw Messages: {len(result.raw_messages):,}")
            print(f"Filtered Messages: {len([m for m in result.filtered_messages if m.filter_result.passed]):,}")
            print(f"Atomic Messages: {len(result.atomic_messages):,}")
            print(f"Clusters Found: {result.pipeline_state.total_clusters}")
            print(f"Total Duration: {sum(result.pipeline_state.stage_durations.values()):.1f}s")
            print(f"Output Directory: {result.pipeline_state.output_dir}")

            # Show top themes
            if result.cluster_analyses:
                print(f"\nTop 5 Civic Themes Discovered:")
                for i, analysis in enumerate(result.cluster_analyses[:5], 1):
                    print(f"  {i}. {analysis.theme_analysis.theme} ({analysis.size} messages)")

        print(f"{'='*60}")

    except Exception as e:
        logger.error(f"Hierarchical clustering pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()