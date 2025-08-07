#!/usr/bin/env python3
"""
Retry Error Records Script

This script reads parquet files from the output directory, identifies records with 
"Processing error" in the llm_reason field or "QUOTA_ERROR" in the l2_district_name 
field, and retries them using the same production matching logic.

Usage:
    uv run stitch_golden_data/prod_gold_data/retry_errors.py [--file FILENAME] [--all]
    
Examples:
    # Retry errors in specific file
    uv run stitch_golden_data/prod_gold_data/retry_errors.py --file full_state_matching_nd.parquet
    
    # Retry errors in all parquet files
    uv run stitch_golden_data/prod_gold_data/retry_errors.py --all
"""

import os
import sys
import asyncio
import pandas as pd
import argparse
from typing import List, Optional
from dataclasses import dataclass

# Add parent directory to path to import shared modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from production_matcher import ProductionMatcher
from shared.logger import get_logger

@dataclass
class RetryStats:
    total_error_records: int = 0
    total_quota_errors: int = 0
    successfully_retried: int = 0
    still_failed: int = 0
    quota_exhausted: int = 0

class ErrorRetryProcessor:
    """Process error records from existing parquet files"""
    
    def __init__(self):
        self.logger = get_logger(__name__)
        self.matcher = ProductionMatcher()
        self.output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
        self.fixed_output_dir = os.path.join(self.output_dir, "fixed")
        self.retry_stats = RetryStats()
        
        # Create fixed output directory
        os.makedirs(self.fixed_output_dir, exist_ok=True)
        
    def find_parquet_files(self, filename: Optional[str] = None) -> List[str]:
        """Find parquet files to process"""
        if filename:
            # Process specific file
            filepath = os.path.join(self.output_dir, filename)
            if os.path.exists(filepath):
                return [filepath]
            else:
                self.logger.error(f"File not found: {filepath}")
                return []
        else:
            # Find all parquet files in output directory
            parquet_files = []
            for file in os.listdir(self.output_dir):
                if file.endswith('.parquet'):
                    parquet_files.append(os.path.join(self.output_dir, file))
            return sorted(parquet_files)
    
    def identify_error_records(self, df: pd.DataFrame) -> pd.DataFrame:
        """Identify records that have processing errors or quota errors"""
        if 'llm_reason' not in df.columns and 'l2_district_name' not in df.columns:
            self.logger.warning("No 'llm_reason' or 'l2_district_name' column found in DataFrame")
            return pd.DataFrame()
        
        # Find records with "Processing error" in llm_reason OR QUOTA_ERROR in l2_district_name
        error_mask = pd.Series([False] * len(df))
        
        if 'llm_reason' in df.columns:
            error_mask |= df['llm_reason'].str.contains('Processing error', case=False, na=False)
        
        if 'l2_district_name' in df.columns:
            error_mask |= df['l2_district_name'] == 'QUOTA_ERROR'
        
        error_records = df[error_mask].copy()
        
        self.logger.info(f"Found {len(error_records)} error/quota records out of {len(df)} total records")
        
        if len(error_records) > 0:
            # Count different error types
            quota_errors = (error_records['l2_district_name'] == 'QUOTA_ERROR').sum() if 'l2_district_name' in error_records.columns else 0
            processing_errors = len(error_records) - quota_errors
            
            self.logger.info(f"  - Processing errors: {processing_errors}")
            self.logger.info(f"  - Quota errors: {quota_errors}")
            
            # Show sample of error reasons
            sample_errors = error_records.head(3)
            for i, (_, row) in enumerate(sample_errors.iterrows(), 1):
                if row.get('l2_district_name') == 'QUOTA_ERROR':
                    self.logger.info(f"Sample error {i}: QUOTA_ERROR - {row.get('llm_reason', 'N/A')[:100]}...")
                else:
                    self.logger.info(f"Sample error {i}: {row.get('llm_reason', 'N/A')[:100]}...")
        
        return error_records
    
    async def retry_error_records(self, error_records: pd.DataFrame) -> pd.DataFrame:
        """Retry processing for error records"""
        if error_records.empty:
            return error_records
        
        self.logger.info(f"🔄 Retrying {len(error_records)} error records...")
        
        # Convert error records back to the format expected by match_single_record
        retried_records = []
        
        for idx, row in error_records.iterrows():
            try:
                self.logger.debug(f"Retrying record: {row.get('name', 'Unknown')} ({row.get('state', 'Unknown')})")
                
                # Use the production matcher to retry this record
                retried_row = await self.matcher.match_single_record(row)
                retried_records.append(retried_row)
                
                # Update retry stats
                if retried_row['is_matched']:
                    self.retry_stats.successfully_retried += 1
                    self.logger.debug(f"✅ Successfully retried: {row.get('name', 'Unknown')}")
                elif 'Processing error' in str(retried_row.get('llm_reason', '')):
                    self.retry_stats.still_failed += 1
                    self.logger.debug(f"❌ Still failed: {row.get('name', 'Unknown')}")
                else:
                    # Not matched but no processing error (e.g., NOT_MATCHED)
                    self.retry_stats.successfully_retried += 1
                    self.logger.debug(f"✅ Successfully processed (no match): {row.get('name', 'Unknown')}")
                
            except Exception as e:
                error_str = str(e).lower()
                if ("quota exhausted" in error_str or "resource_exhausted" in error_str):
                    self.logger.error(f"🚨 Quota exhausted during retry - stopping")
                    self.retry_stats.quota_exhausted += 1
                    # Add the original error record back
                    retried_records.append(row)
                    break
                else:
                    self.logger.error(f"❌ Error retrying record {row.get('name', 'Unknown')}: {e}")
                    self.retry_stats.still_failed += 1
                    # Add the original error record back
                    retried_records.append(row)
        
        return pd.DataFrame(retried_records)
    
    def merge_retried_records(self, original_df: pd.DataFrame, error_records: pd.DataFrame, retried_records: pd.DataFrame) -> pd.DataFrame:
        """Merge retried records back into the original DataFrame"""
        if retried_records.empty:
            return original_df, []
        
        # Create a copy of the original DataFrame
        updated_df = original_df.copy()
        fixed_row_indices = []
        
        # Update the rows that were retried
        for idx, retried_row in retried_records.iterrows():
            # Find the matching original record by ID or index
            if 'id' in retried_row and 'id' in updated_df.columns:
                match_mask = updated_df['id'] == retried_row['id']
            elif 'br_database_id' in retried_row and 'br_database_id' in updated_df.columns:
                match_mask = updated_df['br_database_id'] == retried_row['br_database_id']
            else:
                # Fallback: match by name and state
                match_mask = (
                    (updated_df['name'] == retried_row['name']) & 
                    (updated_df['state'] == retried_row['state'])
                )
            
            if match_mask.any():
                # Track which rows were fixed
                fixed_row_indices.extend(updated_df[match_mask].index.tolist())
                # Update the matched rows with retried data
                for col in retried_row.index:
                    if col in updated_df.columns:
                        updated_df.loc[match_mask, col] = retried_row[col]
        
        return updated_df, fixed_row_indices
    
    def save_updated_file(self, df: pd.DataFrame, original_filepath: str, fixed_row_indices: List[int], error_records: pd.DataFrame):
        """Save the updated DataFrame to the fixed output directory"""
        # Get the original filename
        original_filename = os.path.basename(original_filepath)
        
        # Create fixed filenames
        fixed_parquet_path = os.path.join(self.fixed_output_dir, original_filename)
        fixed_tsv_path = os.path.join(self.fixed_output_dir, original_filename.replace('.parquet', '_fixed.tsv'))
        
        # Save fixed parquet file
        df.to_parquet(fixed_parquet_path, index=False)
        self.logger.info(f"💾 Fixed parquet saved: {fixed_parquet_path}")
        
        # Save TSV version with metadata
        df.to_csv(fixed_tsv_path, index=False, sep='\t')
        
        # Read original TSV to get original metadata if exists
        original_tsv_path = original_filepath.replace('.parquet', '.tsv')
        original_metadata = []
        if os.path.exists(original_tsv_path):
            with open(original_tsv_path, 'r') as f:
                lines = f.readlines()
                capturing = False
                for line in lines:
                    if line.strip().startswith('# PRODUCTION MATCHING METADATA'):
                        capturing = True
                    if capturing:
                        original_metadata.append(line.rstrip())
        
        # Calculate retry statistics
        successfully_fixed = len([idx for idx in fixed_row_indices if df.iloc[idx]['is_matched'] or 'NOT_MATCHED' in str(df.iloc[idx]['llm_reason'])])
        
        # Get sample of fixed records for metadata
        fixed_samples = []
        for i, idx in enumerate(fixed_row_indices[:5]):  # First 5 fixed records
            row = df.iloc[idx]
            district_name = row.get('l2_district_name', 'Unknown')
            fixed_samples.append(f"#   Row {idx+1}: {row['name']} -> {district_name if row['is_matched'] else 'NOT_MATCHED'}")
        
        # Append both original and retry metadata to TSV
        metadata = []
        
        # Add original metadata if found
        if original_metadata:
            metadata.extend(["\n# ORIGINAL FILE METADATA:"])
            metadata.extend(original_metadata)
        
        # Add retry processing metadata
        metadata.extend([
            f"\n# RETRY PROCESSING METADATA:",
            f"# Original file: {original_filename}",
            f"# Retry timestamp: {pd.Timestamp.now().isoformat()}",
            f"# Total records in file: {len(df):,}",
            f"# Error records found: {len(error_records)}",
            f"# Records successfully retried: {self.retry_stats.successfully_retried}",
            f"# Records still failed: {self.retry_stats.still_failed}",
            f"# Quota exhausted during retry: {self.retry_stats.quota_exhausted}",
            f"# Row indices fixed (1-based): {', '.join(str(idx+1) for idx in sorted(fixed_row_indices[:20]))}{'...' if len(fixed_row_indices) > 20 else ''}",
            f"# Number of rows fixed: {len(fixed_row_indices)}",
            f"# Fixed records now matched: {successfully_fixed}",
            f"# Sample of fixed records:"
        ])
        metadata.extend(fixed_samples)
        
        # Add cost information if available
        if hasattr(self.matcher, 'stats') and self.matcher.stats.total_cost > 0:
            metadata.extend([
                f"# Retry processing costs:",
                f"#   Total Cost: ${self.matcher.stats.total_cost:.6f}",
                f"#   Embedding Cost: ${self.matcher.stats.embedding_cost:.6f}",
                f"#   LLM Cost: ${self.matcher.stats.llm_cost:.6f}"
            ])
        
        with open(fixed_tsv_path, 'a') as f:
            f.write('\n'.join(metadata))
        
        self.logger.info(f"💾 Fixed TSV saved: {fixed_tsv_path}")
        
        return {
            'parquet': fixed_parquet_path,
            'tsv': fixed_tsv_path
        }
    
    async def process_file(self, filepath: str):
        """Process a single parquet file for error retry"""
        filename = os.path.basename(filepath)
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"PROCESSING FILE: {filename}")
        self.logger.info(f"{'='*80}")
        
        try:
            # Load the parquet file
            df = pd.read_parquet(filepath)
            self.logger.info(f"📁 Loaded {len(df):,} records from {filename}")
            
            # Identify error records
            error_records = self.identify_error_records(df)
            self.retry_stats.total_error_records += len(error_records)
            
            # Count quota errors
            if 'l2_district_name' in error_records.columns:
                quota_error_count = (error_records['l2_district_name'] == 'QUOTA_ERROR').sum()
                self.retry_stats.total_quota_errors += quota_error_count
            
            if error_records.empty:
                self.logger.info("✅ No error records found - nothing to retry")
                return
            
            # Retry error records
            retried_records = await self.retry_error_records(error_records)
            
            # Merge retried records back into original DataFrame
            updated_df, fixed_row_indices = self.merge_retried_records(df, error_records, retried_records)
            
            # Save fixed file to output/fixed directory
            output_paths = self.save_updated_file(updated_df, filepath, fixed_row_indices, error_records)
            
            # Report on this file
            success_count = len(error_records) - self.retry_stats.still_failed
            self.logger.info(f"📊 File Results: {success_count}/{len(error_records)} errors resolved")
            
        except Exception as e:
            self.logger.error(f"❌ Error processing {filename}: {e}")
    
    async def process_multiple_files(self, filepaths: List[str]):
        """Process multiple parquet files"""
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"ERROR RETRY PROCESSOR - {len(filepaths)} FILES")
        self.logger.info(f"{'='*80}")
        
        for i, filepath in enumerate(filepaths, 1):
            self.logger.info(f"Processing file {i}/{len(filepaths)}")
            await self.process_file(filepath)
            
            # Brief pause between files
            if i < len(filepaths):
                await asyncio.sleep(1)
    
    def print_final_summary(self):
        """Print final summary of retry processing"""
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"ERROR RETRY FINAL SUMMARY")
        self.logger.info(f"{'='*80}")
        self.logger.info(f"Total Error Records Found: {self.retry_stats.total_error_records:,}")
        self.logger.info(f"  - Processing Errors: {self.retry_stats.total_error_records - self.retry_stats.total_quota_errors:,}")
        self.logger.info(f"  - Quota Errors: {self.retry_stats.total_quota_errors:,}")
        self.logger.info(f"Successfully Retried: {self.retry_stats.successfully_retried:,}")
        self.logger.info(f"Still Failed: {self.retry_stats.still_failed:,}")
        self.logger.info(f"Quota Exhausted During Retry: {self.retry_stats.quota_exhausted:,}")
        
        if self.retry_stats.total_error_records > 0:
            success_rate = (self.retry_stats.successfully_retried / self.retry_stats.total_error_records) * 100
            self.logger.info(f"Retry Success Rate: {success_rate:.1f}%")
        
        # Get updated cost information
        self.matcher.update_cost_stats()
        stats = self.matcher.stats
        if stats.total_cost > 0:
            self.logger.info(f"\n💰 RETRY COSTS:")
            self.logger.info(f"Total Cost: ${stats.total_cost:.6f}")
            self.logger.info(f"Embedding Cost: ${stats.embedding_cost:.6f}")
            self.logger.info(f"LLM Cost: ${stats.llm_cost:.6f}")

