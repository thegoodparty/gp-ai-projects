#!/usr/bin/env python3

import pandas as pd
import sys
from pathlib import Path
sys.path.append('/Users/collinpark/work/gp-ai-projects')

from shared.logger import get_logger

logger = get_logger(__name__)

def consolidate_campaign_files():
    """Consolidate all campaign files into 3 master files: Berkley, Cara, Josh"""

    data_dir = Path("/Users/collinpark/work/gp-ai-projects/serve/data")
    output_dir = Path("/Users/collinpark/work/gp-ai-projects/serve/data")
    output_dir.mkdir(exist_ok=True)

    # Define file mappings for each campaign
    campaigns = {
        'berkley': {
            'files': [
                'City-of-Berkley-MI-Serve-Round-1-replies-2025-08-28-17_41_11.csv',
                '-Serve--Berkley--R1-Followups-replies-2025-09-08-16_31_35.csv',
                '-Serve--Berkley--R2-replies-2025-09-08-16_05_06.csv'
            ],
            'output': 'berkley_all_rounds_consolidated.csv'
        },
        'cara': {
            'files': [
                '-Serve--Cara-Burnsville--R1-replies-2025-09-08-16_30_36.csv',
                '-Serve--Cara-Burnsville--R2-replies-2025-09-10-18_30_59.csv',
                '-Serve--Cara-Burnsville--R3-replies-2025-09-18-16_36_44.csv'
            ],
            'output': 'cara_all_rounds_consolidated.csv'
        },
        'josh': {
            'files': [
                '-Serve--Josh-Minooka--R1-replies-2025-09-08-16_31_53.csv',
                '-Serve--Josh-Minooka--R1-Followups-replies-2025-09-08-16_31_07.csv',
                '-Serve--Josh-Minooka--R2-replies-2025-09-08-16_30_07.csv',
                '-Serve--Josh-Minooka--R3-replies-2025-09-10-18_27_58.csv'
            ],
            'output': 'josh_all_rounds_consolidated.csv'
        },
        'heather': {
            'files': [
                '-Serve--Heather-GHAPS-500-replies-2025-09-30-21_12_25.csv'
            ],
            'output': 'heather_all_rounds_consolidated.csv'
        },
        'japjeet': {
            'files': [
                '-Serve--Japjeet-Livingston-500-replies-2025-09-30-21_11_49.csv'
            ],
            'output': 'japjeet_all_rounds_consolidated.csv'
        },
        'joanna': {
            'files': [
                '-Serve--Joanna-Missouri-City-500-replies-2025-09-30-21_12_51.csv'
            ],
            'output': 'joanna_all_rounds_consolidated.csv'
        },
        'jonathan': {
            'files': [
                '-Serve--Jonathan-North-Las-Vegas-500-replies-2025-09-30-21_13_32.csv'
            ],
            'output': 'jonathan_all_rounds_consolidated.csv'
        }
    }

    # Process each campaign
    for campaign_name, config in campaigns.items():
        logger.info(f"📁 Consolidating {campaign_name.upper()} campaign files...")

        all_dataframes = []
        total_messages = 0

        for file_name in config['files']:
            file_path = data_dir / file_name

            if not file_path.exists():
                logger.warning(f"⚠️  File not found: {file_name}")
                continue

            try:
                df = pd.read_csv(file_path)
                messages_count = len(df)
                all_dataframes.append(df)
                total_messages += messages_count

                logger.info(f"  ✅ {file_name}: {messages_count} messages")

            except Exception as e:
                logger.error(f"  ❌ Error reading {file_name}: {e}")
                continue

        if all_dataframes:
            # Consolidate all dataframes
            consolidated_df = pd.concat(all_dataframes, ignore_index=True)

            # Remove duplicates based on phone number + message text + timestamp
            before_dedup = len(consolidated_df)
            consolidated_df = consolidated_df.drop_duplicates(
                subset=['Contact Phone Number', 'Message Text', 'Sent At'],
                keep='first'
            )
            after_dedup = len(consolidated_df)
            duplicates_removed = before_dedup - after_dedup

            # Save consolidated file
            output_path = output_dir / config['output']
            consolidated_df.to_csv(output_path, index=False)

            logger.info(f"  🎯 CONSOLIDATED: {after_dedup} messages ({duplicates_removed} duplicates removed)")
            logger.info(f"  💾 Saved to: {output_path}")

        else:
            logger.error(f"  ❌ No valid files found for {campaign_name}")

        print()  # Add spacing between campaigns

    logger.info("🏆 All campaign data consolidated successfully!")
    logger.info(f"📂 Output directory: {output_dir}")

def main():
    """Run the consolidation process"""
    consolidate_campaign_files()

if __name__ == "__main__":
    main()