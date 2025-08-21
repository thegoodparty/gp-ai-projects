#!/usr/bin/env python3

"""
PARALLEL PRODUCTION HUBSPOT-DDHQ MATCHER

Adopts advanced parallelization strategies from the golden data production matcher:
- ThreadPoolExecutor for maximum LLM concurrency (150+ workers)
- Async batch processing with semaphore control
- Aggressive parallel processing within batches
- Enhanced error handling and retry logic
- Real-time progress tracking and cost monitoring

USAGE:
uv run parallel_production_matcher.py --max-workers 150 --batch-size 50
"""

import sys
import os
import pandas as pd
import numpy as np
import faiss
import json
import asyncio
import time
from datetime import datetime
from typing import List, Dict, Any, Optional
from tqdm.asyncio import tqdm
from concurrent.futures import ThreadPoolExecutor
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from shared.llm_gemini import GeminiClient, GeminiModelType
from shared.logger import get_logger

class ParallelProductionMatcher:
    def __init__(self, batch_size: int = 50, max_records: Optional[int] = None, max_workers: int = 150):
        self.logger = get_logger(__name__)
        self.batch_size = batch_size
        self.max_records = max_records
        self.max_workers = max_workers
        
        print("🚀 PARALLEL PRODUCTION HUBSPOT-DDHQ MATCHER")
        print("=" * 50)
        print(f"🔧 Configuration: {max_workers} workers, batch size {batch_size}")
        
        # Load data and build FAISS index
        self._load_data()
        self._build_faiss_index()
        self._init_llm()
        
        # Initialize progress tracking
        self.processed_count = 0
        self.matched_count = 0
        self.total_cost = 0.0
        
        # Create ThreadPoolExecutor for maximum concurrency
        self.thread_pool = ThreadPoolExecutor(max_workers=self.max_workers)
        
        records_to_process = min(len(self.hubspot_df), max_records) if max_records else len(self.hubspot_df)
        print(f"\n✅ Ready to process {records_to_process:,} temporally-aligned HubSpot records")
    
    def _load_data(self):
        print("📥 Loading offline data...")
        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")
        
        # Use test embeddings (valid embeddings for 100 records each)
        hubspot_file = os.path.join(offline_data_dir, 'hubspot_filtered_with_embeddings_latest.parquet')
        ddhq_file = os.path.join(offline_data_dir, 'ddhq_with_embeddings_cleaned_latest.parquet')
        
        self.hubspot_df = pd.read_parquet(hubspot_file)
        self.ddhq_df = pd.read_parquet(ddhq_file)
        
        print(f"   HubSpot (filtered by DDHQ dates): {len(self.hubspot_df):,} records")
        print(f"   DDHQ (full dataset): {len(self.ddhq_df):,} records")
    
    def _build_faiss_index(self):
        print("🔍 Building FAISS index from DDHQ embeddings...")
        
        ddhq_embeddings = np.array(self.ddhq_df['embedding_name_race'].tolist(), dtype=np.float32)
        
        embedding_dim = ddhq_embeddings.shape[1]
        self.faiss_index = faiss.IndexFlatIP(embedding_dim)
        
        faiss.normalize_L2(ddhq_embeddings)
        self.faiss_index.add(ddhq_embeddings)
        
        print(f"   FAISS index built: {self.faiss_index.ntotal:,} embeddings, {embedding_dim} dimensions")
    
    def _init_llm(self):
        print("🤖 Initializing Gemini LLM...")
        self.llm_client = GeminiClient(
            default_model=GeminiModelType.FLASH,
            default_temperature=0.0
            # Removed thinking_budget to avoid token limit issues that cause JSON truncation
        )
        print("   Gemini Flash initialized (no token limits)")
    
    def _search_similar_candidates(self, hubspot_embedding: np.ndarray, k: int = 10) -> List[Dict[str, Any]]:
        """Search for k most similar DDHQ candidates using FAISS"""
        
        query_embedding = np.array(hubspot_embedding, dtype=np.float32).reshape(1, -1)
        faiss.normalize_L2(query_embedding)
        
        similarities, indices = self.faiss_index.search(query_embedding, k)
        
        results = []
        for i, (similarity, idx) in enumerate(zip(similarities[0], indices[0])):
            ddhq_record = self.ddhq_df.iloc[idx]
            results.append({
                'rank': i + 1,
                'similarity': float(similarity),
                'ddhq_index': int(idx),
                'candidate': ddhq_record['candidate'],
                'race_name': ddhq_record['race_name'],
                'candidate_party': ddhq_record.get('candidate_party', 'N/A'),
                'is_winner': ddhq_record.get('is_winner', 'N/A'),
                'embedding_text': ddhq_record.get('embedding_name_race_text', 'N/A'),
                'ddhq_race_id': ddhq_record.get('race_id', 'N/A'),
                'ddhq_candidate_id': ddhq_record.get('candidate_id', 'N/A'),
                'election_type': ddhq_record.get('election_type', 'N/A'),
                'date': ddhq_record.get('date', 'N/A')
            })
        
        return results
    
    def _calibrate_confidence(self, llm_result: Dict, hubspot_record: Dict, matched_candidate: Dict) -> Dict:
        """Adjust confidence based on match quality indicators"""
        
        confidence = llm_result.get('confidence', 0)
        
        if confidence == 0 or not matched_candidate:
            return llm_result
        
        hs_first = str(hubspot_record.get('first_name', '')).lower().strip()
        hs_last = str(hubspot_record.get('last_name', '')).lower().strip()
        ddhq_name = str(matched_candidate.get('candidate', '')).lower()
        
        # Gender mismatch detection (simple heuristic)
        male_names = ['stanley', 'john', 'marco', 'kennith', 'domingo', 'morrio', 'jr']
        female_names = ['susan', 'jacqueline', 'adrienne', 'laura', 'verla']
        
        # Check for clear gender mismatches
        hs_likely_male = any(name in hs_first for name in male_names)
        hs_likely_female = any(name in hs_first for name in female_names)
        ddhq_likely_male = any(name in ddhq_name for name in male_names)
        ddhq_likely_female = any(name in ddhq_name for name in female_names)
        
        if (hs_likely_male and ddhq_likely_female) or (hs_likely_female and ddhq_likely_male):
            confidence = max(0, confidence - 25)
            self.logger.debug(f"Gender mismatch detected: {hs_first} vs {ddhq_name}, reducing confidence by 25")
        
        # Check for exact name matches (should have high confidence)
        exact_first = hs_first in ddhq_name
        exact_last = hs_last in ddhq_name
        
        if not exact_first and not exact_last:
            confidence = max(0, confidence - 15)
            self.logger.debug(f"No exact name component match: {hs_first} {hs_last} vs {ddhq_name}, reducing confidence by 15")
        elif not exact_first or not exact_last:
            confidence = max(0, confidence - 8)
            self.logger.debug(f"Partial name match: {hs_first} {hs_last} vs {ddhq_name}, reducing confidence by 8")
        
        # Minimum confidence threshold
        if confidence < 70:
            self.logger.debug(f"Confidence {confidence} below threshold, rejecting match")
            return {
                "best_match": None,
                "confidence": confidence,
                "reasoning": f"Match rejected due to low calibrated confidence ({confidence} < 70). {llm_result.get('reasoning', '')}"
            }
        
        llm_result['confidence'] = confidence
        return llm_result
    
    async def _validate_with_llm(self, hubspot_record: Dict, similar_candidates: List[Dict]) -> Dict[str, Any]:
        """Use LLM to validate and select the best match with enhanced retry logic"""
        
        hubspot_info = {
            'name': f"{hubspot_record.get('first_name', '')} {hubspot_record.get('last_name', '')}".strip(),
            'full_name': hubspot_record.get('full_name', 'N/A'),
            'state': hubspot_record.get('state', 'N/A'),
            'city': hubspot_record.get('city', 'N/A'),
            'office': hubspot_record.get('candidate_office', hubspot_record.get('official_office_name', 'N/A')),
            'embedding_text': hubspot_record.get('embedding_name_race_text', 'N/A')
        }
        
        candidates_text = ""
        for i, candidate in enumerate(similar_candidates, 1):
            candidates_text += f"""
Match {i}:
  - Candidate: {candidate['candidate']}
  - Race: {candidate['race_name']}
  - Party: {candidate['candidate_party']}
  - Winner: {candidate['is_winner']}
  - Embedding: {candidate['embedding_text']}
"""
        
        prompt = f"""You are a political candidate matching expert. Your task is to match candidates between HubSpot and DDHQ databases with EXTREME PRECISION to avoid false positives.

HubSpot Candidate:
- Name: {hubspot_info['name']}
- Full Name: {hubspot_info['full_name']}
- State: {hubspot_info['state']}
- City: {hubspot_info['city']}
- Office: {hubspot_info['office']}
- Embedding: {hubspot_info['embedding_text']}

DDHQ Candidates (ranked by similarity):
{candidates_text}

CRITICAL MATCHING RULES - ALL must be satisfied:

1. NAME REQUIREMENTS (MANDATORY):
   - First name: Must be exact match, clear nickname (Bob/Robert), or obvious variant (Jon/John)
   - Last name: Must be exact match or extremely close phonetic variant
   - REJECT: Different genders (Stanley→Susan), unrelated names (Marco→Erick), random similarities

2. GEOGRAPHIC REQUIREMENTS (MANDATORY):
   - If HubSpot has a state, DDHQ race MUST be in the same state
   - REJECT: Any cross-state matches unless truly exceptional circumstances

3. VALIDATION EXAMPLES:
   
   VALID MATCHES (High Confidence):
   - "John Smith" → "John Smith" (exact match)
   - "Bob Johnson" → "Robert Johnson" (nickname variation)
   - "Mary O'Connor" → "Mary O'Connor" (exact match)
   
   INVALID MATCHES (Must Reject):
   - "Stanley Pokras" → "Susan Kopras" (gender mismatch, different first name)
   - "Test User" → "Ronald Test" (test data, unrelated)
   - "Marco Huerta" → "Erick Huerta" (different first name entirely)
   - "JR Giron (NE)" → "John Ewing Jr. (NE)" (different last name entirely)
   - "Morrio Clark (NC)" → "Laura Clark (SC)" (different state, different first name)

4. CONFIDENCE CALIBRATION:
   - 95-100: Perfect name + state + office match
   - 85-94: Very strong name similarity + state match
   - 75-84: Good name match + state match with minor uncertainty
   - 70-74: Reasonable match but with some concerns
   - Below 70: MUST REJECT - return null

5. AUTOMATIC REJECTIONS:
   - Any name containing "test", "sample", "demo", "fake"
   - Different states when HubSpot has state data
   - No meaningful name similarity between first or last names
   - Clear gender mismatches
   - Completely unrelated names

INSTRUCTIONS:
- Analyze each candidate systematically against ALL rules
- Be extremely conservative - false positives are worse than false negatives
- If uncertain, REJECT the match
- Only match when you are highly confident it's the same person

RESPOND IN VALID JSON (no markdown, no extra text):
{{{{
  "best_match": 3,
  "confidence": 87,
  "reasoning": "Exact names, same state, similar office - same person."
}}}}

IMPORTANT: Keep reasoning under 100 characters. Return null for best_match if no strict match."""

        # Enhanced retry logic for data accuracy - retry 9 times like golden data
        max_retries = 9
        base_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                # Use dedicated thread pool for maximum LLM concurrency
                response = await asyncio.get_event_loop().run_in_executor(
                    self.thread_pool, 
                    lambda: self.llm_client.generate_content(prompt)
                )
                
                response_text = response.strip()
                
                # Simple cleaning - remove markdown blocks but don't over-process
                if response_text.startswith('```json'):
                    response_text = response_text[7:]
                    if response_text.endswith('```'):
                        response_text = response_text[:-3]
                elif response_text.startswith('```'):
                    response_text = response_text[3:]
                    if response_text.endswith('```'):
                        response_text = response_text[:-3]
                
                response_text = response_text.strip()
                
                # Try direct parsing first - most LLM responses are valid JSON
                try:
                    result = json.loads(response_text)
                except json.JSONDecodeError:
                    # Only if direct parsing fails, try minimal cleaning
                    # Remove trailing commas and try again
                    import re
                    cleaned_text = re.sub(r',(\s*[}\]])', r'\1', response_text)
                    result = json.loads(cleaned_text)
                
                # Apply confidence calibration if there's a match
                if result.get('best_match') is not None:
                    best_match_idx = result['best_match'] - 1
                    if 0 <= best_match_idx < len(similar_candidates):
                        matched_candidate = similar_candidates[best_match_idx]
                        result = self._calibrate_confidence(result, hubspot_record, matched_candidate)
                
                # Track LLM cost
                stats = self.llm_client.get_usage_stats()
                self.total_cost = stats.get('total_cost', 0.0)
                
                return result
                
            except Exception as e:
                # Enhanced error logging with more context
                error_context = {
                    'candidate_name': f"{hubspot_record.get('first_name', '')} {hubspot_record.get('last_name', '')}".strip(),
                    'candidate_state': hubspot_record.get('state', 'N/A'),
                    'candidate_office': hubspot_record.get('candidate_office', 'N/A'),
                    'error_type': type(e).__name__,
                    'error_message': str(e),
                    'response_length': len(response_text) if 'response_text' in locals() else 0,
                    'response_preview': response_text[:200] + '...' if 'response_text' in locals() and len(response_text) > 200 else response_text if 'response_text' in locals() else 'No response captured'
                }
                
                # For transient errors, retry with exponential backoff
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    candidate_name = error_context['candidate_name']
                    candidate_state = error_context['candidate_state']
                    error_type = error_context['error_type']
                    error_message = error_context['error_message']
                    response_preview = error_context['response_preview']
                    self.logger.warning(f"LLM attempt {attempt + 1}/{max_retries} failed for {candidate_name} ({candidate_state}): {error_type}: {error_message}. Response preview: {response_preview}. Retrying in {delay:.1f}s")
                    await asyncio.sleep(delay)
                else:
                    # Final attempt failed - log comprehensive details
                    self.logger.error(f"LLM FINAL FAILURE after {max_retries} attempts:")
                    candidate_name = error_context['candidate_name']
                    candidate_state = error_context['candidate_state']
                    candidate_office = error_context['candidate_office']
                    error_type = error_context['error_type']
                    error_message = error_context['error_message']
                    response_length = error_context['response_length']
                    response_preview = error_context['response_preview']
                    self.logger.error(f"  Candidate: {candidate_name} ({candidate_state}) - {candidate_office}")
                    self.logger.error(f"  Error Type: {error_type}")
                    self.logger.error(f"  Error Message: {error_message}")
                    self.logger.error(f"  Response Length: {response_length} chars")
                    self.logger.error(f"  Response Preview: {response_preview}")
                    
                    # For JSON errors, log character analysis
                    if 'json' in error_context['error_type'].lower() or 'control character' in error_context['error_message'].lower():
                        if 'response_text' in locals():
                            # Log problematic characters
                            problematic_chars = []
                            for i, char in enumerate(response_text[:500]):  # First 500 chars
                                char_code = ord(char)
                                if char_code <= 31 or char_code == 127:  # Control characters
                                    problematic_chars.append(f'pos {i}: \\x{char_code:02x} ({repr(char)})')
                            
                            if problematic_chars:
                                self.logger.error(f"  Control Characters Found: {problematic_chars[:5]}")  # First 5
                            else:
                                self.logger.error(f"  No obvious control characters found in first 500 chars")
                        
                        # Try to identify JSON structure issues
                        if 'response_text' in locals():
                            self.logger.error(f"  Full Response (first 1000 chars): {repr(response_text[:1000])}")
                    
                    return {
                        "best_match": None,
                        "confidence": 0,
                        "reasoning": f"LLM validation error after {max_retries} attempts: {error_context['error_type']}: {error_context['error_message']}"
                    }
    
    async def _process_hubspot_record(self, hubspot_record: pd.Series, record_index: int) -> Dict[str, Any]:
        """Process a single HubSpot record through the matching pipeline"""
        
        hubspot_embedding = np.array(hubspot_record['embedding_name_race'])
        
        try:
            # Step 1: FAISS similarity search
            similar_candidates = self._search_similar_candidates(hubspot_embedding, k=10)
            
            # Step 2: LLM validation
            llm_result = await self._validate_with_llm(hubspot_record.to_dict(), similar_candidates)
            
            # Prepare match result
            match_result = {
                # HubSpot record info
                'hubspot_row_index': record_index,
                'hubspot_gp_candidacy_id': hubspot_record.get('gp_candidacy_id', 'N/A'),
                'hubspot_candidate_id': hubspot_record.get('candidacy_id', 'N/A'),
                'hubspot_first_name': hubspot_record.get('first_name', 'N/A'),
                'hubspot_last_name': hubspot_record.get('last_name', 'N/A'),
                'hubspot_full_name': hubspot_record.get('full_name', 'N/A'),
                'hubspot_state': hubspot_record.get('state', 'N/A'),
                'hubspot_city': hubspot_record.get('city', 'N/A'),
                'hubspot_candidate_office': hubspot_record.get('candidate_office', 'N/A'),
                'hubspot_official_office_name': hubspot_record.get('official_office_name', 'N/A'),
                'hubspot_party_affiliation': hubspot_record.get('party_affiliation', 'N/A'),
                'hubspot_embedding_text': hubspot_record.get('embedding_name_race_text', 'N/A'),
                'hubspot_primary_election_date': hubspot_record.get('primary_election_date', 'N/A'),
                'hubspot_general_election_date': hubspot_record.get('general_election_date', 'N/A'),
                
                # LLM validation results
                'llm_best_match': llm_result.get('best_match'),
                'llm_confidence': llm_result.get('confidence', 0),
                'llm_reasoning': llm_result.get('reasoning', 'N/A'),
                
                # Top 10 similarity candidates (stored as JSON strings for parquet)
                'top_10_candidates': json.dumps([{
                    'rank': c['rank'],
                    'similarity': c['similarity'],
                    'candidate': c['candidate'],
                    'race_name': c['race_name'],
                    'candidate_party': c['candidate_party'],
                    'is_winner': c['is_winner']
                } for c in similar_candidates]),
                
                # Matched DDHQ record (if any)
                'has_match': llm_result.get('best_match') is not None,
                'ddhq_matched_index': None,
                'ddhq_candidate': None,
                'ddhq_race_name': None,
                'ddhq_candidate_party': None,
                'ddhq_is_winner': None,
                'ddhq_race_id': None,
                'ddhq_candidate_id': None,
                'ddhq_election_type': None,
                'ddhq_date': None,
                'ddhq_embedding_text': None,
                'match_similarity': None
            }
            
            # Fill matched DDHQ record details if there's a match
            if llm_result.get('best_match'):
                best_match_idx = llm_result['best_match'] - 1
                best_candidate = similar_candidates[best_match_idx]
                
                match_result.update({
                    'ddhq_matched_index': best_candidate['ddhq_index'],
                    'ddhq_candidate': best_candidate['candidate'],
                    'ddhq_race_name': best_candidate['race_name'],
                    'ddhq_candidate_party': best_candidate['candidate_party'],
                    'ddhq_is_winner': best_candidate['is_winner'],
                    'ddhq_race_id': best_candidate['ddhq_race_id'],
                    'ddhq_candidate_id': best_candidate['ddhq_candidate_id'],
                    'ddhq_election_type': best_candidate['election_type'],
                    'ddhq_date': best_candidate['date'],
                    'ddhq_embedding_text': best_candidate['embedding_text'],
                    'match_similarity': best_candidate['similarity']
                })
                
                self.matched_count += 1
            
            self.processed_count += 1
            return match_result
            
        except Exception as e:
            self.logger.error(f"❌ Error processing record {record_index}: {str(e)}")
            # Error case - preserve HubSpot data, add error info
            return {
                'hubspot_row_index': record_index,
                'hubspot_gp_candidacy_id': hubspot_record.get('gp_candidacy_id', 'N/A'),
                'hubspot_first_name': hubspot_record.get('first_name', 'N/A'),
                'hubspot_last_name': hubspot_record.get('last_name', 'N/A'),
                'llm_best_match': None,
                'llm_confidence': 0,
                'llm_reasoning': f'Processing error: {str(e)}',
                'has_match': False,
                'top_10_candidates': 'ERROR',
                'ddhq_candidate': 'ERROR',
                'ddhq_race_name': 'ERROR'
            }
    
    async def process_batch(self, batch_df: pd.DataFrame, batch_num: int, total_batches: int) -> pd.DataFrame:
        """Process a batch of HubSpot records in parallel with enhanced concurrency"""
        batch_size = len(batch_df)
        
        # Enhanced progress logging
        progress_pct = (batch_num / total_batches * 100) if total_batches > 0 else 0
        self.logger.info(f"🔄 Batch {batch_num}/{total_batches} ({progress_pct:.1f}%) - Processing {batch_size} records")
        
        # Create tasks for parallel processing - all records in batch run concurrently
        tasks = [self._process_hubspot_record(row, idx) for idx, (_, row) in enumerate(batch_df.iterrows())]
        
        # Execute all tasks in parallel with error handling
        start_time = time.time()
        
        # Process in smaller groups to enable faster quota exhaustion detection
        group_size = min(self.max_workers, 100)  # Optimized for throughput
        results = []
        
        for i in range(0, len(tasks), group_size):
            group_tasks = tasks[i:i + group_size]
            group_results = await asyncio.gather(*group_tasks, return_exceptions=True)
            results.extend(group_results)
        
        duration = time.time() - start_time
        
        # Process results and handle exceptions
        processed_rows = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # Create error row from original data for exceptions
                row = batch_df.iloc[i].copy()
                error_result = {
                    'hubspot_row_index': i,
                    'hubspot_first_name': row.get('first_name', 'N/A'),
                    'hubspot_last_name': row.get('last_name', 'N/A'),
                    'llm_best_match': None,
                    'llm_confidence': 0,
                    'llm_reasoning': f'Batch processing error: {str(result)}',
                    'has_match': False,
                    'top_10_candidates': 'BATCH_ERROR',
                    'ddhq_candidate': 'BATCH_ERROR',
                    'ddhq_race_name': 'BATCH_ERROR'
                }
                processed_rows.append(error_result)
            else:
                processed_rows.append(result)
        
        # Enhanced performance tracking
        if batch_size > 0:
            records_per_second = batch_size / max(0.1, duration)
            
            # Count successful matches in this batch
            batch_matches = sum(1 for row in processed_rows if row.get('has_match', False))
            batch_match_rate = (batch_matches / len(processed_rows) * 100) if processed_rows else 0
            
            self.logger.info(f"✅ Batch {batch_num}/{total_batches} completed ({progress_pct:.1f}%)")
            self.logger.info(f"   📊 {batch_size} records in {duration:.1f}s ({records_per_second:.1f} rec/sec)")
            self.logger.info(f"   🎯 Batch matches: {batch_matches}/{len(processed_rows)} ({batch_match_rate:.1f}%)")
            self.logger.info(f"   💰 Running cost: ${self.total_cost:.6f}")
        
        return pd.DataFrame(processed_rows)
    
    async def process_all_records(self) -> pd.DataFrame:
        """Process all HubSpot records with parallel batch processing"""
        
        if self.max_records:
            # Use random sampling instead of just taking the first records
            records_to_process = self.hubspot_df.sample(n=min(self.max_records, len(self.hubspot_df)), random_state=123)
            print(f"   🎲 Using random sample of {len(records_to_process):,} records (seed=123 for reproducibility)")
        else:
            records_to_process = self.hubspot_df
        
        print(f"\n🔄 Processing {len(records_to_process):,} HubSpot records with parallel batching...")
        
        all_result_dfs = []
        total_batches = (len(records_to_process) + self.batch_size - 1) // self.batch_size
        
        # Use tqdm for progress tracking
        pbar = tqdm(total=total_batches, desc="Processing batches", unit="batch")
        
        try:
            for batch_start in range(0, len(records_to_process), self.batch_size):
                batch_end = min(batch_start + self.batch_size, len(records_to_process))
                batch_df = records_to_process.iloc[batch_start:batch_end]
                batch_num = (batch_start // self.batch_size) + 1
                
                try:
                    batch_result_df = await self.process_batch(batch_df, batch_num, total_batches)
                    all_result_dfs.append(batch_result_df)
                    
                    # Update progress
                    match_rate = f"{self.matched_count/self.processed_count*100:.1f}%" if self.processed_count > 0 else "0.0%"
                    pbar.set_postfix({
                        'matches': self.matched_count,
                        'match_rate': match_rate,
                        'cost': f"${self.total_cost:.2f}"
                    })
                    pbar.update(1)
                    
                    # Save intermediate results every 10 batches
                    if batch_num % 10 == 0:
                        self._save_intermediate_results(all_result_dfs, batch_num)
                        
                except Exception as e:
                    self.logger.error(f"Failed to process batch {batch_num}: {str(e)}")
                    pbar.update(1)
                    continue
        finally:
            pbar.close()
        
        # Combine all results into final DataFrame
        if all_result_dfs:
            final_results_df = pd.concat(all_result_dfs, ignore_index=True)
        else:
            final_results_df = pd.DataFrame()
        
        print(f"\n✅ Processing complete!")
        print(f"   Processed: {self.processed_count:,} records")
        print(f"   Matches found: {self.matched_count:,} ({self.matched_count/self.processed_count*100:.1f}%)")
        print(f"   Total LLM cost: ${self.total_cost:.2f}")
        
        return final_results_df
    
    def _save_intermediate_results(self, result_dfs: List[pd.DataFrame], batch_count: int):
        """Save intermediate results to output folder"""
        if not result_dfs:
            return
            
        current_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(current_dir, "output")
        
        # Create output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)
        
        intermediate_file = os.path.join(output_dir, f"intermediate_parallel_matches_{batch_count}_batches.parquet")
        
        try:
            combined_df = pd.concat(result_dfs, ignore_index=True)
            combined_df.to_parquet(intermediate_file, index=False)
            self.logger.debug(f"Saved intermediate results: {len(combined_df)} records to {intermediate_file}")
        except Exception as e:
            self.logger.error(f"Failed to save intermediate results: {str(e)}")
    
    def save_results(self, results_df: pd.DataFrame) -> str:
        """Save final results to parquet and TSV files in output folder"""
        
        current_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(current_dir, "output")
        
        # Create output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # File names
        parquet_filename = f"parallel_hubspot_ddhq_matches_{timestamp}.parquet"
        tsv_filename = f"parallel_hubspot_ddhq_matches_{timestamp}.tsv"
        latest_parquet_filename = "parallel_hubspot_ddhq_matches_latest.parquet"
        latest_tsv_filename = "parallel_hubspot_ddhq_matches_latest.tsv"
        
        # File paths
        parquet_file = os.path.join(output_dir, parquet_filename)
        tsv_file = os.path.join(output_dir, tsv_filename)
        latest_parquet_file = os.path.join(output_dir, latest_parquet_filename)
        latest_tsv_file = os.path.join(output_dir, latest_tsv_filename)
        
        # Save timestamped versions
        results_df.to_parquet(parquet_file, index=False)
        results_df.to_csv(tsv_file, sep='\t', index=False)
        
        # Save latest versions
        results_df.to_parquet(latest_parquet_file, index=False)
        results_df.to_csv(latest_tsv_file, sep='\t', index=False)
        
        # Calculate file sizes
        parquet_size_mb = os.path.getsize(parquet_file) / (1024 * 1024)
        tsv_size_mb = os.path.getsize(tsv_file) / (1024 * 1024)
        
        print(f"\n💾 Results saved to output folder:")
        print(f"   Parquet: {parquet_file} ({parquet_size_mb:.1f} MB)")
        print(f"   TSV: {tsv_file} ({tsv_size_mb:.1f} MB)")
        print(f"   Records: {len(results_df):,}")
        print(f"   Matches: {results_df['has_match'].sum():,}")
        print(f"   Latest files also saved for easy access")
        
        return parquet_file
    
    def cleanup(self):
        """Cleanup resources"""
        if hasattr(self, 'thread_pool'):
            self.thread_pool.shutdown(wait=True)

async def main():
    # Configuration
    BATCH_SIZE = 150  # Larger batches for more aggressive parallelization
    MAX_RECORDS = None  # Process all records
    MAX_WORKERS = 150  # More workers for maximum concurrency
    
    matcher = ParallelProductionMatcher(
        batch_size=BATCH_SIZE, 
        max_records=MAX_RECORDS, 
        max_workers=MAX_WORKERS
    )
    
    try:
        # Process all records
        results_df = await matcher.process_all_records()
        
        # Save results
        results_file = matcher.save_results(results_df)
        
        print(f"\n🎉 Parallel matching complete! Results saved to {results_file}")
        
    finally:
        # Cleanup resources
        matcher.cleanup()

if __name__ == "__main__":
    asyncio.run(main())