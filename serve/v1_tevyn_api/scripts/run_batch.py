#!/usr/bin/env python3

"""
V1 Tevyn API Batch Pipeline Runner

Run the pipeline for multiple campaigns in sequence or parallel.

Usage:
    uv run serve/v1_tevyn_api/scripts/run_batch.py --all
    uv run serve/v1_tevyn_api/scripts/run_batch.py --campaigns berkley cara josh
    uv run serve/v1_tevyn_api/scripts/run_batch.py --all --parallel
"""

import asyncio
import argparse
import sys
import json
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

# Add project paths
sys.path.append('/Users/collinpark/work/gp-ai-projects')
from shared.logger import get_logger

# Import pipeline components
from serve.v1_tevyn_api.pipeline.orchestrator import TevynPipelineOrchestrator
from serve.consolidate_replies_results import RepliesResultsConsolidator

logger = get_logger(__name__)


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description='V1 Tevyn API Batch Pipeline Runner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process all discovered campaigns
  %(prog)s --all

  # Process specific campaigns
  %(prog)s --campaigns berkley cara josh

  # Process campaigns in parallel
  %(prog)s --all --parallel

  # Test mode (no upload)
  %(prog)s --all --test

  # Save detailed results
  %(prog)s --all --save-results batch_results.json
        """
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        '--all',
        action='store_true',
        help='Process all discovered campaigns'
    )

    group.add_argument(
        '--campaigns',
        nargs='+',
        help='List of specific campaigns to process'
    )

    parser.add_argument(
        '--config',
        type=str,
        help='Path to pipeline configuration file'
    )

    parser.add_argument(
        '--parallel',
        action='store_true',
        help='Run campaigns in parallel (faster but more resource intensive)'
    )

    parser.add_argument(
        '--max-parallel',
        type=int,
        default=3,
        help='Maximum number of parallel campaigns (default: 3)'
    )

    parser.add_argument(
        '--test',
        action='store_true',
        help='Test mode: run pipeline but skip upload to DynamoDB'
    )

    parser.add_argument(
        '--skip-classification',
        action='store_true',
        help='Skip classification stage for all campaigns'
    )

    parser.add_argument(
        '--skip-clustering',
        action='store_true',
        help='Skip clustering stage for all campaigns'
    )

    parser.add_argument(
        '--continue-on-error',
        action='store_true',
        help='Continue processing other campaigns if one fails'
    )

    parser.add_argument(
        '--save-results',
        type=str,
        help='Save batch results to JSON file'
    )

    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug logging'
    )

    parser.add_argument(
        '--anonymize-keywords',
        type=str,
        nargs='*',
        help='Keywords to anonymize during AI summarization (e.g. --anonymize-keywords Berkeley "Kendall County")'
    )

    return parser.parse_args()


def discover_campaigns(input_dir: str = "../input") -> List[str]:
    """Discover available campaigns"""
    try:
        consolidator = RepliesResultsConsolidator(input_dir)
        campaigns_data = consolidator.discover_files()
        campaigns = list(campaigns_data.keys())
        logger.info(f"Discovered {len(campaigns)} campaigns: {campaigns}")
        return campaigns
    except Exception as e:
        logger.error(f"Failed to discover campaigns: {e}")
        return []


def setup_logging(debug: bool = False):
    """Setup logging configuration"""
    import logging

    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def create_modified_config(base_config_path: Optional[str], args) -> str:
    """Create a modified config based on arguments"""
    import yaml
    import tempfile

    # Use default config if none provided
    if not base_config_path:
        base_config_path = str(Path(__file__).parent.parent / "config/pipeline_config.yaml")

    try:
        with open(base_config_path, 'r') as f:
            config = yaml.safe_load(f)

        # Apply modifications
        if args.test:
            config['upload']['enabled'] = False

        if args.skip_classification:
            config['classification']['enabled'] = False

        if args.skip_clustering:
            config['clustering']['enabled'] = False

        if args.anonymize_keywords:
            if 'clustering' not in config:
                config['clustering'] = {}
            config['clustering']['anonymize_keywords'] = args.anonymize_keywords

        # Create temporary config file
        temp_config = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False)
        yaml.dump(config, temp_config)
        temp_config.close()

        return temp_config.name

    except Exception as e:
        logger.error(f"Failed to create modified config: {e}")
        return base_config_path


async def process_campaign(campaign_name: str, config_path: str, semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    """Process a single campaign"""
    async with semaphore:
        logger.info(f"🎯 Starting pipeline for campaign: {campaign_name}")
        start_time = time.time()

        try:
            orchestrator = TevynPipelineOrchestrator(config_path)
            result = await orchestrator.run_pipeline(campaign_name)

            processing_time = time.time() - start_time
            success = result.failed_records == 0 and not result.errors

            campaign_result = {
                'campaign_name': campaign_name,
                'success': success,
                'processing_time': processing_time,
                'summary': result.summary,
                'total_messages': result.total_messages,
                'successful_records': result.successful_records,
                'failed_records': result.failed_records,
                'errors': result.errors,
                'warnings': result.warnings
            }

            if success:
                logger.info(f"✅ {campaign_name} completed successfully in {processing_time:.2f}s")
            else:
                logger.warning(f"⚠️ {campaign_name} completed with errors in {processing_time:.2f}s")

            return campaign_result

        except Exception as e:
            processing_time = time.time() - start_time
            logger.error(f"❌ {campaign_name} failed after {processing_time:.2f}s: {e}")

            return {
                'campaign_name': campaign_name,
                'success': False,
                'processing_time': processing_time,
                'error': str(e),
                'total_messages': 0,
                'successful_records': 0,
                'failed_records': 0,
                'errors': [str(e)],
                'warnings': []
            }


async def process_campaigns_sequential(campaigns: List[str], config_path: str) -> List[Dict[str, Any]]:
    """Process campaigns sequentially"""
    results = []
    semaphore = asyncio.Semaphore(1)  # Only one at a time

    for campaign in campaigns:
        result = await process_campaign(campaign, config_path, semaphore)
        results.append(result)

        # Stop on first failure if not continuing on error
        if not result['success'] and not args.continue_on_error:
            logger.error(f"Stopping batch processing due to failure in {campaign}")
            break

    return results


async def process_campaigns_parallel(campaigns: List[str], config_path: str, max_parallel: int) -> List[Dict[str, Any]]:
    """Process campaigns in parallel"""
    semaphore = asyncio.Semaphore(max_parallel)

    # Create tasks for all campaigns
    tasks = [
        process_campaign(campaign, config_path, semaphore)
        for campaign in campaigns
    ]

    # Execute all campaigns in parallel
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Handle any exceptions
    processed_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(f"Campaign {campaigns[i]} failed with exception: {result}")
            processed_results.append({
                'campaign_name': campaigns[i],
                'success': False,
                'error': str(result),
                'total_messages': 0,
                'successful_records': 0,
                'failed_records': 0,
                'processing_time': 0,
                'errors': [str(result)],
                'warnings': []
            })
        else:
            processed_results.append(result)

    return processed_results


def print_batch_summary(results: List[Dict[str, Any]], total_time: float):
    """Print summary of batch processing"""
    total_campaigns = len(results)
    successful_campaigns = sum(1 for r in results if r['success'])
    failed_campaigns = total_campaigns - successful_campaigns

    total_messages = sum(r['total_messages'] for r in results)
    total_successful_records = sum(r['successful_records'] for r in results)
    total_failed_records = sum(r['failed_records'] for r in results)

    print("\n" + "="*80)
    print("📊 BATCH PROCESSING SUMMARY")
    print("="*80)
    print(f"Total Campaigns: {total_campaigns}")
    print(f"Successful Campaigns: {successful_campaigns}")
    print(f"Failed Campaigns: {failed_campaigns}")
    print(f"Total Processing Time: {total_time:.2f}s")
    print(f"Total Messages Processed: {total_messages:,}")
    print(f"Total Successful Records: {total_successful_records:,}")
    print(f"Total Failed Records: {total_failed_records:,}")

    if total_messages > 0:
        success_rate = (total_successful_records / total_messages) * 100
        print(f"Overall Success Rate: {success_rate:.1f}%")

    print("\n📋 Campaign Results:")
    print("-" * 80)
    for result in results:
        status = "✅" if result['success'] else "❌"
        campaign = result['campaign_name']
        messages = result['total_messages']
        time_taken = result['processing_time']
        print(f"{status} {campaign:<15} | {messages:>5} messages | {time_taken:>6.1f}s")

    print("="*80)


async def main():
    """Main execution function"""
    global args
    args = parse_arguments()

    # Setup logging
    setup_logging(args.debug)

    logger.info("🚀 V1 Tevyn API Batch Pipeline Starting")

    start_time = time.time()

    try:
        # Determine campaigns to process
        if args.all:
            campaigns = discover_campaigns()
            if not campaigns:
                logger.error("No campaigns discovered")
                sys.exit(1)
        else:
            campaigns = args.campaigns

        logger.info(f"Processing {len(campaigns)} campaigns: {campaigns}")

        # Create modified config
        config_path = create_modified_config(args.config, args)

        # Process campaigns
        if args.parallel:
            logger.info(f"Processing campaigns in parallel (max {args.max_parallel})")
            results = await process_campaigns_parallel(campaigns, config_path, args.max_parallel)
        else:
            logger.info("Processing campaigns sequentially")
            results = await process_campaigns_sequential(campaigns, config_path)

        total_time = time.time() - start_time

        # Print summary
        print_batch_summary(results, total_time)

        # Save results if requested
        if args.save_results:
            batch_data = {
                'batch_summary': {
                    'total_campaigns': len(results),
                    'successful_campaigns': sum(1 for r in results if r['success']),
                    'failed_campaigns': sum(1 for r in results if not r['success']),
                    'total_processing_time': total_time,
                    'parallel_processing': args.parallel
                },
                'campaign_results': results
            }

            with open(args.save_results, 'w') as f:
                json.dump(batch_data, f, indent=2, default=str)
            logger.info(f"💾 Batch results saved to: {args.save_results}")

        # Exit with appropriate code
        failed_campaigns = sum(1 for r in results if not r['success'])
        if failed_campaigns > 0:
            logger.warning(f"❌ Batch completed with {failed_campaigns} failed campaigns")
            sys.exit(1)
        else:
            logger.info("✅ Batch completed successfully!")
            sys.exit(0)

    except KeyboardInterrupt:
        logger.info("⏹️ Batch processing interrupted by user")
        sys.exit(130)

    except Exception as e:
        logger.error(f"💥 Batch processing failed: {e}")
        if args.debug:
            import traceback
            logger.error(f"Traceback:\n{traceback.format_exc()}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())