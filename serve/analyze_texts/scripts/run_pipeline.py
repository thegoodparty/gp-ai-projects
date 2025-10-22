#!/usr/bin/env python3

import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent.parent))

from serve.analyze_texts.orchestrator import AnalyzeTextsOrchestrator
from shared.logger import get_logger

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Run the Analyze Texts Pipeline on campaign message data"
    )

    parser.add_argument(
        "--campaign",
        type=str,
        required=True,
        help="Campaign name (e.g., berkley, cara-burnsville, josh-minooka)"
    )

    parser.add_argument(
        "--config",
        type=str,
        default="serve/analyze_texts/config.yaml",
        help="Path to config file (default: serve/analyze_texts/config.yaml)"
    )

    parser.add_argument(
        "--no-atomize",
        action="store_true",
        help="Skip atomization stage (use original messages)"
    )

    args = parser.parse_args()

    try:
        logger.info(f"Starting Analyze Texts Pipeline for campaign: {args.campaign}")

        orchestrator = AnalyzeTextsOrchestrator(config_path=args.config)

        if args.no_atomize:
            orchestrator.config['atomizer']['enabled'] = False
            logger.info("Atomization disabled by --no-atomize flag")

        results = orchestrator.run(campaign=args.campaign)

        logger.info("\n✅ Pipeline completed successfully!")
        logger.info(f"   Campaign: {results['campaign']}")
        logger.info(f"   Messages analyzed: {len(results['classified_messages'])}")
        logger.info(f"   Categories found: {len(results['category_summaries'])}")

        return 0

    except FileNotFoundError as e:
        logger.error(f"❌ File not found: {e}")
        return 1

    except Exception as e:
        logger.error(f"❌ Pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
