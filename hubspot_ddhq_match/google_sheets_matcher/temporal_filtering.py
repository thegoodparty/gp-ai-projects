#!/usr/bin/env python3

"""
TEMPORAL FILTERING FOR HUBSPOT-GOOGLE SHEETS MATCHING

Filters HubSpot companies to only those with election dates that exist in Google Sheets.
This prevents matching candidates from dates that don't have any Google Sheets races.

USAGE:
uv run temporal_filtering.py
"""

import sys
import os
import pandas as pd
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))
from shared.logger import get_logger

class TemporalFilter:
    def __init__(self):
        self.logger = get_logger(__name__)

    def filter_by_dates(self, hubspot_df: pd.DataFrame, google_sheets_df: pd.DataFrame) -> pd.DataFrame:
        """Filter HubSpot companies to only dates present in Google Sheets"""
        self.logger.info("🔍 Filtering HubSpot by temporal alignment...")

        original_count = len(hubspot_df)

        google_dates = set(google_sheets_df['date'].unique())
        self.logger.info(f"   - Google Sheets has {len(google_dates)} unique election dates")

        hubspot_filtered = hubspot_df[hubspot_df['election_date'].isin(google_dates)].copy()

        filtered_count = len(hubspot_filtered)
        removed_count = original_count - filtered_count

        self.logger.info(f"✅ Temporal filtering complete:")
        self.logger.info(f"   - Original HubSpot records: {original_count:,}")
        self.logger.info(f"   - Records with matching dates: {filtered_count:,}")
        self.logger.info(f"   - Records filtered out: {removed_count:,}")

        return hubspot_filtered

    def save_filtered_data(self, df: pd.DataFrame):
        """Save filtered data to offline_data directory"""
        self.logger.info("💾 Saving filtered dataset...")

        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        parquet_file = os.path.join(offline_data_dir, "hubspot_companies_filtered_latest.parquet")
        tsv_file = os.path.join(offline_data_dir, f"hubspot_companies_filtered_{timestamp}.tsv")

        df.to_parquet(parquet_file, index=False)
        df.to_csv(tsv_file, sep='\t', index=False)

        self.logger.info(f"✅ Filtered data saved:")
        self.logger.info(f"   - {parquet_file}")

    def run(self):
        """Execute complete filtering pipeline"""
        self.logger.info("🚀 Starting temporal filtering...")

        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")

        hubspot_cleaned = pd.read_parquet(
            os.path.join(offline_data_dir, "hubspot_companies_cleaned_latest.parquet")
        )
        google_sheets_cleaned = pd.read_parquet(
            os.path.join(offline_data_dir, "google_sheets_races_cleaned_latest.parquet")
        )

        self.logger.info(f"   - Loaded {len(hubspot_cleaned):,} HubSpot records")
        self.logger.info(f"   - Loaded {len(google_sheets_cleaned):,} Google Sheets races")

        hubspot_filtered = self.filter_by_dates(hubspot_cleaned, google_sheets_cleaned)

        self.save_filtered_data(hubspot_filtered)

        self.logger.info("✅ Temporal filtering complete!")
        self.logger.info(f"   - Final count: {len(hubspot_filtered):,} HubSpot records ready for matching")


def main():
    """Main execution"""
    print("="*80)
    print("HUBSPOT-GOOGLE SHEETS TEMPORAL FILTERING")
    print("="*80)

    try:
        filter = TemporalFilter()
        filter.run()

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        raise


if __name__ == "__main__":
    main()
