#!/usr/bin/env python3

"""
DATA EXTRACTION FOR HUBSPOT-DDHQ MATCHING

Extracts raw data from Databricks tables:
- HubSpot: dbt.m_general__candidacy 
- DDHQ: dbt.stg_airbyte_source__ddhq_gdrive_election_results

Saves as parquet and TSV files in offline_data directory.

USAGE:
uv run data_extraction.py
"""

import sys
import os
import pandas as pd
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from shared.logger import get_logger

class DataExtractor:
    def __init__(self):
        self.logger = get_logger(__name__)
        
        # Import Databricks client from shared directory
        try:
            from shared.databricks_client import DatabricksClient
            self.db_client = DatabricksClient()
            self.logger.debug(f"Successfully imported DatabricksClient from shared directory")
        except ImportError as e:
            self.logger.error(f"Failed to import DatabricksClient from shared directory: {e}")
            raise
    
    def extract_hubspot_candidacy(self) -> pd.DataFrame:
        """Extract HubSpot candidacy data from Databricks"""
        self.logger.info("📥 Extracting HubSpot candidacy data...")
        
        query = """
        SELECT *
        FROM dbt.m_general__candidacy
        ORDER BY updated_at DESC
        """
        
        try:
            df = self.db_client.execute_query(query)
            self.logger.info(f"✅ HubSpot data extracted: {len(df):,} records")
            return df
        except Exception as e:
            self.logger.error(f"❌ Failed to extract HubSpot data: {e}")
            raise
    
    def extract_ddhq_election_results(self) -> pd.DataFrame:
        """Extract DDHQ election results from Databricks"""
        self.logger.info("📥 Extracting DDHQ election results...")
        
        query = """
        SELECT *
        FROM dbt.stg_airbyte_source__ddhq_gdrive_election_results
        ORDER BY date DESC
        """
        
        try:
            df = self.db_client.execute_query(query)
            self.logger.info(f"✅ DDHQ data extracted: {len(df):,} records")
            return df
        except Exception as e:
            self.logger.error(f"❌ Failed to extract DDHQ data: {e}")
            raise
    
    def save_raw_data(self, hubspot_df: pd.DataFrame, ddhq_df: pd.DataFrame):
        """Save raw extracted data to offline_data directory"""
        self.logger.info("💾 Saving raw datasets...")
        
        # Create offline_data directory relative to script location
        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")
        os.makedirs(offline_data_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Save HubSpot data
        hubspot_parquet = os.path.join(offline_data_dir, f"hubspot_candidacy_{timestamp}.parquet")
        hubspot_tsv = os.path.join(offline_data_dir, f"hubspot_candidacy_{timestamp}.tsv")
        hubspot_latest_parquet = os.path.join(offline_data_dir, "hubspot_candidacy_latest.parquet")
        hubspot_latest_tsv = os.path.join(offline_data_dir, "hubspot_candidacy_latest.tsv")
        
        hubspot_df.to_parquet(hubspot_parquet, index=False)
        hubspot_df.to_csv(hubspot_tsv, sep='\t', index=False)
        hubspot_df.to_parquet(hubspot_latest_parquet, index=False)
        hubspot_df.to_csv(hubspot_latest_tsv, sep='\t', index=False)
        
        # Save DDHQ data
        ddhq_parquet = os.path.join(offline_data_dir, f"ddhq_election_results_{timestamp}.parquet")
        ddhq_tsv = os.path.join(offline_data_dir, f"ddhq_election_results_{timestamp}.tsv")
        ddhq_latest_parquet = os.path.join(offline_data_dir, "ddhq_election_results_latest.parquet")
        ddhq_latest_tsv = os.path.join(offline_data_dir, "ddhq_election_results_latest.tsv")
        
        ddhq_df.to_parquet(ddhq_parquet, index=False)
        ddhq_df.to_csv(ddhq_tsv, sep='\t', index=False)
        ddhq_df.to_parquet(ddhq_latest_parquet, index=False)
        ddhq_df.to_csv(ddhq_latest_tsv, sep='\t', index=False)
        
        # Calculate file sizes
        hubspot_size_mb = os.path.getsize(hubspot_parquet) / (1024 * 1024)
        ddhq_size_mb = os.path.getsize(ddhq_parquet) / (1024 * 1024)
        
        self.logger.info(f"✅ Raw data saved:")
        self.logger.info(f"   HubSpot: {hubspot_parquet} ({hubspot_size_mb:.1f} MB)")
        self.logger.info(f"   DDHQ: {ddhq_parquet} ({ddhq_size_mb:.1f} MB)")
        self.logger.info(f"   Latest files also saved for pipeline")
        
        return hubspot_parquet, ddhq_parquet
    
    def print_data_summary(self, hubspot_df: pd.DataFrame, ddhq_df: pd.DataFrame):
        """Print summary of extracted data"""
        print("\n" + "="*60)
        print("📊 DATA EXTRACTION SUMMARY")
        print("="*60)
        
        print(f"\n🏢 HubSpot Candidacy Data:")
        print(f"   Records: {len(hubspot_df):,}")
        print(f"   Columns: {len(hubspot_df.columns)}")
        print(f"   Key columns: {list(hubspot_df.columns[:10])}")
        
        # Check for name fields
        name_cols = [col for col in hubspot_df.columns if 'name' in col.lower()]
        if name_cols:
            print(f"   Name columns: {name_cols}")
        
        # Check for date fields  
        date_cols = [col for col in hubspot_df.columns if 'date' in col.lower() or 'election' in col.lower()]
        if date_cols:
            print(f"   Date columns: {date_cols}")
        
        print(f"\n🗳️  DDHQ Election Results:")
        print(f"   Records: {len(ddhq_df):,}")
        print(f"   Columns: {len(ddhq_df.columns)}")
        print(f"   Key columns: {list(ddhq_df.columns[:10])}")
        
        # Check date range in DDHQ
        if 'date' in ddhq_df.columns:
            ddhq_dates = pd.to_datetime(ddhq_df['date'], errors='coerce')
            valid_dates = ddhq_dates.dropna()
            if len(valid_dates) > 0:
                print(f"   Date range: {valid_dates.min().date()} to {valid_dates.max().date()}")
                print(f"   Valid dates: {len(valid_dates):,} / {len(ddhq_df):,}")
        
        print(f"\n✅ Next step: Run data_cleaning.py")
        print("="*60)

def main():
    """Main data extraction process"""
    extractor = DataExtractor()
    
    print("🚀 STARTING DATA EXTRACTION")
    print("=" * 50)
    print("Extracting HubSpot candidacy and DDHQ election results from Databricks")
    
    try:
        # Extract data from Databricks
        hubspot_df = extractor.extract_hubspot_candidacy()
        ddhq_df = extractor.extract_ddhq_election_results()
        
        # Save raw data
        hubspot_file, ddhq_file = extractor.save_raw_data(hubspot_df, ddhq_df)
        
        # Print summary
        extractor.print_data_summary(hubspot_df, ddhq_df)
        
        print(f"\n🎉 Data extraction complete!")
        
    except Exception as e:
        extractor.logger.error(f"❌ Data extraction failed: {str(e)}")
        raise

if __name__ == "__main__":
    main()