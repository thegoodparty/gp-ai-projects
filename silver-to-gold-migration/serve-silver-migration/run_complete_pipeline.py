#!/usr/bin/env python3

import os
import sys
import pandas as pd
import requests
import time
import json
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

sys.path.append(str(Path(__file__).parent.parent.parent))

from shared.databricks_client import DatabricksClient
from shared.logger import get_logger

class SilverToGoldPipeline:
    def __init__(self, hubspot_csv_path: str, output_dir: Optional[str] = None):
        self.logger = get_logger(__name__)
        self.hubspot_csv_path = hubspot_csv_path
        self.output_dir = Path(output_dir) if output_dir else Path(__file__).parent / "data"
        self.output_dir.mkdir(exist_ok=True, parents=True)

        self.databricks = DatabricksClient()
        self.api_token = os.getenv('GOODPARTY_API_TOKEN')
        self.base_url = "https://goodparty.org/api/v1/campaigns/admin"
        self.delay_seconds = 0.5  # Default 0.5 seconds between requests

        if not self.api_token:
            raise ValueError("GOODPARTY_API_TOKEN not found in environment variables")

    def set_delay(self, delay_seconds: float):
        """Set the delay between API requests"""
        self.delay_seconds = delay_seconds
        self.logger.info(f"Delay set to {delay_seconds} seconds between requests")

    def load_hubspot_csv(self) -> pd.DataFrame:
        self.logger.info(f"📥 Loading HubSpot CSV: {self.hubspot_csv_path}")
        df = pd.read_csv(self.hubspot_csv_path)
        self.logger.info(f"  Loaded {len(df):,} candidates")
        return df

    def fetch_slug_mappings(self) -> pd.DataFrame:
        self.logger.info("📥 Fetching campaign slug mappings from Databricks")

        query = """
        SELECT
            id as product_campaign_id,
            slug as candidate_slug
        FROM goodparty_data_catalog.dbt.stg_airbyte_source__gp_api_db_campaign
        WHERE slug IS NOT NULL
        """

        df = self.databricks.execute_query(query)
        self.logger.info(f"  Fetched {len(df):,} campaign slugs")
        return df

    def fetch_hubspot_candidacy(self) -> pd.DataFrame:
        self.logger.info("📥 Fetching HubSpot candidacy data from Databricks")

        query = """
        SELECT
            company_id,
            product_campaign_id,
            full_name
        FROM goodparty_data_catalog.dbt.m_general__candidacy
        WHERE company_id IS NOT NULL
        """

        df = self.databricks.execute_query(query)
        self.logger.info(f"  Fetched {len(df):,} candidacy records")
        return df

    def map_csv_to_slugs(self, csv_df: pd.DataFrame, candidacy_df: pd.DataFrame,
                         slug_df: pd.DataFrame) -> pd.DataFrame:
        self.logger.info("🔄 Mapping CSV candidates to campaign slugs")

        csv_df['Record ID'] = csv_df['Record ID'].astype(str)
        candidacy_df['company_id'] = candidacy_df['company_id'].astype(str)
        candidacy_df['product_campaign_id'] = pd.to_numeric(
            candidacy_df['product_campaign_id'], errors='coerce'
        ).fillna(0).astype(int).astype(str)
        slug_df['product_campaign_id'] = slug_df['product_campaign_id'].astype(int).astype(str)

        step1 = csv_df.merge(
            candidacy_df[['company_id', 'product_campaign_id', 'full_name']],
            left_on='Record ID',
            right_on='company_id',
            how='left'
        )

        step2 = step1.merge(
            slug_df[['product_campaign_id', 'candidate_slug']],
            on='product_campaign_id',
            how='left'
        )

        matched = step2['candidate_slug'].notna().sum()
        total = len(csv_df)

        self.logger.info(f"  ✅ Matched: {matched:,} / {total:,} ({matched/total*100:.1f}%)")

        return step2

    def recover_missing_slugs(self, df: pd.DataFrame, slug_df: pd.DataFrame) -> pd.DataFrame:
        self.logger.info("🔍 Recovering missing slugs via name matching")

        import re

        def build_slug(name):
            if pd.isna(name) or not name:
                return None
            name = str(name).lower()
            name = re.sub(r'[^a-z0-9\s-]', '', name)
            name = re.sub(r'\s+', '-', name)
            return name.strip('-')

        unmatched = df[df['candidate_slug'].isna()].copy()

        if len(unmatched) == 0:
            self.logger.info("  No unmatched candidates to recover")
            return df

        unmatched['potential_slug'] = unmatched['Company name'].apply(build_slug)
        potential_slugs = unmatched['potential_slug'].dropna().unique().tolist()

        self.logger.info(f"  Searching for {len(potential_slugs)} potential slugs")

        slug_list = "','".join([s for s in potential_slugs if s])
        query = f"""
        SELECT id as product_campaign_id, slug as candidate_slug
        FROM goodparty_data_catalog.dbt.stg_airbyte_source__gp_api_db_campaign
        WHERE slug IN ('{slug_list}')
        """

        matches = self.databricks.execute_query(query)
        matches['potential_slug'] = matches['candidate_slug']

        unmatched_with_slugs = unmatched.merge(
            matches[['potential_slug', 'candidate_slug']],
            on='potential_slug',
            how='left',
            suffixes=('_old', '')
        )

        recovered_count = unmatched_with_slugs['candidate_slug'].notna().sum()
        self.logger.info(f"  ✅ Recovered {recovered_count} additional slugs")

        matched = df[df['candidate_slug'].notna()]
        newly_matched = unmatched_with_slugs[unmatched_with_slugs['candidate_slug'].notna()]

        return pd.concat([matched, newly_matched], ignore_index=True)

    def filter_past_elections(self, df: pd.DataFrame) -> pd.DataFrame:
        self.logger.info("📅 Filtering candidates to only include past elections")

        df['Election Date'] = pd.to_datetime(df['Election Date'])
        today = datetime.now()

        past_elections = df[df['Election Date'] < today].copy()
        future_elections = df[df['Election Date'] >= today].copy()

        if len(future_elections) > 0:
            self.logger.warning(f"  ⚠️  Filtered out {len(future_elections)} candidates with future elections:")
            for _, row in future_elections.iterrows():
                self.logger.warning(f"      - {row['Company name']} ({row['Election Date'].strftime('%Y-%m-%d')})")

        self.logger.info(f"  ✅ Kept {len(past_elections):,} candidates with past elections")
        self.logger.info(f"  ⚠️  Excluded {len(future_elections):,} candidates with future elections")

        return past_elections.reset_index(drop=True)

    def save_candidates_with_slugs(self, df: pd.DataFrame, timestamp: str) -> str:
        self.logger.info("💾 Saving candidates with slugs")

        csv_path = self.output_dir / f"candidates_with_slugs_{timestamp}.csv"
        parquet_path = self.output_dir / f"candidates_with_slugs_{timestamp}.parquet"

        df.to_csv(csv_path, index=False)
        df.to_parquet(parquet_path, index=False)

        self.logger.info(f"  Saved: {csv_path}")
        self.logger.info(f"  Saved: {parquet_path}")

        return str(parquet_path)

    def make_api_request(self, candidate_slug: str) -> Dict[str, Any]:
        url = f"{self.base_url}/{candidate_slug}/race-target-details"

        headers = {
            'accept': '*/*',
            'content-type': 'application/json',
        }

        cookies = {
            'token': self.api_token
        }

        payload = {
            'includeTurnout': False
        }

        try:
            response = requests.put(
                url,
                headers=headers,
                cookies=cookies,
                json=payload,
                timeout=30
            )

            return {
                'status_code': response.status_code,
                'response_data': response.json() if response.headers.get('content-type', '').startswith('application/json') else response.text,
                'success': response.status_code == 200,
                'error': None
            }

        except requests.exceptions.RequestException as e:
            return {
                'status_code': None,
                'response_data': None,
                'success': False,
                'error': str(e)
            }

    def process_api_calls(self, df: pd.DataFrame, timestamp: str,
                         start_index: int = 0, limit: Optional[int] = None) -> pd.DataFrame:
        total = len(df)
        end_index = min(total, start_index + limit) if limit else total

        estimated_time = (end_index - start_index) * self.delay_seconds / 60
        requests_per_min = 60 / self.delay_seconds if self.delay_seconds > 0 else float('inf')

        self.logger.info(f"🔄 Processing API calls for {end_index - start_index} candidates")
        self.logger.info(f"  Start index: {start_index}")
        self.logger.info(f"  End index: {end_index}")
        self.logger.info(f"  Delay per request: {self.delay_seconds}s")
        self.logger.info(f"  Throughput: {requests_per_min:.0f} requests/minute")
        self.logger.info(f"  Estimated time: {estimated_time:.1f} minutes ({estimated_time/60:.1f} hours)")

        if 'api_status_code' not in df.columns:
            df['api_status_code'] = None
        if 'api_response_data' not in df.columns:
            df['api_response_data'] = None
        if 'api_success' not in df.columns:
            df['api_success'] = None
        if 'api_error' not in df.columns:
            df['api_error'] = None

        for idx in range(start_index, end_index):
            row = df.iloc[idx]
            candidate_slug = row['candidate_slug']

            if pd.notna(df.at[idx, 'api_success']):
                self.logger.info(f"  [{idx + 1}/{end_index}] Skipping {candidate_slug} (already processed)")
                continue

            self.logger.info(f"  [{idx + 1}/{end_index}] Processing: {candidate_slug}")

            response = self.make_api_request(candidate_slug)

            df.at[idx, 'api_status_code'] = response['status_code']
            df.at[idx, 'api_response_data'] = json.dumps(response['response_data']) if response['response_data'] else None
            df.at[idx, 'api_success'] = response['success']
            df.at[idx, 'api_error'] = response['error']

            if response['success']:
                self.logger.info(f"    ✅ Success")
            else:
                self.logger.warning(f"    ❌ Failed: {response['error'] or f'Status {response['status_code']}'}")

            if (idx + 1) % 10 == 0:
                self.save_progress(df, timestamp, idx + 1)

            if idx < end_index - 1:
                time.sleep(self.delay_seconds)

        return df

    def save_progress(self, df: pd.DataFrame, timestamp: str, processed_count: int):
        progress_path = self.output_dir / f"results_progress_{timestamp}_{processed_count}.parquet"
        df.to_parquet(progress_path, index=False)
        self.logger.info(f"    💾 Progress saved: {processed_count} processed")

    def save_final_results(self, df: pd.DataFrame, timestamp: str):
        self.logger.info("💾 Saving final results")

        csv_path = self.output_dir / f"results_final_{timestamp}.csv"
        parquet_path = self.output_dir / f"results_final_{timestamp}.parquet"

        df.to_csv(csv_path, index=False)
        df.to_parquet(parquet_path, index=False)

        self.logger.info(f"  Saved: {csv_path}")
        self.logger.info(f"  Saved: {parquet_path}")

        successful = df['api_success'].sum() if 'api_success' in df.columns else 0
        failed = len(df) - successful

        self.logger.info(f"\n📊 FINAL RESULTS:")
        self.logger.info(f"  Total processed: {len(df):,}")
        self.logger.info(f"  Successful: {successful:,} ({successful/len(df)*100:.1f}%)")
        self.logger.info(f"  Failed: {failed:,} ({failed/len(df)*100:.1f}%)")

        return str(csv_path)

    def run(self, start_index: int = 0, limit: Optional[int] = None):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        self.logger.info("🚀 STARTING SILVER TO GOLD MIGRATION PIPELINE")
        self.logger.info("="*60)

        if not self.databricks.test_connection():
            raise Exception("Failed to connect to Databricks")

        csv_df = self.load_hubspot_csv()

        slug_df = self.fetch_slug_mappings()
        candidacy_df = self.fetch_hubspot_candidacy()

        mapped_df = self.map_csv_to_slugs(csv_df, candidacy_df, slug_df)

        recovered_df = self.recover_missing_slugs(mapped_df, slug_df)

        matched_df = recovered_df[recovered_df['candidate_slug'].notna()].copy()
        matched_df = matched_df.drop_duplicates(subset=['Record ID'], keep='first')

        self.logger.info(f"\n📊 CANDIDATES WITH SLUGS:")
        self.logger.info(f"  Total in CSV: {len(csv_df):,}")
        self.logger.info(f"  With valid slugs: {len(matched_df):,} ({len(matched_df)/len(csv_df)*100:.1f}%)")
        self.logger.info(f"  Without slugs: {len(csv_df) - len(matched_df):,}")

        past_elections_df = self.filter_past_elections(matched_df)

        self.logger.info(f"\n📊 FINAL CANDIDATE COUNTS:")
        self.logger.info(f"  Total in CSV: {len(csv_df):,}")
        self.logger.info(f"  With valid slugs: {len(matched_df):,}")
        self.logger.info(f"  With past elections: {len(past_elections_df):,}")
        self.logger.info(f"  Ready for processing: {len(past_elections_df):,}")

        slugs_path = self.save_candidates_with_slugs(past_elections_df, timestamp)

        self.logger.info(f"\n" + "="*60)
        self.logger.info("PHASE 1 COMPLETE: Slug mapping saved")
        self.logger.info("="*60)

        processed_df = self.process_api_calls(past_elections_df, timestamp, start_index, limit)

        final_csv = self.save_final_results(processed_df, timestamp)

        self.logger.info(f"\n" + "="*60)
        self.logger.info("✅ PIPELINE COMPLETE!")
        self.logger.info("="*60)
        self.logger.info(f"Final results: {final_csv}")

        return final_csv


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run complete silver-to-gold migration pipeline")
    parser.add_argument('hubspot_csv', help='Path to HubSpot CSV export')
    parser.add_argument('--output-dir', help='Output directory for results')
    parser.add_argument('--start-index', type=int, default=0, help='Start processing from this index')
    parser.add_argument('--limit', type=int, help='Limit number of candidates to process')
    parser.add_argument('--delay', type=float, default=0.5,
                       help='Delay between API requests in seconds (default: 0.5)')
    parser.add_argument('--test', action='store_true', help='Test mode: process only first 5 candidates')

    args = parser.parse_args()

    if args.test:
        args.limit = 5
        print("🧪 TEST MODE: Processing first 5 candidates only")

    pipeline = SilverToGoldPipeline(args.hubspot_csv, args.output_dir)
    pipeline.set_delay(args.delay)

    try:
        final_csv = pipeline.run(args.start_index, args.limit)
        print(f"\n✅ SUCCESS!")
        print(f"Results saved to: {final_csv}")
        return 0
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit(main())
