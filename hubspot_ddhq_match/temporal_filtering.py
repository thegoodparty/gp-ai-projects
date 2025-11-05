#!/usr/bin/env python3

"""
TEMPORAL FILTERING FOR HUBSPOT-DDHQ MATCHING

Filters HubSpot candidates to only include those with election dates that exactly match
DDHQ election dates. This ensures we're only matching candidates from the same actual elections.

USAGE:
uv run temporal_filtering.py
"""

import sys
import os
import pandas as pd
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from shared.logger import get_logger

class TemporalFilter:
    def __init__(self):
        self.logger = get_logger(__name__)
        
    def filter_hubspot_by_ddhq_dates(self, hubspot_df: pd.DataFrame, ddhq_df: pd.DataFrame) -> pd.DataFrame:
        """
        Filter HubSpot records to only include those with election dates that exactly match DDHQ election dates.
        Special handling for runoff elections: if runoff date doesn't match, check if general_election_date matches.
        """
        self.logger.info("🗓️ Starting temporal filtering...")

        original_count = len(hubspot_df)

        # Get all unique election dates from DDHQ
        ddhq_unique_dates = set(pd.to_datetime(ddhq_df['date']).dt.date)
        self.logger.info(f"   DDHQ unique election dates: {len(ddhq_unique_dates):,}")
        self.logger.info(f"   Date range: {min(ddhq_unique_dates)} to {max(ddhq_unique_dates)}")

        # Convert HubSpot election dates to date objects for comparison
        # Note: After cleaning, each HubSpot record now has a single election_date
        hubspot_election_dates = pd.to_datetime(hubspot_df['election_date'], errors='coerce').dt.date

        # Standard temporal match: election_date exists in DDHQ
        temporal_match = hubspot_election_dates.isin(ddhq_unique_dates)

        # Special handling for runoffs: if runoff date doesn't match, check general_election_date
        is_runoff = hubspot_df['election_type'] == 'runoff'
        has_general_date = hubspot_df['general_election_date'].notna()
        general_dates = pd.to_datetime(hubspot_df['general_election_date'], errors='coerce').dt.date
        general_match = general_dates.isin(ddhq_unique_dates)

        # Runoff fallback: runoff records where runoff_date doesn't match but general_date does
        runoff_fallback_match = is_runoff & has_general_date & general_match & ~temporal_match

        # Combined filter: standard match OR runoff with general fallback
        final_match = temporal_match | runoff_fallback_match

        # Apply the filter
        hubspot_filtered = hubspot_df[final_match].copy()

        final_count = len(hubspot_filtered)
        removed_count = original_count - final_count
        retention_rate = final_count / original_count * 100

        # Count statistics
        standard_matches = temporal_match.sum()
        runoff_fallback_matches = runoff_fallback_match.sum()

        self.logger.info(f"✅ Temporal filtering complete:")
        self.logger.info(f"   Original records: {original_count:,}")
        self.logger.info(f"   Filtered records: {final_count:,}")
        self.logger.info(f"   Standard date matches: {standard_matches:,}")
        self.logger.info(f"   Runoff fallback matches: {runoff_fallback_matches:,}")
        self.logger.info(f"   Removed records:  {removed_count:,}")
        self.logger.info(f"   Retention rate:   {retention_rate:.1f}%")

        # Log match statistics by election type
        if 'election_type' in hubspot_filtered.columns:
            primary_records = hubspot_filtered[hubspot_filtered['election_type'] == 'primary']
            general_records = hubspot_filtered[hubspot_filtered['election_type'] == 'general']
            runoff_records = hubspot_filtered[hubspot_filtered['election_type'] == 'runoff']

            self.logger.info(f"   Primary election records: {len(primary_records):,}")
            self.logger.info(f"   General election records: {len(general_records):,}")
            self.logger.info(f"   Runoff election records: {len(runoff_records):,}")

        return hubspot_filtered
    
    def save_filtered_data(self, hubspot_filtered: pd.DataFrame) -> str:
        """Save temporally filtered HubSpot data"""
        self.logger.info("💾 Saving filtered dataset...")
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Get script directory for saving files
        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")
        os.makedirs(offline_data_dir, exist_ok=True)
        
        # Save timestamped and latest versions
        timestamped_file = os.path.join(offline_data_dir, f"hubspot_filtered_to_match_ddhq_dates_{timestamp}.parquet")
        latest_file = os.path.join(offline_data_dir, "hubspot_filtered_to_match_ddhq_dates_latest.parquet")
        
        hubspot_filtered.to_parquet(timestamped_file, index=False)
        hubspot_filtered.to_parquet(latest_file, index=False)
        
        self.logger.info(f"✅ Filtered data saved:")
        self.logger.info(f"   Timestamped: {timestamped_file}")
        self.logger.info(f"   Latest: {latest_file}")
        
        return latest_file

def main():
    """Main temporal filtering process"""
    filter_obj = TemporalFilter()
    
    print("🚀 STARTING TEMPORAL FILTERING")
    print("=" * 50)
    print("Filtering HubSpot candidates to match DDHQ election dates exactly")
    
    try:
        # Load cleaned datasets
        print("📥 Loading cleaned datasets...")
        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")
        
        hubspot_df = pd.read_parquet(os.path.join(offline_data_dir, 'hubspot_candidacy_cleaned_latest.parquet'))
        ddhq_df = pd.read_parquet(os.path.join(offline_data_dir, 'ddhq_election_results_cleaned_latest.parquet'))
        
        print(f"   HubSpot (cleaned): {len(hubspot_df):,} records")
        print(f"   DDHQ (cleaned): {len(ddhq_df):,} records")
        
        # Apply temporal filtering
        hubspot_filtered = filter_obj.filter_hubspot_by_ddhq_dates(hubspot_df, ddhq_df)
        
        # Save filtered data
        output_file = filter_obj.save_filtered_data(hubspot_filtered)
        
        print(f"\n✅ Temporal filtering complete!")
        print(f"   Filtered records: {len(hubspot_filtered):,}")
        print(f"   Output: {output_file}")
        print(f"   Ready for embedding generation")
        
    except Exception as e:
        filter_obj.logger.error(f"❌ Temporal filtering failed: {str(e)}")
        raise

if __name__ == "__main__":
    main()