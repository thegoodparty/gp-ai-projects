import sys
from pathlib import Path
import time

sys.path.append(str(Path(__file__).parent.parent.parent))

from serve.analyze_texts.orchestrator import AnalyzeTextsOrchestrator
from shared.logger import get_logger

logger = get_logger(__name__)

campaigns = [
    "cara-burnsville",
    "josh-minooka",
    "japjeet-livingston",
    "joanna-missouri-city",
    "jonathan-north-las-vegas",
    "heather-ghaps"
]

if __name__ == "__main__":
    logger.info("=" * 80)
    logger.info(f"RERUNNING PIPELINE FOR {len(campaigns)} CAMPAIGNS WITH UNIQUE RECORD IDS")
    logger.info("=" * 80)

    results = {}
    overall_start = time.time()

    for i, campaign in enumerate(campaigns, 1):
        logger.info(f"\n{'=' * 80}")
        logger.info(f"[{i}/{len(campaigns)}] Processing: {campaign}")
        logger.info(f"{'=' * 80}")

        campaign_start = time.time()

        try:
            orchestrator = AnalyzeTextsOrchestrator()
            result = orchestrator.run(campaign=campaign)

            campaign_duration = time.time() - campaign_start

            results[campaign] = {
                "messages": len(result['classified_messages']),
                "categories": len(result['category_summaries']),
                "duration": campaign_duration,
                "status": "success"
            }

            logger.info(f"✅ {campaign}: {results[campaign]['messages']} messages, "
                       f"{results[campaign]['categories']} categories "
                       f"({campaign_duration/60:.1f} minutes)")

        except Exception as e:
            campaign_duration = time.time() - campaign_start
            results[campaign] = {
                "status": "failed",
                "error": str(e),
                "duration": campaign_duration
            }
            logger.error(f"❌ {campaign} failed: {e}")

    overall_duration = time.time() - overall_start

    logger.info("\n" + "=" * 80)
    logger.info("ALL CAMPAIGNS COMPLETED")
    logger.info(f"Total time: {overall_duration/60:.1f} minutes")
    logger.info("=" * 80)

    for campaign, stats in results.items():
        if stats['status'] == 'success':
            logger.info(f"  ✅ {campaign:30s}: {stats['messages']:4d} messages, "
                       f"{stats['categories']:2d} categories ({stats['duration']/60:.1f}m)")
        else:
            logger.info(f"  ❌ {campaign:30s}: FAILED - {stats['error']}")
