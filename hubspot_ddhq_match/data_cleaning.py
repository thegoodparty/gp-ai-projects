#!/usr/bin/env python3

"""
DATA CLEANING SCRIPT FOR HUBSPOT-DDHQ MATCHING

Cleans both HubSpot and DDHQ datasets to improve matching quality:
- Standardizes name formatting
- Removes data quality issues
- Normalizes geographic information
- Cleans office/race names

USAGE:
uv run data_cleaning.py
"""

import sys
import os
import pandas as pd
import numpy as np
import re
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from shared.logger import get_logger

class DataCleaner:
    def __init__(self):
        self.logger = get_logger(__name__)
        self.stats = {
            'hubspot_original': 0,
            'hubspot_cleaned': 0,
            'hubspot_removed': 0,
            'ddhq_original': 0,
            'ddhq_cleaned': 0,
            'ddhq_removed': 0
        }
    
    def clean_name_field(self, name_series: pd.Series, field_name: str) -> pd.Series:
        """Clean and standardize name fields"""
        original_count = len(name_series)
        
        # Convert to string and strip whitespace
        cleaned = name_series.astype(str).str.strip()
        
        # Remove records with 'nan', empty, or very short names
        cleaned = cleaned.replace('nan', '')
        cleaned = cleaned.replace('', np.nan)
        
        # Fix multiple consecutive spaces
        cleaned = cleaned.str.replace(r'\s+', ' ', regex=True)
        
        # Remove leading/trailing punctuation except hyphens and apostrophes
        cleaned = cleaned.str.replace(r'^[^\w\s\-\']+|[^\w\s\-\']+$', '', regex=True)
        
        # Fix case issues - proper case for names
        def fix_name_case(name):
            if pd.isna(name) or name == '':
                return name
            
            # Don't change names that are already properly formatted
            if name.istitle() or (name.islower() and len(name) <= 3):  # Keep short names like 'Jr'
                return name
            
            # Convert all caps or all lowercase to title case
            if name.isupper() or name.islower():
                return name.title()
            
            return name
        
        cleaned = cleaned.apply(fix_name_case)
        
        # Remove names with digits (likely data corruption)
        mask_no_digits = ~cleaned.str.contains(r'\d', na=False)
        cleaned = cleaned.where(mask_no_digits)
        
        # Remove unreasonably long names (likely data errors)
        mask_reasonable_length = cleaned.str.len() <= 50
        cleaned = cleaned.where(mask_reasonable_length)
        
        # Remove very short names (< 2 chars) except common abbreviations
        common_abbrevs = ['Jr', 'Sr', 'II', 'III', 'IV', 'V']
        mask_not_too_short = (cleaned.str.len() >= 2) | cleaned.isin(common_abbrevs)
        cleaned = cleaned.where(mask_not_too_short)
        
        removed_count = original_count - cleaned.notna().sum()
        self.logger.info(f"Cleaned {field_name}: {removed_count:,} problematic entries removed")
        
        return cleaned
    
    def clean_state_field(self, state_series: pd.Series) -> pd.Series:
        """Clean and standardize state abbreviations"""
        cleaned = state_series.astype(str).str.strip().str.upper()
        
        # Replace 'nan' with actual NaN
        cleaned = cleaned.replace('NAN', '')
        cleaned = cleaned.replace('', np.nan)
        
        # Only keep valid 2-character state codes
        valid_states = {
            'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
            'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
            'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
            'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
            'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY',
            'DC'  # Include District of Columbia
        }
        
        # Only keep valid state codes
        mask_valid_state = cleaned.isin(valid_states)
        cleaned = cleaned.where(mask_valid_state)
        
        return cleaned
    
    def clean_office_field(self, office_series: pd.Series, field_name: str) -> pd.Series:
        """Clean office/race name fields"""
        cleaned = office_series.astype(str).str.strip()
        
        # Replace 'nan' and 'None' with actual NaN
        cleaned = cleaned.replace(['nan', 'None'], '')
        cleaned = cleaned.replace('', np.nan)
        
        # Fix multiple spaces
        cleaned = cleaned.str.replace(r'\s+', ' ', regex=True)
        
        # Remove unreasonably long office names (likely data corruption)
        mask_reasonable_length = cleaned.str.len() <= 150
        cleaned = cleaned.where(mask_reasonable_length)
        
        # Standardize common office name patterns
        def standardize_office_name(office):
            if pd.isna(office):
                return office
            
            # Common standardizations
            office = re.sub(r'\bBoard\b', 'Board', office, flags=re.IGNORECASE)
            office = re.sub(r'\bCouncil\b', 'Council', office, flags=re.IGNORECASE)
            office = re.sub(r'\bMayor\b', 'Mayor', office, flags=re.IGNORECASE)
            office = re.sub(r'\bSchool\b', 'School', office, flags=re.IGNORECASE)
            office = re.sub(r'\bDistrict\b', 'District', office, flags=re.IGNORECASE)
            
            return office
        
        cleaned = cleaned.apply(standardize_office_name)
        
        return cleaned
    
    def expand_hubspot_elections(self, df: pd.DataFrame) -> pd.DataFrame:
        """Expand HubSpot candidates into separate primary/general records"""
        self.logger.info("🔄 Expanding HubSpot candidates into election-specific records...")
        
        original_count = len(df)
        expanded_records = []
        
        for _, row in df.iterrows():
            # Primary election record
            if pd.notna(row.get('primary_election_date')):
                primary_row = row.copy()
                primary_row['election_type'] = 'primary'
                primary_row['election_date'] = primary_row['primary_election_date']
                # Clear the other election dates to avoid confusion
                primary_row['general_election_date'] = pd.NaT
                primary_row['runoff_election_date'] = pd.NaT
                expanded_records.append(primary_row)
            
            # General election record  
            if pd.notna(row.get('general_election_date')):
                general_row = row.copy()
                general_row['election_type'] = 'general'
                general_row['election_date'] = general_row['general_election_date']
                # Clear the other election date to avoid confusion
                general_row['primary_election_date'] = pd.NaT
                general_row['runoff_election_date'] = pd.NaT
                expanded_records.append(general_row)
            
            # Runoff election record
            if pd.notna(row.get('runoff_election_date')):
                runoff_row = row.copy()
                runoff_row['election_type'] = 'runoff'
                runoff_row['election_date'] = runoff_row['runoff_election_date']
                # Clear the other election dates to avoid confusion
                runoff_row['primary_election_date'] = pd.NaT
                runoff_row['general_election_date'] = pd.NaT
                expanded_records.append(runoff_row)
        
        expanded_df = pd.DataFrame(expanded_records)
        final_count = len(expanded_df)
        
        self.logger.info(f"✅ Election expansion complete: {original_count:,} → {final_count:,} records")
        return expanded_df
    
    def clean_hubspot_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """Clean HubSpot candidate data"""
        self.logger.info("🧹 Cleaning HubSpot data...")
        
        original_count = len(df)
        self.stats['hubspot_original'] = original_count
        
        # Clean name fields
        df = df.copy()
        df['first_name'] = self.clean_name_field(df['first_name'], 'first_name')
        df['last_name'] = self.clean_name_field(df['last_name'], 'last_name')
        
        # Clean geographic fields
        df['state'] = self.clean_state_field(df['state'])
        
        # Clean office fields
        df['candidate_office'] = self.clean_office_field(df['candidate_office'], 'candidate_office')
        df['official_office_name'] = self.clean_office_field(df['official_office_name'], 'official_office_name')
        
        # Remove records without basic name information
        has_name = df['first_name'].notna() & df['last_name'].notna()
        df = df[has_name]
        
        # Expand into election-specific records (primary and general)
        df = self.expand_hubspot_elections(df)
        
        # Clean party affiliation
        if 'party_affiliation' in df.columns:
            df['party_affiliation'] = df['party_affiliation'].astype(str).str.strip()
            df['party_affiliation'] = df['party_affiliation'].replace(['nan', 'None'], '')
            df['party_affiliation'] = df['party_affiliation'].replace('', np.nan)
            
            # Standardize common party names
            party_standardization = {
                'nonpartisan': 'Nonpartisan',
                'independent': 'Independent', 
                'Nonparisan': 'Nonpartisan',
                'Nonpartisian': 'Nonpartisan',
                'Non Partisan': 'Nonpartisan',
                'Libertarian Party': 'Libertarian'
            }
            df['party_affiliation'] = df['party_affiliation'].replace(party_standardization)
        
        final_count = len(df)
        removed_count = original_count - final_count
        self.stats['hubspot_cleaned'] = final_count
        self.stats['hubspot_removed'] = removed_count
        
        self.logger.info(f"✅ HubSpot cleaning complete: {original_count:,} → {final_count:,} records ({removed_count:,} removed)")
        
        return df
    
    def clean_ddhq_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """Clean DDHQ election data"""
        self.logger.info("🧹 Cleaning DDHQ data...")
        
        original_count = len(df)
        self.stats['ddhq_original'] = original_count
        
        df = df.copy()
        
        # Clean candidate names
        df['candidate'] = df['candidate'].astype(str).str.strip()
        
        # Fix multiple spaces in candidate names
        df['candidate'] = df['candidate'].str.replace(r'\s+', ' ', regex=True)
        
        # Standardize candidate name case (title case)
        def fix_candidate_case(name):
            if pd.isna(name) or name == '':
                return name
            # Most names should be title case
            return name.title() if not name.istitle() else name
        
        df['candidate'] = df['candidate'].apply(fix_candidate_case)
        
        # Clean race names
        df['race_name'] = df['race_name'].astype(str).str.strip()
        
        # Remove records with missing critical information
        has_candidate = df['candidate'].notna() & (df['candidate'] != '')
        has_race = df['race_name'].notna() & (df['race_name'] != '')
        df = df[has_candidate & has_race]
        
        # Extract state from race name and add as separate column for easier matching
        df['extracted_state'] = df['race_name'].str[:2].str.upper()
        
        # Validate extracted states
        valid_states = {
            'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
            'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
            'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
            'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
            'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY',
            'DC'
        }
        
        # Only keep records with valid state prefixes
        has_valid_state = df['extracted_state'].isin(valid_states)
        df = df[has_valid_state]
        
        # Clean party affiliations
        if 'candidate_party' in df.columns:
            df['candidate_party'] = df['candidate_party'].astype(str).str.strip()
            df['candidate_party'] = df['candidate_party'].replace(['nan', 'None'], '')
            df['candidate_party'] = df['candidate_party'].replace('', np.nan)
        
        # Normalize election types to match HubSpot format
        df = self._normalize_ddhq_election_types(df)
        
        final_count = len(df)
        removed_count = original_count - final_count
        self.stats['ddhq_cleaned'] = final_count
        self.stats['ddhq_removed'] = removed_count
        
        self.logger.info(f"✅ DDHQ cleaning complete: {original_count:,} → {final_count:,} records ({removed_count:,} removed)")
        
        return df
    
    def _normalize_ddhq_election_types(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normalize DDHQ election types to match HubSpot format using pattern matching"""
        if 'election_type' not in df.columns:
            self.logger.warning("No election_type column found in DDHQ data")
            return df
        
        # Store original for debugging/reference
        df['election_type_original'] = df['election_type'].copy()
        
        # Apply flexible pattern matching in order (like your original pipeline)
        # Order matters: primary first, then runoff, then general (most specific to least specific)
        def normalize_election_type(election_type_str):
            if pd.isna(election_type_str):
                return election_type_str
            
            election_type_lower = str(election_type_str).lower()
            
            # Check in order: primary -> runoff -> general
            if 'primary' in election_type_lower:
                return 'primary'
            elif 'runoff' in election_type_lower:
                return 'runoff' 
            elif 'general' in election_type_lower:
                return 'general'
            else:
                # Fallback for unmatched types
                return election_type_str
        
        # Apply normalization
        df['election_type_normalized'] = df['election_type'].apply(normalize_election_type)
        
        # Count normalizations
        normalized_count = (df['election_type_normalized'] != df['election_type']).sum()
        unmapped_types = df[df['election_type_normalized'] == df['election_type']]['election_type'].unique()
        # Filter out NaN from unmapped_types
        unmapped_types = [t for t in unmapped_types if pd.notna(t)]
        
        # Replace original with normalized
        df['election_type'] = df['election_type_normalized']
        
        # Clean up temporary column
        df = df.drop('election_type_normalized', axis=1)
        
        self.logger.info(f"✅ Election type normalization: {normalized_count:,} types normalized")
        
        if len(unmapped_types) > 0:
            self.logger.warning(f"⚠️ Unmapped election types found: {list(unmapped_types)}")
        
        # Log the normalization results
        type_counts = df['election_type'].value_counts()
        for election_type, count in type_counts.items():
            self.logger.debug(f"   {election_type}: {count:,} records")
        
        return df
    
    def save_cleaned_data(self, hubspot_df: pd.DataFrame, ddhq_df: pd.DataFrame):
        """Save cleaned datasets"""
        self.logger.info("💾 Saving cleaned datasets...")
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Get script directory for saving files
        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")
        os.makedirs(offline_data_dir, exist_ok=True)
        
        # Save HubSpot cleaned data
        hubspot_file = os.path.join(offline_data_dir, f"hubspot_candidacy_cleaned_{timestamp}.parquet")
        hubspot_latest = os.path.join(offline_data_dir, "hubspot_candidacy_cleaned_latest.parquet")
        
        hubspot_df.to_parquet(hubspot_file, index=False)
        hubspot_df.to_parquet(hubspot_latest, index=False)
        
        # Save DDHQ cleaned data
        ddhq_file = os.path.join(offline_data_dir, f"ddhq_election_results_cleaned_{timestamp}.parquet")
        ddhq_latest = os.path.join(offline_data_dir, "ddhq_election_results_cleaned_latest.parquet")
        
        ddhq_df.to_parquet(ddhq_file, index=False)
        ddhq_df.to_parquet(ddhq_latest, index=False)
        
        self.logger.info(f"✅ Cleaned data saved:")
        self.logger.info(f"   HubSpot: {hubspot_file}")
        self.logger.info(f"   DDHQ: {ddhq_file}")
        
        return hubspot_file, ddhq_file
    
    def print_cleaning_summary(self):
        """Print summary of cleaning operations"""
        print("\n" + "="*60)
        print("📊 DATA CLEANING SUMMARY")
        print("="*60)
        
        print(f"\n🏢 HubSpot Candidates:")
        print(f"   Original records: {self.stats['hubspot_original']:,}")
        print(f"   Cleaned records:  {self.stats['hubspot_cleaned']:,}")
        print(f"   Removed records:  {self.stats['hubspot_removed']:,}")
        print(f"   Retention rate:   {self.stats['hubspot_cleaned']/self.stats['hubspot_original']*100:.1f}%")
        
        print(f"\n🗳️  DDHQ Elections:")
        print(f"   Original records: {self.stats['ddhq_original']:,}")
        print(f"   Cleaned records:  {self.stats['ddhq_cleaned']:,}")
        print(f"   Removed records:  {self.stats['ddhq_removed']:,}")
        print(f"   Retention rate:   {self.stats['ddhq_cleaned']/self.stats['ddhq_original']*100:.1f}%")
        
        print(f"\n✅ Next step: Generate embeddings for cleaned data")
        print("="*60)

def main():
    """Main cleaning process"""
    cleaner = DataCleaner()
    
    print("🚀 STARTING DATA CLEANING PROCESS")
    print("="*50)
    
    try:
        # Load raw data
        print("📥 Loading raw datasets...")
        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")
        
        hubspot_df = pd.read_parquet(os.path.join(offline_data_dir, 'hubspot_candidacy_latest.parquet'))
        ddhq_df = pd.read_parquet(os.path.join(offline_data_dir, 'ddhq_election_results_latest.parquet'))
        
        print(f"   HubSpot: {len(hubspot_df):,} records")
        print(f"   DDHQ: {len(ddhq_df):,} records")
        
        # Clean datasets
        hubspot_cleaned = cleaner.clean_hubspot_data(hubspot_df)
        ddhq_cleaned = cleaner.clean_ddhq_data(ddhq_df)
        
        # Save cleaned data
        hubspot_file, ddhq_file = cleaner.save_cleaned_data(hubspot_cleaned, ddhq_cleaned)
        
        # Print summary
        cleaner.print_cleaning_summary()
        
    except Exception as e:
        cleaner.logger.error(f"❌ Data cleaning failed: {str(e)}")
        raise

if __name__ == "__main__":
    main()