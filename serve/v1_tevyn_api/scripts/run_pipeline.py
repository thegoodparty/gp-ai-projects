#!/usr/bin/env python3

"""
V1 Tevyn API Pipeline Runner

Run the complete pipeline for processing campaign messages through consolidation,
classification, clustering, and upload to DynamoDB.

Usage:
    uv run serve/v1_tevyn_api/scripts/run_pipeline.py --campaign berkley
    uv run serve/v1_tevyn_api/scripts/run_pipeline.py --campaign berkley --test
    uv run serve/v1_tevyn_api/scripts/run_pipeline.py --campaign berkley --config custom_config.yaml
"""

import asyncio
import argparse
import sys
import json
from pathlib import Path
from typing import Optional

# Add project paths
sys.path.append('/Users/collinpark/work/gp-ai-projects')
from shared.logger import get_logger

# Import pipeline components
from serve.v1_tevyn_api.pipeline.orchestrator import TevynPipelineOrchestrator

logger = get_logger(__name__)


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description='V1 Tevyn API Pipeline Runner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run pipeline for Berkley campaign
  %(prog)s --campaign berkley

  # Test mode (no actual upload)
  %(prog)s --campaign berkley --test

  # Use custom configuration
  %(prog)s --campaign berkley --config /path/to/config.yaml

  # Skip specific stages
  %(prog)s --campaign berkley --skip-classification --skip-clustering

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
        '--test',
        action='store_true',
        help='Test mode: run pipeline but skip upload to DynamoDB'
    )

    parser.add_argument(
        '--skip-classification',
        action='store_true',
        help='Skip classification stage'
    )

    parser.add_argument(
        '--skip-clustering',
        action='store_true',
        help='Skip clustering stage'
    )

    parser.add_argument(
        '--skip-upload',
        action='store_true',
        help='Skip upload stage'
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
        '--resume',
        action='store_true',
        help='Resume from last checkpoint (if available)'
    )

    parser.add_argument(
        '--save-results',
        type=str,
        help='Save pipeline results to JSON file'
    )

    parser.add_argument(
        '--anonymize-keywords',
        type=str,
        nargs='*',
        help='Keywords to anonymize during AI summarization (e.g. --anonymize-keywords Berkeley "Kendall County")'
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


def modify_config_for_arguments(config_path: Optional[str], args) -> Optional[str]:
    """Modify configuration based on command line arguments"""
    if not config_path:
        # Use default config
        config_path = str(Path(__file__).parent.parent / "config/pipeline_config.yaml")

    # If we need to modify config, create a temporary one
    modifications = {}

    if args.test or args.skip_upload:
        modifications['upload'] = {'enabled': False}

    if args.skip_classification:
        modifications['classification'] = {'enabled': False}

    if args.skip_clustering:
        modifications['clustering'] = {'enabled': False}

    if args.output_dir:
        modifications['consolidation'] = {'output_dir': args.output_dir}

    if args.anonymize_keywords:
        modifications['clustering'] = modifications.get('clustering', {})
        modifications['clustering']['anonymize_keywords'] = args.anonymize_keywords

    # If no modifications needed, return original config
    if not modifications:
        return config_path

    # Create temporary config with modifications
    import yaml
    import tempfile

    try:
        with open(config_path, 'r') as f:
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

    logger.info("🚀 V1 Tevyn API Pipeline Starting")
    logger.info(f"Campaign: {args.campaign}")

    try:
        # Modify config based on arguments
        config_path = modify_config_for_arguments(args.config, args)

        # Initialize orchestrator
        orchestrator = TevynPipelineOrchestrator(config_path)

        # Log configuration
        if args.test:
            logger.info("🧪 Running in TEST mode (no upload)")
        if args.skip_classification:
            logger.info("⏭️ Skipping classification stage")
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
                'classification': result.classification_result,
                'clustering': result.clustering_result,
                'upload': result.upload_result,
                'errors': result.errors,
                'warnings': result.warnings
            }

            with open(args.save_results, 'w') as f:
                json.dump(results_data, f, indent=2, default=str)
            logger.info(f"💾 Results saved to: {args.save_results}")

        # Exit with appropriate code
        if result.failed_records > 0 or result.errors:
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