#!/usr/bin/env python3

"""
DATA EXTRACTION FOR HUBSPOT-GOOGLE SHEETS MATCHING

Extracts raw data from:
- HubSpot: stg_airbyte_source__hubspot_api_companies (Databricks)
- Google Sheets: Restructured Data tab

Saves as parquet and TSV files in offline_data directory.

USAGE:
uv run data_extraction.py
"""

import sys
import os
import pandas as pd
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))
from shared.logger import get_logger
from shared.databricks_client import DatabricksClient
from shared.google_sheets_client import GoogleSheetsClient

SPREADSHEET_ID = '1SnTjTOWjl-m694DZY0TA2ZplYKY_J6m-lyYhhsu_vNs'

class DataExtractor:
    def __init__(self):
        self.logger = get_logger(__name__)
        self.db_client = DatabricksClient()

    def extract_hubspot_companies(self) -> pd.DataFrame:
        """Extract HubSpot companies data from Databricks"""
        self.logger.info("📥 Extracting HubSpot companies data...")

        query = """
        SELECT
          id,
          properties_candidate_name,
          properties_candidate_office,
          properties_official_office_name,
          properties_office_level,
          properties_state,
          properties_city,
          properties_candidate_district,
          properties_candidate_party,
          properties_election_date,
          properties_primary_date,
          properties_runoff_date
        FROM goodparty_data_catalog.dbt.stg_airbyte_source__hubspot_api_companies
        WHERE properties_candidate_office IS NOT NULL
        ORDER BY properties_election_date DESC
        """

        try:
            df = self.db_client.execute_query(query)
            self.logger.info(f"✅ HubSpot companies extracted: {len(df):,} records")
            return df
        except Exception as e:
            self.logger.error(f"❌ Failed to extract HubSpot companies: {e}")
            raise

    def extract_google_sheets_races(self) -> pd.DataFrame:
        """Extract Google Sheets races data"""
        self.logger.info("📥 Extracting Google Sheets races data...")

        try:
            token_path = os.path.join(os.path.dirname(__file__), 'token.pickle')

            with GoogleSheetsClient(token_path=token_path) as client:
                raw_data = client.read_sheet(SPREADSHEET_ID, 'Restructured Data')

            if not raw_data:
                raise ValueError("No data retrieved from Google Sheets")

            self.logger.info(f"Raw Google Sheets data: {len(raw_data)} rows")
            self.logger.debug(f"Header row: {raw_data[0]}")

            df = pd.DataFrame(raw_data[1:])

            if len(df.columns) == 4:
                df.columns = [
                    'race_id',
                    'date',
                    'state_election_type',
                    'race_name_orig'
                ]
                df['election_type_clean'] = df['state_election_type']
                df['race_name_with_state'] = df['race_name_orig']
            elif len(df.columns) == 6:
                df.columns = [
                    'race_id_empty',
                    'date',
                    'state_election_type',
                    'race_name_orig',
                    'election_type_clean',
                    'race_name_with_state'
                ]
            else:
                raise ValueError(f"Unexpected number of columns: {len(df.columns)}. Expected 4 or 6.")

            self.logger.info(f"✅ Google Sheets races extracted: {len(df):,} records")
            self.logger.debug(f"Columns: {list(df.columns)}")

            return df

        except Exception as e:
            self.logger.error(f"❌ Failed to extract Google Sheets races: {e}")
            raise

    def save_raw_data(self, hubspot_df: pd.DataFrame, google_sheets_df: pd.DataFrame):
        """Save raw extracted data to offline_data directory"""
        self.logger.info("💾 Saving raw datasets...")

        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")
        os.makedirs(offline_data_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        hubspot_parquet = os.path.join(offline_data_dir, "hubspot_companies_raw_latest.parquet")
        hubspot_tsv = os.path.join(offline_data_dir, f"hubspot_companies_raw_{timestamp}.tsv")

        google_parquet = os.path.join(offline_data_dir, "google_sheets_races_raw_latest.parquet")
        google_tsv = os.path.join(offline_data_dir, f"google_sheets_races_raw_{timestamp}.tsv")

        hubspot_df.to_parquet(hubspot_parquet, index=False)
        hubspot_df.to_csv(hubspot_tsv, sep='\t', index=False)
        self.logger.info(f"✅ HubSpot companies saved:")
        self.logger.info(f"   - {hubspot_parquet}")
        self.logger.info(f"   - {hubspot_tsv}")

        google_sheets_df.to_parquet(google_parquet, index=False)
        google_sheets_df.to_csv(google_tsv, sep='\t', index=False)
        self.logger.info(f"✅ Google Sheets races saved:")
        self.logger.info(f"   - {google_parquet}")
        self.logger.info(f"   - {google_tsv}")

    def run(self):
        """Execute complete extraction pipeline"""
        self.logger.info("🚀 Starting data extraction...")

        hubspot_df = self.extract_hubspot_companies()
        google_sheets_df = self.extract_google_sheets_races()

        self.save_raw_data(hubspot_df, google_sheets_df)

        self.logger.info("✅ Data extraction complete!")
        self.logger.info(f"   - HubSpot companies: {len(hubspot_df):,} records")
        self.logger.info(f"   - Google Sheets races: {len(google_sheets_df):,} records")


def main():
    """Main execution"""
    print("="*80)
    print("HUBSPOT-GOOGLE SHEETS DATA EXTRACTION")
    print("="*80)

    try:
        extractor = DataExtractor()
        extractor.run()

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        raise


if __name__ == "__main__":
    main()
