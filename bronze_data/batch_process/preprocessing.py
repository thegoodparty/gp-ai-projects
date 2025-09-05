#!/usr/bin/env python3

import pandas as pd
import numpy as np
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional
from dataclasses import dataclass
from pathlib import Path
import sys
import time
import re

# Add parent directory to path
sys.path.append(str(Path(__file__).parent.parent.parent.parent))

from shared.llm_gemini import GeminiClient, GeminiModelType
from shared.logger import get_logger


class CandidateSeriousnessAssessor:
    """
    LLM-based system to assess whether a candidate record represents a serious political campaign
    or is test data, joke entry, or data quality issue.
    """
    
    def __init__(self, max_workers: int = 1500, batch_size: int = 1000):
        self.logger = get_logger(__name__)
        self.max_workers = max_workers
        self.batch_size = batch_size
        
        # Create high-throughput LLM clients with DDHQ proven settings
        target_concurrency = 1200  # DDHQ proven - 10k/min target
        self.llm_clients = [
            GeminiClient(
                default_model=GeminiModelType.FLASH,
                default_temperature=0.0,
                thinking_budget=0,  # Disable thinking for cost efficiency
                max_connections=target_concurrency,
                max_keepalive_connections=target_concurrency // 4  # 300 keepalive
            ) for _ in range(10)  # 10 high-throughput client instances
        ]
        self.thread_pool = ThreadPoolExecutor(max_workers=max_workers)
        
        # Tracking
        self.processed_count = 0
        self.total_cost = 0.0
        self.start_time = None
    
    
    def assess_candidate_sync(self, candidate_record: Dict, client_id: int) -> Dict:
        """Synchronous assessment function for thread pool using pure LLM assessment"""
        
        client = self.llm_clients[client_id % len(self.llm_clients)]
        
        # Extract fields
        candidate_name = str(candidate_record.get('candidate_name', '')).strip()
        office = str(candidate_record.get('office', '')).strip()
        city = str(candidate_record.get('city', '')).strip()
        state = str(candidate_record.get('state', '')).strip()
        
        # Build prompt focused on political legitimacy and matchability
        zip_code = candidate_record.get('zip_code', '')
        district = candidate_record.get('district', '')
        county = candidate_record.get('county', '')
        
        # Check what location data we have
        has_city = city and city.strip()
        has_district = district and district.strip()
        has_state = state and state.strip()
        has_county = county and county.strip()
        has_zip = zip_code and zip_code.strip()
        
        has_any_location = has_city or has_district or has_state or has_county
        
        # Only include ZIP if we're missing location data
        include_zip = not has_any_location and has_zip
        
        # Determine what needs to be derived
        needs_city = not has_city and has_zip
        needs_county = not has_county and has_zip  
        needs_state = not has_state and has_zip
        
        candidate_info = f"""CANDIDATE:
- Name: "{candidate_name}"
- Office: "{office}"  
- City: "{city}"
- State: "{state}"
- County: "{county}"
- District: "{district}\""""
        
        if include_zip:
            candidate_info += f"""
- ZIP Code: "{zip_code}" """
        
        prompt = f"""Assess if this is a MATCHABLE POLITICAL CANDIDATE using two criteria:

{candidate_info}

CRITERIA 1 - POLITICAL LEGITIMACY:
✓ ACCEPT if: Real candidate name + Real political office (Mayor, City Council, Judge, School Board, etc.)
✓ BE FLEXIBLE: "Town Mayor" vs "City Mayor" vs "Village Mayor" are all legitimate mayoral offices
✗ REJECT if: Business names, "Work from home", "Charity", spam, gibberish names/offices

CRITERIA 2 - GEOGRAPHIC MATCHABILITY:
✓ ACCEPT if: Has sufficient geographic identifier for political races:
  - Any valid location combination (city/county/district + state)
  - ZIP code with derivable geographic context
  - Be flexible - "Orange County" as city is valid geographic identifier for county-level races
✗ REJECT if: Missing ALL geographic identifiers OR obviously fake locations OR invalid ZIP codes

GEOGRAPHIC INTELLIGENCE: When ZIP codes are provided, use your knowledge to derive missing location context ONLY when geographic information is insufficient.
- Don't use ZIP to resolve conflicts - focus on whether there's enough geographic information to identify where political races would occur
- Be flexible about minor geographic inconsistencies - if there's a real person + real office + reasonable location, it's likely MATCHABLE

ASSESSMENT: "MATCHABLE" if both criteria met, "UNMATCHABLE" if either fails.

LOCATION DERIVATION: Only provide derived location fields to fill gaps in geographic coverage:
- Provide derived_city ONLY when no city information exists and ZIP allows city lookup
- Provide derived_county ONLY when no county information exists and ZIP allows county lookup  
- Provide derived_state ONLY when no state information exists and ZIP allows state lookup
- Don't duplicate existing geographic information, just fill missing gaps

Examples:
- "John Smith - Mayor (Springfield, IL)" → MATCHABLE (city+state available)
- "David Denune - Martinsburg Town Mayor (Martinsburg, OH)" → MATCHABLE (legitimate mayoral office, don't worry about town vs city vs village classification)
- "Jason Bhardwaj - School Committee (Newton, MI)" → MATCHABLE (real person + real office + sufficient location, minor state inconsistency acceptable)
- "Benjamin Tracy - Upper Arlington City Council (Arlington, MA)" → MATCHABLE (real person + real office + sufficient location context)
- "Jane Doe - Town Meeting (, MA, ZIP: 02467)" → MATCHABLE (ZIP 02467 = Chestnut Hill, MA - ZIP used due to missing city)
- "Bob Wilson - School Board (, TX, ZIP: 78701)" → MATCHABLE (ZIP 78701 = Austin, TX - ZIP used due to missing city)
- "Janet Dee - Orange County Supervisor (Orange County, CA)" → MATCHABLE (county in city field is valid geographic identifier)
- "Work Inc - City Council (, TX)" → UNMATCHABLE (business name, fails political legitimacy)  
- "Mary Johnson - Work from home (Austin, TX)" → UNMATCHABLE (not political office, fails political legitimacy)
- "Janet Dee - jent (Orange County, CA)" → UNMATCHABLE (invalid office, fails political legitimacy)"""

        response_schema = {
            "type": "object", 
            "properties": {
                "assessment": {"type": "string", "enum": ["MATCHABLE", "UNMATCHABLE"]},
                "confidence": {"type": "integer", "minimum": 50, "maximum": 100},
                "reasoning": {"type": "string", "maxLength": 200},
                "derived_city": {"type": "string", "description": "City derived from ZIP code if available"},
                "derived_county": {"type": "string", "description": "County derived from ZIP code if available"},
                "derived_state": {"type": "string", "description": "State derived from ZIP code if available"}
            },
            "required": ["assessment", "confidence", "reasoning"]
        }
        
        # LLM call with retry and exponential backoff
        max_retries = 5
        for attempt in range(max_retries):
            try:
                response = client.generate_structured_content(
                    prompt=prompt,
                    response_schema=response_schema,
                    model=GeminiModelType.FLASH,  # Fastest model
                    thinking_budget=0,  # Disable thinking for maximum cost efficiency
                    temperature=0.0
                )
                
                # Parse response
                assessment = response['assessment']
                confidence = float(response['confidence'])
                reasoning = response['reasoning']
                
                if assessment == "MATCHABLE":
                    category = "Matchable"
                    is_serious = True
                else:  # UNMATCHABLE
                    category = "Unmatchable"  
                    is_serious = False
                
                return {
                    **candidate_record,
                    'preprocessing_is_serious_candidate': is_serious,
                    'preprocessing_confidence': confidence,
                    'preprocessing_category': category,
                    'preprocessing_reasoning': reasoning,
                    'preprocessing_derived_city': response.get('derived_city') if needs_city else None,
                    'preprocessing_derived_county': response.get('derived_county') if needs_county else None, 
                    'preprocessing_derived_state': response.get('derived_state') if needs_state else None
                }
                
            except Exception as e:
                if attempt < max_retries - 1:
                    # Exponential backoff with jitter
                    import random
                    delay = (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(delay)
                    continue
                else:
                    # Final fallback - conservative assessment
                    return {
                        **candidate_record,
                        'preprocessing_is_serious_candidate': True,  # Conservative default
                        'preprocessing_confidence': 30.0,
                        'preprocessing_category': 'Questionable',
                        'preprocessing_reasoning': f'Assessment failed: {str(e)[:100]}',
                        'preprocessing_derived_city': None,
                        'preprocessing_derived_county': None,
                        'preprocessing_derived_state': None
                    }

    
    async def process_batch_optimized(self, batch_df: pd.DataFrame, batch_num: int, total_batches: int) -> pd.DataFrame:
        """Process batch with maximum parallelization"""
        
        batch_size = len(batch_df)
        start_time = time.time()
        
        self.logger.info(f"🔥 Batch {batch_num}/{total_batches} - {batch_size} candidates with {self.max_workers} workers")
        
        # Create assessment tasks with client rotation
        tasks = []
        for idx, (_, row) in enumerate(batch_df.iterrows()):
            candidate_record = row.to_dict()
            client_id = idx  # Rotate through clients
            
            task = asyncio.get_event_loop().run_in_executor(
                self.thread_pool,
                self.assess_candidate_sync,
                candidate_record,
                client_id
            )
            tasks.append(task)
        
        # Execute all assessments in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # Error fallback
                original_row = batch_df.iloc[i].to_dict()
                error_result = {
                    **original_row,
                    'preprocessing_is_serious_candidate': True,  # Conservative
                    'preprocessing_confidence': 25.0,
                    'preprocessing_category': 'Questionable',
                    'preprocessing_reasoning': f'Processing error: {str(result)[:50]}',
                    'preprocessing_derived_city': None,
                    'preprocessing_derived_county': None,
                    'preprocessing_derived_state': None
                }
                final_results.append(error_result)
                self.logger.warning(f"Assessment error: {result}")
            else:
                final_results.append(result)
        
        duration = time.time() - start_time
        records_per_second = batch_size / duration
        self.processed_count += batch_size
        
        # Update cost tracking
        total_cost = sum(client.total_cost for client in self.llm_clients)
        self.total_cost = total_cost
        
        elapsed_total = time.time() - self.start_time if self.start_time else duration
        overall_rate = self.processed_count / elapsed_total
        
        self.logger.info(f"✅ Batch {batch_num}/{total_batches} completed in {duration:.1f}s")
        self.logger.info(f"   📊 {records_per_second:.0f} rec/sec batch, {overall_rate:.0f} rec/sec overall")
        self.logger.info(f"   💰 Running cost: ${self.total_cost:.2f}")
        
        return pd.DataFrame(final_results)
    
    async def assess_dataset(self, df: pd.DataFrame, batch_size: int = None, output_filename: str = None) -> pd.DataFrame:
        """Assess seriousness for entire dataset with ultra-high throughput"""
        
        # Use instance batch_size if not provided
        if batch_size is None:
            batch_size = self.batch_size
        
        self.start_time = time.time()
        
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"🚀 OPTIMIZED SERIOUSNESS ASSESSMENT")
        self.logger.info(f"{'='*80}")
        self.logger.info(f"Dataset: {len(df):,} candidates")
        self.logger.info(f"Workers: {self.max_workers}")
        self.logger.info(f"Batch size: {batch_size}")
        self.logger.info(f"LLM clients: {len(self.llm_clients)}")
        
        # Process in batches
        all_result_dfs = []
        total_batches = (len(df) + batch_size - 1) // batch_size
        
        for batch_start in range(0, len(df), batch_size):
            batch_end = min(batch_start + batch_size, len(df))
            batch_df = df.iloc[batch_start:batch_end]
            batch_num = (batch_start // batch_size) + 1
            
            batch_result_df = await self.process_batch_optimized(batch_df, batch_num, total_batches)
            all_result_dfs.append(batch_result_df)
        
        # Combine all results
        final_df = pd.concat(all_result_dfs, ignore_index=True)
        
        total_duration = time.time() - self.start_time
        
        # Final summary
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"✅ ASSESSMENT COMPLETED")
        self.logger.info(f"{'='*80}")
        
        total = len(final_df)
        matchable = (final_df['preprocessing_category'] == 'Matchable').sum()
        unmatchable = (final_df['preprocessing_category'] == 'Unmatchable').sum()
        questionable = (final_df['preprocessing_category'] == 'Questionable').sum()  # Error fallbacks only
        
        junk_total = unmatchable
        overall_rate = total / (total_duration / 60)  # per minute
        
        self.logger.info(f"📊 RESULTS:")
        self.logger.info(f"  Total: {total:,} candidates in {total_duration/60:.1f} minutes ({overall_rate:.0f}/min)")
        self.logger.info(f"  Matchable: {matchable:,} ({matchable/total*100:.1f}%)")
        self.logger.info(f"  Unmatchable: {unmatchable:,} ({unmatchable/total*100:.1f}%)")
        self.logger.info(f"  Questionable (errors): {questionable:,} ({questionable/total*100:.1f}%)")
        self.logger.info(f"💰 Total cost: ${self.total_cost:.2f} (${self.total_cost/total:.4f}/candidate)")
        
        if junk_total > 0:
            self.logger.info(f"💡 Est. matching cost savings: ${junk_total * 0.02:.2f}")
        
        # Save results - create separate files for accepted and rejected candidates
        timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        if output_filename:
            base_filename = f"{output_filename}_{timestamp}"
        else:
            base_filename = f"preprocessing_results_{timestamp}"
        
        # Save complete results
        complete_file = output_dir / f"{base_filename}.parquet"
        final_df.to_parquet(complete_file, index=False)
        self.logger.info(f"📁 Complete results saved: {complete_file}")
        
        # Split into accepted and rejected based on LLM assessment
        # Accept: Matchable (legitimate political candidates)
        # Reject: Unmatchable (fails political legitimacy or location requirements)
        accepted_df = final_df[final_df['preprocessing_category'] == 'Matchable']
        rejected_df = final_df[final_df['preprocessing_category'] == 'Unmatchable']
        
        # Save accepted candidates (clean data for matching)
        if len(accepted_df) > 0:
            accepted_file = output_dir / f"{base_filename}_accepted.parquet"
            accepted_df.to_parquet(accepted_file, index=False)
            self.logger.info(f"✅ Accepted candidates saved: {accepted_file} ({len(accepted_df):,} records)")
        
        # Save rejected candidates (for analysis and debugging)
        if len(rejected_df) > 0:
            rejected_file = output_dir / f"{base_filename}_rejected.parquet"
            rejected_df.to_parquet(rejected_file, index=False)
            self.logger.info(f"❌ Rejected candidates saved: {rejected_file} ({len(rejected_df):,} records)")
        
        # Summary of saved files
        self.logger.info(f"\n📊 SAVED FILES SUMMARY:")
        self.logger.info(f"  Complete dataset: {complete_file.name}")
        if len(accepted_df) > 0:
            self.logger.info(f"  Accepted (clean): {accepted_file.name} - Ready for L2 matching")
        if len(rejected_df) > 0:
            self.logger.info(f"  Rejected (junk): {rejected_file.name} - For analysis/debugging")
        
        # Cleanup
        self.thread_pool.shutdown(wait=True)
        
        return final_df
    

async def main():
    """Run the candidate seriousness assessor on full dataset"""
    
    # Load the motivated users dataset
    current_dir = Path(__file__).parent
    offline_data_dir = current_dir / "offline_data"
    
    # Find the most recent motivated users file
    data_files = list(offline_data_dir.glob("motivated_unmatched_users_*.parquet"))
    
    if not data_files:
        print(f"No motivated users data files found in {offline_data_dir}")
        print("Please run create_motivated_users_parquet.py first")
        return
    
    # Use the most recent file
    data_file = sorted(data_files)[-1]
    print(f"Loading dataset: {data_file.name}")
    
    df = pd.read_parquet(data_file)
    print(f"Loaded {len(df):,} candidates")
    
    # Run full assessment
    assessor = CandidateSeriousnessAssessor(
        max_workers=1500,  # Same as DDHQ production
        batch_size=1000   # Proven batch size
    )
    
    assessed_df = await assessor.assess_dataset(
        df, 
        output_filename="preprocessing_full_dataset"
    )
    
    print(f"Assessment completed: {len(assessed_df):,} candidates processed")

if __name__ == "__main__":
    asyncio.run(main())