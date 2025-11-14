#!/usr/bin/env python3
"""
Retry Error Records Script for DDHQ Matcher

This script reads parquet files from the output directory, identifies failed records
(timeouts, errors, unprocessed), and retries them using parallel processing.

Usage:
    uv run hubspot_ddhq_match/retry_errors.py [--file FILENAME] [--all]

Examples:
    # Retry errors in latest output file
    uv run hubspot_ddhq_match/retry_errors.py

    # Retry errors in specific file
    uv run hubspot_ddhq_match/retry_errors.py --file parallel_hubspot_ddhq_matches_20250107_092052.parquet

    # Retry errors in all parquet files
    uv run hubspot_ddhq_match/retry_errors.py --all
"""

import os
import sys
import asyncio
import pandas as pd
import argparse
import json
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime
from tqdm.asyncio import tqdm

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from shared.logger import get_logger

@dataclass
class RetryStats:
    total_error_records: int = 0
    successfully_retried: int = 0
    still_failed: int = 0
    quota_exhausted: int = 0

class ErrorRetryProcessor:
    """Process error records from existing parquet files"""

    def __init__(self, max_workers: int = 200):
        self.logger = get_logger(__name__)
        self.max_workers = max_workers
        self.output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
        self.retry_output_dir = os.path.join(self.output_dir, "retried")
        self.checkpoint_dir = os.path.join(self.output_dir, "checkpoints")
        self.retry_stats = RetryStats()

        os.makedirs(self.retry_output_dir, exist_ok=True)
        os.makedirs(self.checkpoint_dir, exist_ok=True)

    def _save_checkpoint(self, run_id: str, filepath: str, remaining_indices: List[int],
                        completed_count: int, error_records: pd.DataFrame) -> str:
        """Save checkpoint for resuming after quota exhaustion"""
        checkpoint_data = {
            'run_id': run_id,
            'timestamp': datetime.now().isoformat(),
            'original_file': filepath,
            'completed_count': completed_count,
            'remaining_count': len(remaining_indices),
            'remaining_indices': remaining_indices
        }

        checkpoint_file = os.path.join(self.checkpoint_dir, f"checkpoint_{run_id}.json")
        with open(checkpoint_file, 'w') as f:
            json.dump(checkpoint_data, f, indent=2)

        remaining_records = error_records.loc[remaining_indices]
        records_file = os.path.join(self.checkpoint_dir, f"remaining_records_{run_id}.parquet")
        remaining_records.to_parquet(records_file, index=True, engine='pyarrow')

        return checkpoint_file

    def _load_checkpoint(self, run_id: str) -> Dict[str, Any]:
        """Load checkpoint data for resuming"""
        checkpoint_file = os.path.join(self.checkpoint_dir, f"checkpoint_{run_id}.json")

        if not os.path.exists(checkpoint_file):
            raise FileNotFoundError(f"Checkpoint not found: {checkpoint_file}")

        with open(checkpoint_file, 'r') as f:
            checkpoint_data = json.load(f)

        records_file = os.path.join(self.checkpoint_dir, f"remaining_records_{run_id}.parquet")
        if not os.path.exists(records_file):
            raise FileNotFoundError(f"Remaining records file not found: {records_file}")

        checkpoint_data['remaining_records'] = pd.read_parquet(records_file)

        return checkpoint_data

    def _convert_timestamps_to_compatible_format(self, df: pd.DataFrame) -> pd.DataFrame:
        """Convert nanosecond timestamps to microsecond precision for Parquet compatibility"""
        df = df.copy()
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                # Convert datetime64[ns] to datetime64[us] (microseconds)
                df[col] = df[col].astype('datetime64[us]')
        return df

    def find_parquet_files(self, filename: Optional[str] = None, all_files: bool = False) -> List[str]:
        """Find parquet files to process"""
        if filename:
            filepath = os.path.join(self.output_dir, filename)
            if os.path.exists(filepath):
                return [filepath]
            else:
                self.logger.error(f"File not found: {filepath}")
                return []
        elif all_files:
            parquet_files = []
            for file in os.listdir(self.output_dir):
                if file.endswith('.parquet') and not file.startswith('retried_'):
                    parquet_files.append(os.path.join(self.output_dir, file))
            return sorted(parquet_files)
        else:
            latest_file = os.path.join(self.output_dir, "parallel_hubspot_ddhq_matches_latest.parquet")
            if os.path.exists(latest_file):
                return [latest_file]
            else:
                self.logger.error(f"Latest file not found: {latest_file}")
                return []

    def identify_error_records(self, df: pd.DataFrame) -> pd.DataFrame:
        """Identify records that failed (errors, timeouts, unprocessed)"""

        error_mask = pd.Series([False] * len(df))

        # Check for processing errors in reasoning
        if 'llm_reasoning' in df.columns:
            error_mask |= df['llm_reasoning'].str.contains('error', case=False, na=False)
            error_mask |= df['llm_reasoning'].str.contains('timeout', case=False, na=False)
            error_mask |= df['llm_reasoning'].str.contains('failed', case=False, na=False)

        # Check for error markers in top_10_candidates
        if 'top_10_candidates' in df.columns:
            error_mask |= df['top_10_candidates'] == 'ERROR'
            error_mask |= df['top_10_candidates'] == 'CONCURRENT_ERROR'
            error_mask |= df['top_10_candidates'] == 'BATCH_ERROR'

        # Check for null/missing LLM validation
        if 'llm_best_match' in df.columns and 'llm_confidence' in df.columns:
            # Records with confidence = 0 AND error reasoning
            if 'llm_reasoning' in df.columns:
                error_mask |= (
                    (df['llm_confidence'] == 0) &
                    df['llm_reasoning'].str.contains('error|timeout|failed', case=False, na=False)
                )

        error_records = df[error_mask].copy()

        self.logger.info(f"Found {len(error_records)} error records out of {len(df)} total records")

        if len(error_records) > 0:
            # Count different error types
            if 'llm_reasoning' in error_records.columns:
                error_reasons = error_records['llm_reasoning'].value_counts().head(5)
                self.logger.info(f"Top error reasons:")
                for reason, count in error_reasons.items():
                    self.logger.info(f"  - {reason[:100]}: {count} records")

        return error_records

    async def retry_error_records(self, error_records: pd.DataFrame, original_hubspot_df: pd.DataFrame, source_filepath: Optional[str] = None) -> pd.DataFrame:
        """Retry error records in parallel"""
        if error_records.empty:
            return error_records

        total_records = len(error_records)
        self.logger.info(f"🔄 Retrying {total_records} error records in parallel...")

        # Import matcher here to avoid circular dependencies
        from parallel_production_matcher import ParallelProductionMatcher

        # Initialize matcher with same config
        matcher = ParallelProductionMatcher(max_workers=self.max_workers)

        # Create tasks for all error records
        retry_tasks = []
        for idx, row in error_records.iterrows():
            # Get original HubSpot row index
            hubspot_idx = row.get('hubspot_row_index', idx)

            # Get original HubSpot record from the embeddings file
            if hubspot_idx < len(original_hubspot_df):
                hubspot_record = original_hubspot_df.iloc[hubspot_idx]
                task = matcher._process_hubspot_record(hubspot_record, hubspot_idx)
                retry_tasks.append((idx, task))
            else:
                self.logger.warning(f"Could not find HubSpot record for index {hubspot_idx}")

        # Process in groups for optimal throughput
        group_size = min(self.max_workers, 200)
        self.logger.info(f"   Using batch size: {group_size} (parallel processing)")

        results = []
        quota_exhausted = False

        with tqdm(total=len(retry_tasks), desc="Retrying failed tasks", unit="record") as pbar:
            for i in range(0, len(retry_tasks), group_size):
                if quota_exhausted:
                    # Add remaining original records back
                    for j in range(i, len(retry_tasks)):
                        idx, _ = retry_tasks[j]
                        results.append(error_records.loc[idx])
                        pbar.update(1)
                    break

                group = retry_tasks[i:i + group_size]
                group_indices = [idx for idx, _ in group]
                group_tasks = [task for _, task in group]

                # Execute all tasks in group concurrently
                group_results = await asyncio.gather(*group_tasks, return_exceptions=True)

                # Process results and check for quota exhaustion
                for idx, result in zip(group_indices, group_results):
                    original_row = error_records.loc[idx]

                    if isinstance(result, Exception):
                        error_str = str(result).lower()

                        # Check for quota exhaustion
                        if "quota exhausted" in error_str or "resource_exhausted" in error_str:
                            self.logger.error(f"🚨 Quota exhausted during retry - stopping further processing")
                            self.retry_stats.quota_exhausted += 1
                            quota_exhausted = True
                            results.append(original_row)
                        else:
                            # Other exception - log and add original record
                            self.logger.error(f"❌ Error retrying index {idx}: {result}")
                            self.retry_stats.still_failed += 1
                            results.append(original_row)
                    else:
                        # Success - add retried result
                        results.append(result)

                        # Update retry stats
                        if result.get('has_match'):
                            self.retry_stats.successfully_retried += 1
                        elif 'Processing error' in str(result.get('llm_reasoning', '')):
                            self.retry_stats.still_failed += 1
                        else:
                            self.retry_stats.successfully_retried += 1

                    pbar.update(1)

                # If quota exhausted, stop processing and save checkpoint
                if quota_exhausted:
                    remaining_indices = [idx for idx, _ in retry_tasks[i:]]
                    if remaining_indices:
                        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                        checkpoint_file = self._save_checkpoint(
                            run_id=run_id,
                            filepath=source_filepath or "unknown",
                            remaining_indices=remaining_indices,
                            completed_count=len(results),
                            error_records=error_records
                        )
                        self.logger.error(f"💾 Checkpoint saved: {checkpoint_file}")
                        self.logger.error(f"   Resume with: uv run hubspot_ddhq_match/retry_errors.py --resume-checkpoint {run_id}")
                    break

        # Cleanup matcher resources
        matcher.cleanup()

        self.logger.info(f"✅ Retry processing completed: {self.retry_stats.successfully_retried} successful, {self.retry_stats.still_failed} failed")
        if quota_exhausted:
            self.logger.warning(f"⚠️  Processing stopped due to quota exhaustion. {len(remaining_indices)} records remaining.")

        return pd.DataFrame(results)

    async def process_file(self, filepath: str):
        """Process a single parquet file"""
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"Processing file: {os.path.basename(filepath)}")
        self.logger.info(f"{'='*60}")

        # Read the file
        df = pd.read_parquet(filepath)
        self.logger.info(f"Loaded {len(df)} records from {os.path.basename(filepath)}")

        # Identify error records
        error_records = self.identify_error_records(df)

        if error_records.empty:
            self.logger.info("✅ No error records found - skipping retry")
            return

        self.retry_stats.total_error_records = len(error_records)

        # Load original HubSpot data with embeddings
        hubspot_file = os.path.join(
            os.path.dirname(filepath),
            "..",
            "offline_data",
            "hubspot_filtered_with_embeddings_latest.parquet"
        )
        hubspot_file = os.path.abspath(hubspot_file)

        if not os.path.exists(hubspot_file):
            self.logger.error(f"HubSpot embeddings file not found: {hubspot_file}")
            self.logger.error("Cannot retry without original HubSpot data")
            return

        self.logger.info(f"Loading original HubSpot data from {os.path.basename(hubspot_file)}")
        original_hubspot_df = pd.read_parquet(hubspot_file)

        # Retry error records
        retried_records = await self.retry_error_records(error_records, original_hubspot_df, source_filepath=filepath)

        # Merge retried records back into original dataframe
        # Keep successful records, replace error records with retried versions
        success_mask = ~df.index.isin(error_records.index)
        successful_records = df[success_mask].copy()

        # Combine successful + retried records
        final_df = pd.concat([successful_records, retried_records], ignore_index=True)

        # Sort by original index order
        if 'hubspot_row_index' in final_df.columns:
            final_df = final_df.sort_values('hubspot_row_index').reset_index(drop=True)

        # Convert timestamps to compatible format (fix nanosecond precision issue)
        final_df = self._convert_timestamps_to_compatible_format(final_df)

        # Save retried results
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        original_filename = os.path.basename(filepath).replace('.parquet', '')
        retried_filename = f"retried_{original_filename}_{timestamp}.parquet"
        retried_filepath = os.path.join(self.retry_output_dir, retried_filename)

        final_df.to_parquet(retried_filepath, index=False, engine='pyarrow', coerce_timestamps='us')

        # Save latest version
        latest_retried_filepath = os.path.join(self.retry_output_dir, f"retried_{original_filename}_latest.parquet")
        final_df.to_parquet(latest_retried_filepath, index=False, engine='pyarrow', coerce_timestamps='us')

        self.logger.info(f"\n💾 Retried results saved:")
        self.logger.info(f"   {retried_filepath}")
        self.logger.info(f"   {latest_retried_filepath}")

        # Print statistics
        self.logger.info(f"\n📊 RETRY STATISTICS:")
        self.logger.info(f"   Total error records: {self.retry_stats.total_error_records}")
        self.logger.info(f"   Successfully retried: {self.retry_stats.successfully_retried}")
        self.logger.info(f"   Still failed: {self.retry_stats.still_failed}")
        if self.retry_stats.quota_exhausted > 0:
            self.logger.info(f"   Quota exhausted: {self.retry_stats.quota_exhausted}")

        # Calculate final match statistics
        total_records = len(final_df)
        local_matches = final_df['has_match'].sum() if 'has_match' in final_df.columns else 0
        federal_races = (final_df['ddhq_race_name'] == 'FEDERAL_RACE').sum() if 'ddhq_race_name' in final_df.columns else 0
        state_races = (final_df['ddhq_race_name'] == 'STATE_RACE').sum() if 'ddhq_race_name' in final_df.columns else 0
        no_match = total_records - local_matches - federal_races - state_races

        self.logger.info(f"\n📊 FINAL MATCH STATISTICS:")
        self.logger.info(f"   Total records: {total_records:,}")
        self.logger.info(f"   Local/municipal matches: {local_matches:,} ({local_matches/total_records*100:.1f}%)")
        self.logger.info(f"   Federal races: {federal_races:,} ({federal_races/total_records*100:.1f}%)")
        self.logger.info(f"   State races: {state_races:,} ({state_races/total_records*100:.1f}%)")
        self.logger.info(f"   No match: {no_match:,} ({no_match/total_records*100:.1f}%)")

