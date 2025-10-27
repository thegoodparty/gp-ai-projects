#!/usr/bin/env python3

"""
V1 Message Analysis Pipeline Runner

Run the complete pipeline for processing campaign messages through consolidation,
clustering, and event publishing.

Usage:
    uv run serve/v1_pipeline/scripts/run_pipeline.py --campaign berkley
    uv run serve/v1_pipeline/scripts/run_pipeline.py --campaign berkley --config custom_config.yaml
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from serve.v1_pipeline.pipeline.orchestrator import V1PipelineOrchestrator
from shared.logger import get_logger

logger = get_logger(__name__)


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description='V1 Message Analysis Pipeline Runner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run pipeline for Berkley campaign
  %(prog)s --campaign berkley

  # Use custom configuration
  %(prog)s --campaign berkley --config /path/to/config.yaml

  # Skip clustering stage
  %(prog)s --campaign berkley --skip-clustering

  # Enable debug logging
  %(prog)s --campaign berkley --debug
        """
    )

    parser.add_argument(
        '--campaign',
        type=str,
        required=True,
        help='Campaign name to process (e.g., "berkley", "cara", "josh")'
    )

    parser.add_argument(
        '--config',
        type=str,
        help='Path to pipeline configuration file'
    )

    parser.add_argument(
        '--skip-clustering',
        action='store_true',
        help='Skip clustering stage'
    )

    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug logging'
    )

    parser.add_argument(
        '--output-dir',
        type=str,
        help='Override output directory'
    )

    parser.add_argument(
        '--save-results',
        type=str,
        help='Save pipeline results to JSON file'
    )

    return parser.parse_args()


def setup_logging(debug: bool = False):
    """Setup logging configuration"""
    import logging

    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Reduce noise from external libraries
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    logging.getLogger('aiohttp').setLevel(logging.WARNING)


def modify_config_for_arguments(config_path: str | None, args) -> str | None:
    """Modify configuration based on command line arguments"""
    if not config_path:
        # Use default config
        config_path = str(Path(__file__).parent.parent / "config/pipeline_config.yaml")

    # If we need to modify config, create a temporary one
    modifications = {}

    if args.skip_clustering:
        modifications['clustering'] = {'enabled': False}

    if args.output_dir:
        modifications['consolidation'] = {'output_dir': args.output_dir}

    # If no modifications needed, return original config
    if not modifications:
        return config_path

    # Create temporary config with modifications
    import tempfile

    import yaml

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)

        # Apply modifications
        for section, updates in modifications.items():
            if section in config:
                config[section].update(updates)
            else:
                config[section] = updates

        # Create temporary config file
        temp_config = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False)
        yaml.dump(config, temp_config)
        temp_config.close()

        logger.debug(f"Created temporary config with modifications: {temp_config.name}")
        return temp_config.name

    except Exception as e:
        logger.error(f"Failed to modify config: {e}")
        return config_path


async def main():
    """Main execution function"""
    args = parse_arguments()

    # Setup logging
    setup_logging(args.debug)

    logger.info("🚀 V1 Message Analysis Pipeline Starting")
    logger.info(f"Campaign: {args.campaign}")

    try:
        # Modify config based on arguments
        config_path = modify_config_for_arguments(args.config, args)

        # Initialize orchestrator
        orchestrator = V1PipelineOrchestrator(config_path)

        # Log configuration
        if args.skip_clustering:
            logger.info("⏭️ Skipping clustering stage")

        # Run pipeline
        result = await orchestrator.run_pipeline(args.campaign)

        # Print results summary
        print("\n" + "="*60)
        print("📊 PIPELINE RESULTS SUMMARY")
        print("="*60)

        summary = result.summary
        for key, value in summary.items():
            print(f"{key.replace('_', ' ').title()}: {value}")

        print("="*60)

        # Save results if requested
        if args.save_results:
            results_data = {
                'campaign_id': result.campaign_id,
                'summary': summary,
                'consolidation': result.consolidation_result,
                'clustering': result.clustering_result,
                'errors': result.errors,
                'warnings': result.warnings
            }

            with open(args.save_results, 'w') as f:
                json.dump(results_data, f, indent=2, default=str)
            logger.info(f"💾 Results saved to: {args.save_results}")

        # Exit with appropriate code
        # Note: Clustering stage filters STOP/invalid messages and splits multi-part texts
        # Only exit with error if there are actual processing errors
        # Upload failures already raise RuntimeError, so they won't reach here
        if result.errors:
            logger.warning("❌ Pipeline completed with errors")
            sys.exit(1)
        else:
            logger.info("✅ Pipeline completed successfully!")
            sys.exit(0)

    except KeyboardInterrupt:
        logger.info("⏹️ Pipeline interrupted by user")
        sys.exit(130)

    except Exception as e:
        logger.error(f"💥 Pipeline failed: {e}")
        if args.debug:
            import traceback
            logger.error(f"Traceback:\n{traceback.format_exc()}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
