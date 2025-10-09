#!/usr/bin/env python3

"""
DATA CLEANING FOR HUBSPOT-GOOGLE SHEETS MATCHING

Cleans and standardizes both datasets:
- HubSpot: Companies table with election expansion
- Google Sheets: Race records with election type normalization

Key features:
- Election type normalization (primary/runoff/general pattern matching)
- Election expansion (split records with multiple election dates)
- State code standardization
- Date parsing and conversion

USAGE:
uv run data_cleaning.py
"""

import sys
import os
import pandas as pd
import re
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))
from shared.logger import get_logger

class DataCleaner:
    def __init__(self):
        self.logger = get_logger(__name__)
        self.stats = {}

    def normalize_election_type(self, text: str) -> str:
        """
        Normalize election type using pattern matching

        Rules:
        - '%primary%' -> 'primary'
        - '%runoff%' -> 'runoff'
        - '%general%' -> 'general'
        - default -> 'general'
        """
        if pd.isna(text) or text == '':
            return 'general'

        text_lower = str(text).lower()

        if 'primary' in text_lower:
            return 'primary'
        elif 'runoff' in text_lower:
            return 'runoff'
        elif 'general' in text_lower:
            return 'general'
        else:
            return 'general'

    def standardize_state_code(self, state: str) -> str:
        """Standardize state to 2-letter code"""
        if pd.isna(state) or state == '':
            return None

        state_str = str(state).strip()

        if len(state_str) == 2:
            return state_str.upper()

        state_mapping = {
            'ALABAMA': 'AL', 'ALASKA': 'AK', 'ARIZONA': 'AZ', 'ARKANSAS': 'AR',
            'CALIFORNIA': 'CA', 'COLORADO': 'CO', 'CONNECTICUT': 'CT', 'DELAWARE': 'DE',
            'FLORIDA': 'FL', 'GEORGIA': 'GA', 'HAWAII': 'HI', 'IDAHO': 'ID',
            'ILLINOIS': 'IL', 'INDIANA': 'IN', 'IOWA': 'IA', 'KANSAS': 'KS',
            'KENTUCKY': 'KY', 'LOUISIANA': 'LA', 'MAINE': 'ME', 'MARYLAND': 'MD',
            'MASSACHUSETTS': 'MA', 'MICHIGAN': 'MI', 'MINNESOTA': 'MN', 'MISSISSIPPI': 'MS',
            'MISSOURI': 'MO', 'MONTANA': 'MT', 'NEBRASKA': 'NE', 'NEVADA': 'NV',
            'NEW HAMPSHIRE': 'NH', 'NEW JERSEY': 'NJ', 'NEW MEXICO': 'NM', 'NEW YORK': 'NY',
            'NORTH CAROLINA': 'NC', 'NORTH DAKOTA': 'ND', 'OHIO': 'OH', 'OKLAHOMA': 'OK',
            'OREGON': 'OR', 'PENNSYLVANIA': 'PA', 'RHODE ISLAND': 'RI', 'SOUTH CAROLINA': 'SC',
            'SOUTH DAKOTA': 'SD', 'TENNESSEE': 'TN', 'TEXAS': 'TX', 'UTAH': 'UT',
            'VERMONT': 'VT', 'VIRGINIA': 'VA', 'WASHINGTON': 'WA', 'WEST VIRGINIA': 'WV',
            'WISCONSIN': 'WI', 'WYOMING': 'WY'
        }

        return state_mapping.get(state_str.upper(), state_str.upper()[:2])

    def excel_date_to_datetime(self, excel_date):
        """Convert Excel serial date to datetime"""
        if pd.isna(excel_date) or excel_date == '':
            return None
        try:
            base_date = datetime(1899, 12, 30)
            return base_date + pd.Timedelta(days=int(excel_date))
        except:
            return None

    def clean_hubspot_companies(self, df: pd.DataFrame) -> pd.DataFrame:
        """Clean HubSpot companies data with election expansion"""
        self.logger.info("🧹 Cleaning HubSpot companies data...")

        original_count = len(df)
        self.stats['hubspot_original'] = original_count

        df = df.copy()

        df['office_name'] = df['properties_candidate_office'].fillna('')
        df['official_office_name'] = df['properties_official_office_name'].fillna('')
        df['state'] = df['properties_state'].apply(self.standardize_state_code)
        df['city'] = df['properties_city'].fillna('')
        df['district'] = df['properties_candidate_district'].fillna('')
        df['party'] = df['properties_candidate_party'].fillna('')

        df['primary_date'] = pd.to_datetime(df['properties_primary_date'], errors='coerce')
        df['general_date'] = pd.to_datetime(df['properties_election_date'], errors='coerce')
        df['runoff_date'] = pd.to_datetime(df['properties_runoff_date'], errors='coerce')

        has_basic_info = (
            (df['office_name'].str.strip() != '') |
            (df['official_office_name'].str.strip() != '')
        ) & df['state'].notna()

        df = df[has_basic_info].copy()

        self.logger.info(f"   - After basic filtering: {len(df):,} companies")

        expanded_records = []

        for idx, row in df.iterrows():
            base_record = {
                'company_id': row['id'],
                'candidate_name': row['properties_candidate_name'],
                'office_name': row['office_name'],
                'official_office_name': row['official_office_name'],
                'state': row['state'],
                'city': row['city'],
                'district': row['district'],
                'party': row['party'],
            }

            if pd.notna(row['primary_date']):
                record = base_record.copy()
                record['election_date'] = row['primary_date']
                record['election_type'] = 'primary'
                expanded_records.append(record)

            if pd.notna(row['general_date']):
                record = base_record.copy()
                record['election_date'] = row['general_date']
                record['election_type'] = 'general'
                expanded_records.append(record)

            if pd.notna(row['runoff_date']):
                record = base_record.copy()
                record['election_date'] = row['runoff_date']
                record['election_type'] = 'runoff'
                expanded_records.append(record)

        cleaned_df = pd.DataFrame(expanded_records)

        if len(cleaned_df) == 0:
            self.logger.warning("No records after election expansion!")
            return cleaned_df

        cleaned_df = cleaned_df[cleaned_df['election_date'].notna()].copy()

        self.stats['hubspot_after_expansion'] = len(cleaned_df)
        self.logger.info(f"✅ HubSpot cleaning complete:")
        self.logger.info(f"   - Original companies: {original_count:,}")
        self.logger.info(f"   - After expansion: {len(cleaned_df):,}")

        return cleaned_df

    def clean_google_sheets_races(self, df: pd.DataFrame) -> pd.DataFrame:
        """Clean Google Sheets races data"""
        self.logger.info("🧹 Cleaning Google Sheets races data...")

        original_count = len(df)
        self.stats['google_sheets_original'] = original_count

        df = df.copy()

        df['date_converted'] = df['date'].apply(self.excel_date_to_datetime)

        df['state_code'] = df['race_name_with_state'].str.extract(r'^([A-Z]{2})\s+', expand=False)

        df['election_type_from_state'] = df['state_election_type'].apply(self.normalize_election_type)
        df['election_type_from_clean'] = df['election_type_clean'].apply(self.normalize_election_type)

        df['election_type_normalized'] = df['election_type_from_state']

        df['race_name_cleaned'] = df['race_name_with_state'].str.replace(r'^[A-Z]{2}\s+', '', regex=True)
        df['race_name_cleaned'] = df['race_name_cleaned'].str.replace(r'\s+(General|Primary|Runoff|Special)$', '', regex=True)

        has_required_data = (
            df['date_converted'].notna() &
            df['state_code'].notna() &
            (df['race_name_cleaned'].str.strip() != '')
        )

        df = df[has_required_data].copy()

        final_df = df[[
            'date_converted',
            'state_code',
            'state_election_type',
            'election_type_normalized',
            'race_name_cleaned',
            'race_name_with_state',
            'race_name_orig'
        ]].copy()

        final_df.columns = [
            'date',
            'state',
            'state_election_type_raw',
            'election_type',
            'race_name',
            'race_name_with_state',
            'race_name_orig'
        ]

        self.stats['google_sheets_cleaned'] = len(final_df)
        self.logger.info(f"✅ Google Sheets cleaning complete:")
        self.logger.info(f"   - Original races: {original_count:,}")
        self.logger.info(f"   - After cleaning: {len(final_df):,}")

        return final_df

    def save_cleaned_data(self, hubspot_df: pd.DataFrame, google_sheets_df: pd.DataFrame):
        """Save cleaned data to offline_data directory"""
        self.logger.info("💾 Saving cleaned datasets...")

        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        hubspot_parquet = os.path.join(offline_data_dir, "hubspot_companies_cleaned_latest.parquet")
        hubspot_tsv = os.path.join(offline_data_dir, f"hubspot_companies_cleaned_{timestamp}.tsv")

        google_parquet = os.path.join(offline_data_dir, "google_sheets_races_cleaned_latest.parquet")
        google_tsv = os.path.join(offline_data_dir, f"google_sheets_races_cleaned_{timestamp}.tsv")

        hubspot_df.to_parquet(hubspot_parquet, index=False)
        hubspot_df.to_csv(hubspot_tsv, sep='\t', index=False)
        self.logger.info(f"✅ HubSpot companies saved:")
        self.logger.info(f"   - {hubspot_parquet}")

        google_sheets_df.to_parquet(google_parquet, index=False)
        google_sheets_df.to_csv(google_tsv, sep='\t', index=False)
        self.logger.info(f"✅ Google Sheets races saved:")
        self.logger.info(f"   - {google_parquet}")

    def run(self):
        """Execute complete cleaning pipeline"""
        self.logger.info("🚀 Starting data cleaning...")

        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")

        hubspot_raw = pd.read_parquet(os.path.join(offline_data_dir, "hubspot_companies_raw_latest.parquet"))
        google_sheets_raw = pd.read_parquet(os.path.join(offline_data_dir, "google_sheets_races_raw_latest.parquet"))

        self.logger.info(f"   - Loaded {len(hubspot_raw):,} HubSpot companies")
        self.logger.info(f"   - Loaded {len(google_sheets_raw):,} Google Sheets races")

        hubspot_cleaned = self.clean_hubspot_companies(hubspot_raw)
        google_sheets_cleaned = self.clean_google_sheets_races(google_sheets_raw)

        self.save_cleaned_data(hubspot_cleaned, google_sheets_cleaned)

        self.logger.info("✅ Data cleaning complete!")
        self.logger.info(f"   - HubSpot: {self.stats['hubspot_original']:,} → {self.stats['hubspot_after_expansion']:,}")
        self.logger.info(f"   - Google Sheets: {self.stats['google_sheets_original']:,} → {self.stats['google_sheets_cleaned']:,}")


def main():
    """Main execution"""
    print("="*80)
    print("HUBSPOT-GOOGLE SHEETS DATA CLEANING")
    print("="*80)

    try:
        cleaner = DataCleaner()
        cleaner.run()

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        raise


if __name__ == "__main__":
    main()