async def main():
    parser = argparse.ArgumentParser(description="Retry error records from parquet files")
    parser.add_argument('--file', '-f', type=str, help='Specific parquet file to process')
    parser.add_argument('--all', '-a', action='store_true', help='Process all parquet files in output directory')
    parser.add_argument('--dry-run', '-d', action='store_true', help='Show what would be processed without making changes')
    
    args = parser.parse_args()
    
    processor = ErrorRetryProcessor()
    
    if args.file:
        filepaths = processor.find_parquet_files(filename=args.file)
    elif args.all:
        filepaths = processor.find_parquet_files()
    else:
        print("Please specify either --file FILENAME or --all")
        print("Use --help for more information")
        return
    
    if not filepaths:
        print("No parquet files found to process")
        return
    
    print(f"Found {len(filepaths)} file(s) to process:")
    for filepath in filepaths:
        filename = os.path.basename(filepath)
        print(f"  - {filename}")
    
    if args.dry_run:
        print("\n🔍 DRY RUN - analyzing error records without retrying...")
        for filepath in filepaths:
            df = pd.read_parquet(filepath)
            error_records = processor.identify_error_records(df)
            print(f"{os.path.basename(filepath)}: {len(error_records)} error records")
        return
    
    # Confirm before processing
    if len(filepaths) > 1:
        confirm = input(f"\nProcess {len(filepaths)} files? Fixed files will be saved to output/fixed/ directory. (y/n): ")
        if confirm.lower() not in ['y', 'yes']:
            print("Cancelled")
            return
    
    try:
        await processor.process_multiple_files(filepaths)
        processor.print_final_summary()
    except KeyboardInterrupt:
        print("\n👋 Processing interrupted by user")
    except Exception as e:
        print(f"❌ Error during processing: {e}")

if __name__ == "__main__":
    asyncio.run(main())