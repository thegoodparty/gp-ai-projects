#!/usr/bin/env python3

"""
Message Analysis Pipeline Runner

Supports two modes:
- cluster: Hierarchical clustering for open-ended questions
- classify: Classification into predefined options for structured questions

Usage:
    # Cluster mode (open-ended)
    uv run serve/v1_pipeline/scripts/run_pipeline.py --campaign berkley --mode cluster

    # Classify mode (structured)
    uv run serve/v1_pipeline/scripts/run_pipeline.py --campaign poll123 --mode classify \
        --poll-id poll123 --question-text "Do you support the park?" --options-json '["Yes", "No"]'
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
        description='Message Analysis Pipeline Runner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Cluster mode: Run clustering pipeline for Berkley campaign
  %(prog)s --campaign berkley --mode cluster

  # Classify mode: Run classification pipeline for poll
  %(prog)s --campaign poll123 --mode classify \\
      --poll-id poll123 \\
      --question-text "Do you support the new park?" \\
      --options-json '["Yes", "No"]'

  # Classify mode with callbacks
  %(prog)s --campaign poll123 --mode classify \\
      --poll-id poll123 \\
      --question-text "Do you support the new park?" \\
      --options-json '["Yes", "No"]' \\
      --callback-success-url "https://api.example.com/success" \\
      --callback-failure-url "https://api.example.com/failure"
        """
    )

    parser.add_argument(
        '--campaign',
        type=str,
        required=True,
        help='Campaign name to process (e.g., "berkley", "poll123")'
    )

    parser.add_argument(
        '--mode',
        type=str,
        choices=['cluster', 'classify'],
        default='cluster',
        help='Pipeline mode: cluster (clustering) or classify (classification)'
    )

    parser.add_argument(
        '--poll-id',
        type=str,
        help='Poll ID for classify mode'
    )

    parser.add_argument(
        '--question-text',
        type=str,
        help='Question text for classify mode'
    )

    parser.add_argument(
        '--options-json',
        type=str,
        help='JSON array of options for classify mode (e.g., \'["Yes", "No"]\')'
    )

    parser.add_argument(
        '--callback-success-url',
        type=str,
        help='Webhook URL to call on success (classify mode)'
    )

    parser.add_argument(
        '--callback-failure-url',
        type=str,
        help='Webhook URL to call on failure (classify mode)'
    )

    parser.add_argument(
        '--config',
        type=str,
        help='Path to pipeline configuration file'
    )

    parser.add_argument(
        '--skip-clustering',
        action='store_true',
        help='Skip clustering stage (cluster mode only)'
    )

    parser.add_argument(
        '--skip-classification',
        action='store_true',
        help='Skip classification stage (classify mode only)'
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

    logging.getLogger('asyncio').setLevel(logging.WARNING)
    logging.getLogger('aiohttp').setLevel(logging.WARNING)


def modify_config_for_arguments(config_path: str | None, args) -> str | None:
    """Modify configuration based on command line arguments"""
    if not config_path:
        config_path = str(Path(__file__).parent.parent / "config/pipeline_config.yaml")

    modifications = {}

    if args.skip_clustering:
        modifications['clustering'] = {'enabled': False}

    if args.output_dir:
        modifications['consolidation'] = {'output_dir': args.output_dir}

    if not modifications:
        return config_path

    import tempfile

    import yaml

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)

        for section, updates in modifications.items():
            if section in config:
                config[section].update(updates)
            else:
                config[section] = updates

        temp_config = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False)
        yaml.dump(config, temp_config)
        temp_config.close()

        logger.debug(f"Created temporary config with modifications: {temp_config.name}")
        return temp_config.name

    except Exception as e:
        logger.error(f"Failed to modify config: {e}")
        return config_path


async def run_cluster_pipeline(args, config_path: str | None):
    """Run clustering pipeline"""
    logger.info("🚀 Clustering Pipeline Starting")
    logger.info(f"Campaign: {args.campaign}")

    orchestrator = V1PipelineOrchestrator(config_path)

    if args.skip_clustering:
        logger.info("⏭️ Skipping clustering stage")

    result = await orchestrator.run_pipeline(args.campaign)

    print("\n" + "="*60)
    print("📊 CLUSTERING RESULTS SUMMARY")
    print("="*60)

    summary = result.summary
    for key, value in summary.items():
        print(f"{key.replace('_', ' ').title()}: {value}")

    print("="*60)

    if args.save_results:
        output_path = Path(args.save_results).resolve()
        results_data = {
            'campaign_id': result.campaign_id,
            'mode': 'cluster',
            'summary': summary,
            'consolidation': result.consolidation_result,
            'clustering': result.clustering_result,
            'errors': result.errors,
            'warnings': result.warnings
        }

        with open(output_path, 'w') as f:
            json.dump(results_data, f, indent=2, default=str)
        print(f"\n📁 OUTPUT FILE: {output_path}")
        logger.info(f"💾 Results saved to: {output_path}")

    return result


async def run_classify_pipeline(args):
    """Run classification pipeline"""
    from serve.v1_pipeline.pipeline.classifier import ClassificationPipeline

    logger.info("🚀 Classification Pipeline Starting")
    logger.info(f"Poll ID: {args.poll_id}")
    logger.info(f"Question: {args.question_text}")

    options = json.loads(args.options_json) if args.options_json else []
    logger.info(f"Options: {options}")

    pipeline = ClassificationPipeline(
        poll_id=args.poll_id,
        campaign_id=args.campaign,
        question_text=args.question_text,
        options=options,
        callback_success_url=args.callback_success_url,
        callback_failure_url=args.callback_failure_url
    )

    result = await pipeline.run()

    print("\n" + "="*60)
    print("📊 CLASSIFICATION RESULTS SUMMARY")
    print("="*60)
    print(f"Poll ID: {result.poll_id}")
    print(f"Question: {result.question_text}")
    print(f"Total Responses: {result.total_responses}")
    print("-"*60)

    for issue in result.issues:
        pct = round(issue.response_count / result.total_responses * 100, 1) if result.total_responses > 0 else 0
        print(f"\n#{issue.rank} {issue.theme}: {issue.response_count} ({pct}%)")
        print(f"  Summary: {issue.summary}")
        print(f"  Quotes: {len(issue.quotes)}")

    print("="*60)

    events_dir = Path(__file__).parent.parent / "output" / "events"
    print(f"\n📁 OUTPUT DIR: {events_dir.resolve()}")

    if args.save_results:
        output_path = Path(args.save_results).resolve()
        with open(output_path, 'w') as f:
            json.dump(result.to_dict(), f, indent=2, default=str)
        print(f"📁 EXTRA COPY: {output_path}")
        logger.info(f"💾 Results also saved to: {output_path}")

    return result


async def main():
    """Main execution function"""
    args = parse_arguments()

    setup_logging(args.debug)

    try:
        if args.mode == 'classify':
            if not args.poll_id:
                logger.error("--poll-id is required for classify mode")
                sys.exit(1)
            if not args.question_text:
                logger.error("--question-text is required for classify mode")
                sys.exit(1)

            result = await run_classify_pipeline(args)

            if hasattr(result, 'errors') and result.errors:
                logger.warning("❌ Pipeline completed with errors")
                sys.exit(1)
            else:
                logger.info("✅ Pipeline completed successfully!")
                sys.exit(0)

        else:
            config_path = modify_config_for_arguments(args.config, args)
            result = await run_cluster_pipeline(args, config_path)

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
