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
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

sys.path.append(str(Path(__file__).parent.parent.parent))

from shared.logger import get_logger

class SilverToGoldPipelineV2:
    """
    Simplified pipeline for processing pre-vetted campaign slugs.

    V2 improvements:
    - No Databricks queries needed (slugs pre-filtered from production DB)
    - No date filtering (already filtered in source SQL query)
    - Direct API processing only
    """

    def __init__(self, slugs_csv_path: str, output_dir: Optional[str] = None):
        self.logger = get_logger(__name__)
        self.slugs_csv_path = slugs_csv_path
        self.output_dir = Path(output_dir) if output_dir else Path(__file__).parent / "data"
        self.output_dir.mkdir(exist_ok=True, parents=True)

        self.api_token = os.getenv('GOODPARTY_API_TOKEN')
        self.base_url = "https://goodparty.org/api/v1/campaigns/admin"
        self.delay_seconds = 0.5  # Default 0.5 seconds between requests

        if not self.api_token:
            raise ValueError("GOODPARTY_API_TOKEN not found in environment variables")

    def set_delay(self, delay_seconds: float):
        """Set the delay between API requests"""
        self.delay_seconds = delay_seconds
        self.logger.info(f"Delay set to {delay_seconds} seconds between requests")

    def load_slugs(self) -> pd.DataFrame:
        """Load slugs from plain text CSV (one slug per line, no headers)"""
        self.logger.info(f"📥 Loading slugs from: {self.slugs_csv_path}")

        with open(self.slugs_csv_path, 'r') as f:
            slugs = [line.strip() for line in f if line.strip()]

        df = pd.DataFrame({'candidate_slug': slugs})

        self.logger.info(f"  Loaded {len(df):,} candidate slugs")
        return df

    def make_api_request(self, candidate_slug: str) -> Dict[str, Any]:
        """Make PUT request to update race-target-details for a candidate"""
        url = f"{self.base_url}/{candidate_slug}/race-target-details"

        headers = {
            'accept': '*/*',
            'content-type': 'application/json',
        }

        cookies = {
            'token': self.api_token
        }

        # Add query parameter to skip turnout calculation
        params = {
            'includeTurnout': 'false'
        }

        try:
            response = requests.put(
                url,
                headers=headers,
                cookies=cookies,
                params=params,
                json={},
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
        """Process API calls for all slugs with progress tracking"""
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

        # Initialize result columns
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

            # Skip if already processed
            if pd.notna(df.at[idx, 'api_success']):
                self.logger.info(f"  [{idx + 1}/{end_index}] Skipping {candidate_slug} (already processed)")
                continue

            self.logger.info(f"  [{idx + 1}/{end_index}] Processing: {candidate_slug}")

            response = self.make_api_request(candidate_slug)

            # Store results
            df.at[idx, 'api_status_code'] = response['status_code']
            df.at[idx, 'api_response_data'] = json.dumps(response['response_data']) if response['response_data'] else None
            df.at[idx, 'api_success'] = response['success']
            df.at[idx, 'api_error'] = response['error']

            if response['success']:
                self.logger.info(f"    ✅ Success")
            else:
                self.logger.warning(f"    ❌ Failed: {response['error'] or f'Status {response['status_code']}'}")

            # Save progress every 10 requests
            if (idx + 1) % 10 == 0:
                self.save_progress(df, timestamp, idx + 1)

            # Delay before next request (except for last one)
            if idx < end_index - 1:
                time.sleep(self.delay_seconds)

        return df

    def save_progress(self, df: pd.DataFrame, timestamp: str, processed_count: int):
        """Save progress checkpoint"""
        progress_path = self.output_dir / f"results_progress_{timestamp}_{processed_count}.parquet"
        df.to_parquet(progress_path, index=False)
        self.logger.info(f"    💾 Progress saved: {processed_count} processed")

    def save_final_results(self, df: pd.DataFrame, timestamp: str):
        """Save final results with summary statistics"""
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
        """Run the complete pipeline"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        self.logger.info("🚀 STARTING SILVER TO GOLD MIGRATION PIPELINE V2")
        self.logger.info("="*60)

        # Load slugs (no Databricks queries needed)
        df = self.load_slugs()

        self.logger.info(f"\n📊 PIPELINE SUMMARY:")
        self.logger.info(f"  Total slugs: {len(df):,}")
        self.logger.info(f"  Ready for processing: {len(df):,}")
        self.logger.info(f"  (No filtering needed - slugs pre-vetted from production DB)")

        # Process API calls
        processed_df = self.process_api_calls(df, timestamp, start_index, limit)

        # Save final results
        final_csv = self.save_final_results(processed_df, timestamp)

        self.logger.info(f"\n" + "="*60)
        self.logger.info("✅ PIPELINE COMPLETE!")
        self.logger.info("="*60)
        self.logger.info(f"Final results: {final_csv}")

        return final_csv


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run silver-to-gold migration pipeline V2 (simplified)")
    parser.add_argument('slugs_csv', help='Path to CSV file with campaign slugs (one per line)')
    parser.add_argument('--output-dir', help='Output directory for results')
    parser.add_argument('--start-index', type=int, default=0, help='Start processing from this index')
    parser.add_argument('--limit', type=int, help='Limit number of slugs to process')
    parser.add_argument('--delay', type=float, default=0.5,
                       help='Delay between API requests in seconds (default: 0.5)')
    parser.add_argument('--test', action='store_true', help='Test mode: process only first 5 slugs')

    args = parser.parse_args()

    if args.test:
        args.limit = 5
        print("🧪 TEST MODE: Processing first 5 slugs only")

    pipeline = SilverToGoldPipelineV2(args.slugs_csv, args.output_dir)
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
