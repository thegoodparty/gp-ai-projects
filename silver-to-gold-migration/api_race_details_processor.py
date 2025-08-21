import pandas as pd
import requests
import time
import json
import os
from pathlib import Path
from typing import Dict, Any, Optional
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class RaceDetailsProcessor:
    def __init__(self, parquet_file_path: str, output_file_path: Optional[str] = None):
        self.parquet_file_path = parquet_file_path
        self.output_file_path = output_file_path or parquet_file_path.replace('.parquet', '_with_responses.parquet')
        self.api_token = os.getenv('GOODPARTY_API_TOKEN')
        self.base_url = "https://goodparty.org/api/v1/campaigns/admin"
        self.delay_seconds = 5
        
        if not self.api_token:
            raise ValueError("GOODPARTY_API_TOKEN not found in environment variables")
    
    def load_data(self) -> pd.DataFrame:
        """Load the parquet file into a DataFrame"""
        logger.info(f"Loading data from {self.parquet_file_path}")
        df = pd.read_parquet(self.parquet_file_path)
        logger.info(f"Loaded {len(df)} rows")
        return df
    
    def make_api_request(self, candidate_slug: str) -> Dict[str, Any]:
        """Make API request for a single candidate"""
        url = f"{self.base_url}/{candidate_slug}/race-target-details"
        
        headers = {
            'accept': '*/*',
            'content-type': 'application/json',
        }
        
        cookies = {
            'token': self.api_token
        }
        
        try:
            response = requests.put(
                url,
                headers=headers,
                cookies=cookies,
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
            logger.error(f"Request failed for {candidate_slug}: {str(e)}")
            return {
                'status_code': None,
                'response_data': None,
                'success': False,
                'error': str(e)
            }
    
    def process_candidates(self, df: pd.DataFrame, start_index: int = 0, limit: Optional[int] = None) -> pd.DataFrame:
        """Process all candidates and add API responses"""
        total_candidates = len(df)
        end_index = min(total_candidates, start_index + limit) if limit else total_candidates
        logger.info(f"Processing candidates {start_index} to {end_index-1} (limit: {limit if limit else 'none'})")
        
        # Initialize response columns if they don't exist
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
            
            # Skip if already processed (has api_success value)
            if 'api_success' in df.columns and pd.notna(df.at[idx, 'api_success']):
                logger.info(f"Skipping {idx + 1}/{end_index}: {candidate_slug} (already processed)")
                continue
            
            logger.info(f"Processing {idx + 1}/{end_index}: {candidate_slug}")
            
            # Make API request
            response = self.make_api_request(candidate_slug)
            
            # Update DataFrame with response
            df.at[idx, 'api_status_code'] = response['status_code']
            df.at[idx, 'api_response_data'] = json.dumps(response['response_data']) if response['response_data'] else None
            df.at[idx, 'api_success'] = response['success']
            df.at[idx, 'api_error'] = response['error']
            
            # Log result
            if response['success']:
                logger.info(f"✓ Success for {candidate_slug}")
            else:
                logger.warning(f"✗ Failed for {candidate_slug}: {response['error'] or f'Status {response['status_code']}'}")
            
            # Save progress every 10 requests
            if (idx + 1) % 10 == 0:
                self.save_progress(df, idx + 1)
            
            # Rate limiting - wait before next request (except for last item)
            if idx < end_index - 1:
                logger.info(f"Waiting {self.delay_seconds} seconds before next request...")
                time.sleep(self.delay_seconds)
        
        return df
    
    def save_progress(self, df: pd.DataFrame, processed_count: int):
        """Save progress to avoid losing work"""
        progress_file = self.output_file_path.replace('.parquet', f'_progress_{processed_count}.parquet')
        df.to_parquet(progress_file, index=False)
        logger.info(f"Progress saved: {processed_count} candidates processed -> {progress_file}")
    
    def save_final_results(self, df: pd.DataFrame):
        """Save the final results"""
        df.to_parquet(self.output_file_path, index=False)
        logger.info(f"Final results saved to {self.output_file_path}")
        
        # Also save as CSV for easy viewing
        csv_path = self.output_file_path.replace('.parquet', '.csv')
        df.to_csv(csv_path, index=False)
        logger.info(f"Results also saved as CSV: {csv_path}")
    
    def run(self, start_index: int = 0, limit: Optional[int] = None):
        """Main execution method"""
        logger.info("Starting Race Details API Processor")
        
        # Load data
        df = self.load_data()
        
        # Process candidates
        df_with_responses = self.process_candidates(df, start_index, limit)
        
        # Save final results
        self.save_final_results(df_with_responses)
        
        # Print summary
        total_requests = len(df_with_responses)
        successful_requests = df_with_responses['api_success'].sum() if 'api_success' in df_with_responses.columns else 0
        
        logger.info(f"""
=== PROCESSING COMPLETE ===
Total candidates: {total_requests}
Successful requests: {successful_requests}
Failed requests: {total_requests - successful_requests}
Success rate: {(successful_requests / total_requests * 100):.1f}%
Output file: {self.output_file_path}
        """)

def main():
    """Main function for CLI usage"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Process candidates and fetch race details from API')
    parser.add_argument('parquet_file', help='Path to the input parquet file')
    parser.add_argument('--output', '-o', help='Output file path (optional)')
    parser.add_argument('--start-index', '-s', type=int, default=0, help='Start processing from this index (for resuming)')
    parser.add_argument('--limit', '-l', type=int, help='Maximum number of candidates to process')
    
    args = parser.parse_args()
    
    processor = RaceDetailsProcessor(args.parquet_file, args.output)
    processor.run(args.start_index, args.limit)

if __name__ == "__main__":
    main()