async def main():
    parser = argparse.ArgumentParser(description="Retry error records from DDHQ matcher output files")
    parser.add_argument('--file', type=str, help='Specific parquet file to process')
    parser.add_argument('--all', action='store_true', help='Process all parquet files in output directory')
    parser.add_argument('--max-workers', type=int, default=200, help='Maximum concurrent workers (default: 200)')
    parser.add_argument('--resume-checkpoint', type=str, help='Resume from checkpoint run ID (e.g., 20250111_143052)')

    args = parser.parse_args()

    processor = ErrorRetryProcessor(max_workers=args.max_workers)

    # Handle checkpoint resume
    if args.resume_checkpoint:
        try:
            checkpoint = processor._load_checkpoint(args.resume_checkpoint)
            print(f"\n📂 Resuming from checkpoint: {args.resume_checkpoint}")
            print(f"   Original file: {os.path.basename(checkpoint['original_file'])}")
            print(f"   Completed: {checkpoint['completed_count']} records")
            print(f"   Remaining: {checkpoint['remaining_count']} records")

            error_records = checkpoint['remaining_records']

            # Load original HubSpot data
            hubspot_file = os.path.join(
                processor.output_dir,
                "..",
                "offline_data",
                "hubspot_filtered_with_embeddings_latest.parquet"
            )
            hubspot_file = os.path.abspath(hubspot_file)

            if not os.path.exists(hubspot_file):
                print(f"❌ HubSpot embeddings file not found: {hubspot_file}")
                return

            print(f"📥 Loading HubSpot data from {os.path.basename(hubspot_file)}")
            original_hubspot_df = pd.read_parquet(hubspot_file)

            # Retry remaining records
            retried_records = await processor.retry_error_records(
                error_records,
                original_hubspot_df,
                source_filepath=checkpoint['original_file']
            )

            # Save results
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            retried_filename = f"resumed_{args.resume_checkpoint}_{timestamp}.parquet"
            retried_filepath = os.path.join(processor.retry_output_dir, retried_filename)

            retried_records = processor._convert_timestamps_to_compatible_format(retried_records)
            retried_records.to_parquet(retried_filepath, index=False, engine='pyarrow', coerce_timestamps='us')

            print(f"\n✅ Resumed retry complete!")
            print(f"   Results saved: {retried_filepath}")

        except Exception as e:
            print(f"❌ Error resuming from checkpoint: {e}")
        return

    # Find files to process
    files = processor.find_parquet_files(filename=args.file, all_files=args.all)

    if not files:
        print("❌ No files found to process")
        return

    print(f"🔍 Found {len(files)} file(s) to process")

    # Process each file
    for filepath in files:
        await processor.process_file(filepath)

    print("\n✅ Retry processing complete!")

if __name__ == "__main__":
    asyncio.run(main())
