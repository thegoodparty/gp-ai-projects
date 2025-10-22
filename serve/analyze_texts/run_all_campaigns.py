import sys
from pathlib import Path

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
    logger.info(f"RUNNING PIPELINE FOR {len(campaigns)} CAMPAIGNS WITH STRUCTURED OUTPUTS")
    logger.info("=" * 80)

    results = {}

    for i, campaign in enumerate(campaigns, 1):
        logger.info(f"\n{'=' * 80}")
        logger.info(f"[{i}/{len(campaigns)}] Processing: {campaign}")
        logger.info(f"{'=' * 80}")

        orchestrator = AnalyzeTextsOrchestrator()
        result = orchestrator.run(campaign=campaign)

        results[campaign] = {
            "messages": len(result['classified_messages']),
            "categories": len(result['category_summaries'])
        }

        logger.info(f"✅ {campaign}: {results[campaign]['messages']} messages, {results[campaign]['categories']} categories")

    logger.info("\n" + "=" * 80)
    logger.info("ALL CAMPAIGNS COMPLETED")
    logger.info("=" * 80)

    for campaign, stats in results.items():
        logger.info(f"  {campaign:30s}: {stats['messages']:4d} messages, {stats['categories']:2d} categories")
