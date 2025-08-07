#!/usr/bin/env python3

import pandas as pd
import os
from pathlib import Path
from typing import List
import time

def merge_all_state_parquets():
    """Merge all individual state parquet files into a single full_state_matching.parquet file"""
    
    output_path = Path("stitch_golden_data/prod_gold_data/output")
    
    # Get all individual state parquet files
    state_files = list(output_path.glob("full_state_matching_*.parquet"))
    
    print(f"🔍 Found {len(state_files)} individual state parquet files")
    print("=" * 60)
    
    if not state_files:
        print("❌ No state parquet files found!")
        return
    
    all_dataframes = []
    total_records = 0
    
    # Load and combine all state files
    print("📁 Loading individual state files:")
    for state_file in sorted(state_files):
        state = state_file.stem.split("_")[-1].upper()
        
        try:
            df = pd.read_parquet(state_file)
            record_count = len(df)
            total_records += record_count
            
            all_dataframes.append(df)
            print(f"  ✅ {state}: {record_count:,} records")
            
        except Exception as e:
            print(f"  ❌ {state}: Error reading file - {e}")
    
    if not all_dataframes:
        print("❌ No data to merge!")
        return
    
    print(f"\n🔄 Merging {len(all_dataframes)} state files...")
    start_time = time.time()
    
    # Combine all dataframes
    merged_df = pd.concat(all_dataframes, ignore_index=True)
    
    # Verify record count
    merged_count = len(merged_df)
    if merged_count != total_records:
        print(f"⚠️  Warning: Record count mismatch! Expected {total_records:,}, got {merged_count:,}")
    else:
        print(f"✅ Record count verified: {merged_count:,} records")
    
    # Sort by state and then by original BR database ID for consistent ordering
    print("🔄 Sorting by state and br_database_id...")
    if 'state' in merged_df.columns and 'br_database_id' in merged_df.columns:
        merged_df = merged_df.sort_values(['state', 'br_database_id']).reset_index(drop=True)
    elif 'state' in merged_df.columns:
        merged_df = merged_df.sort_values('state').reset_index(drop=True)
    
    # Save merged file
    merged_filename = "full_state_matching.parquet"
    merged_path = output_path / merged_filename
    
    print(f"💾 Saving merged file: {merged_filename}")
    merged_df.to_parquet(merged_path, index=False)
    
    merge_time = time.time() - start_time
    
    # Print summary
    print(f"\n{'='*60}")
    print("MERGE SUMMARY")
    print(f"{'='*60}")
    print(f"States merged: {len(all_dataframes)}")
    print(f"Total records: {merged_count:,}")
    print(f"Merge time: {merge_time:.1f} seconds")
    print(f"Output file: {merged_path}")
    print(f"File size: {merged_path.stat().st_size / (1024*1024):.1f} MB")
    
    # Print column summary
    print(f"\nColumns in merged file ({len(merged_df.columns)}):")
    for col in merged_df.columns:
        print(f"  - {col}")
    
    # Print state breakdown
    if 'state' in merged_df.columns:
        state_counts = merged_df['state'].value_counts().sort_index()
        print(f"\nRecords by state:")
        for state, count in state_counts.items():
            print(f"  {state}: {count:,} records")
    
    # Print matching statistics if available
    if 'is_matched' in merged_df.columns:
        total_matched = merged_df['is_matched'].sum()
        match_rate = (total_matched / merged_count) * 100
        print(f"\nMatching statistics:")
        print(f"  Total matched: {total_matched:,} ({match_rate:.1f}%)")
        print(f"  Not matched: {merged_count - total_matched:,} ({100 - match_rate:.1f}%)")
        
        if 'confidence' in merged_df.columns:
            matched_df = merged_df[merged_df['is_matched']]
            if len(matched_df) > 0:
                avg_confidence = matched_df['confidence'].mean()
                print(f"  Average confidence: {avg_confidence:.1f}%")
    
    print(f"\n🎉 Successfully created merged file: {merged_filename}")
    return merged_path

if __name__ == "__main__":
    merge_all_state_parquets()