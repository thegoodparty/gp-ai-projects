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
    def __init__(self, batch_size: int = 1000, max_records: Optional[int] = None, max_workers: int = 1500):
        self.logger = get_logger(__name__)
        self.batch_size = batch_size
        self.max_records = max_records
        self.max_workers = max_workers
        
        print("🚀 PARALLEL PRODUCTION HUBSPOT-DDHQ MATCHER")
        print("=" * 50)
        print(f"🔧 Configuration: {max_workers} workers, batch size {batch_size}")
        
        # Load data and initialize lazy-loading
        self._load_data()
        self._init_lazy_loading()
        self._init_llm()
        
        # Initialize progress tracking
        self.processed_count = 0
        self.matched_count = 0
        self.total_cost = 0.0
        
        # Create ThreadPoolExecutor for maximum concurrency
        self.thread_pool = ThreadPoolExecutor(max_workers=self.max_workers)
        
        records_to_process = min(len(self.hubspot_df), max_records) if max_records else len(self.hubspot_df)
        print(f"\n✅ Ready to process {records_to_process:,} temporally-aligned HubSpot records")
    
    def _get_candidate_name(self, hubspot_record: Dict) -> str:
        """Helper method to construct candidate name from HubSpot record"""
        return f"{hubspot_record.get('first_name', '')} {hubspot_record.get('last_name', '')}".strip()
    
    def _load_data(self):
        print("📥 Loading offline data...")
        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")
        
        # Use full embeddings dataset
        hubspot_file = os.path.join(offline_data_dir, 'hubspot_filtered_with_embeddings_latest.parquet')
        ddhq_file = os.path.join(offline_data_dir, 'ddhq_with_embeddings_cleaned_latest.parquet')
        
        self.hubspot_df = pd.read_parquet(hubspot_file)
        self.ddhq_df = pd.read_parquet(ddhq_file)
        
        # Sort HubSpot data by date + state + election_type for optimal cache performance
        print("🔄 Sorting HubSpot data by (date, state, election_type) for lazy-load cache optimization...")
        self.hubspot_df = self.hubspot_df.sort_values(
            ['election_date', 'state', 'election_type'],
            na_position='last'
        )
        
        print(f"   HubSpot (filtered by DDHQ dates): {len(self.hubspot_df):,} records")
        print(f"   DDHQ (full dataset): {len(self.ddhq_df):,} records")
    
    def _init_lazy_loading(self):
        print("🔍 Initializing lazy-loading with garbage collection (Date + State + Election Type)...")

        # MEMORY OPTIMIZATION: Lazy-load partitions with GC (not pre-build all)
        self.faiss_indices = {}  # "{date}_{state}_{election_type}" -> FAISS index (cache)
        self.partition_records = {}   # "{date}_{state}_{election_type}" -> DataFrame slice (cache)
        self.last_partition_key = None  # Track partition changes for GC
        self.partition_build_count = 0  # Statistics
        self.partition_gc_count = 0  # Statistics

        print("   ✅ Lazy-loading initialized - partitions will be built on-demand")
        print("   ♻️  Garbage collection enabled - old partitions will be freed automatically")

    def _get_or_build_partition(self, date, state, election_type):
        """Lazy-load partition with garbage collection when partition changes"""
        partition_key = f"{date}_{state}_{election_type}"

        # Check if we've moved to a new partition
        if self.last_partition_key is not None and self.last_partition_key != partition_key:
            # Garbage collect old partition
            if self.last_partition_key in self.faiss_indices:
                del self.faiss_indices[self.last_partition_key]
            if self.last_partition_key in self.partition_records:
                del self.partition_records[self.last_partition_key]

            self.partition_gc_count += 1
            self.logger.debug(f"♻️  GC partition {self.partition_gc_count}: {self.last_partition_key}")

        # Build partition if not already cached
        if partition_key not in self.faiss_indices:
            self.logger.debug(f"🔨 Building partition on-demand: {partition_key}")

            # Filter DDHQ records for this partition (date + state + election_type)
            partition_records = self.ddhq_df[
                (self.ddhq_df['date'] == date) &
                (self.ddhq_df['extracted_state'] == state) &
                (self.ddhq_df['election_type'] == election_type)
            ].copy()

            if len(partition_records) == 0:
                self.logger.debug(f"⚠️  Empty partition: {partition_key}")
                self.last_partition_key = partition_key
                return None, None

            # Build FAISS index
            embeddings = np.array(partition_records['embedding_name_race'].tolist(), dtype=np.float32)
            embedding_dim = embeddings.shape[1]
            index = faiss.IndexFlatIP(embedding_dim)
            faiss.normalize_L2(embeddings)
            index.add(embeddings)

            # Cache
            self.faiss_indices[partition_key] = index
            self.partition_records[partition_key] = partition_records
            self.partition_build_count += 1

            self.logger.debug(f"✅ Built partition {self.partition_build_count}: {partition_key} ({len(partition_records)} records)")

        # Update last partition tracker
        self.last_partition_key = partition_key

        return self.faiss_indices.get(partition_key), self.partition_records.get(partition_key)

    def _init_llm(self):
        print("🤖 Initializing Gemini LLM...")
        # Configure for HIGH THROUGHPUT with reliability (reduced from 1200 to avoid API corruption)
        target_concurrency = 500  # Balanced for throughput + reliability (~5k/min target)
        self.llm_client = GeminiClient(
            default_model=GeminiModelType.FLASH,
            default_temperature=0.0,
            max_connections=target_concurrency,
            max_keepalive_connections=target_concurrency // 4  # 125 keepalive
            # Removed thinking_budget to avoid token limit issues that cause JSON truncation
        )
        print(f"   Gemini Flash initialized with {target_concurrency} max connections (HIGH THROUGHPUT with reliability)")

    def _search_similar_candidates(self, hubspot_record: Dict, k: int = 5) -> List[Dict[str, Any]]:
        """Search for k most similar DDHQ candidates using date + state + election type partitioned FAISS"""

        search_start_time = time.time()

        # Get target date, state, and election type from HubSpot record
        target_date = hubspot_record.get('election_date')
        target_state = hubspot_record.get('state')
        target_election_type = hubspot_record.get('election_type')

        if pd.isna(target_date) or pd.isna(target_state) or pd.isna(target_election_type):
            self.logger.warning(f"Missing date, state, or election_type for candidate: {self._get_candidate_name(hubspot_record)}")
            return []  # Cannot search without all three keys

        # Lazy-load partition with garbage collection
        faiss_index, partition_records = self._get_or_build_partition(target_date, target_state, target_election_type)

        if faiss_index is None or partition_records is None:
            partition_key = f"{target_date}_{target_state}_{target_election_type}"
            self.logger.warning(f"No partition found for {partition_key}")
            return []
        
        # Prepare embedding for search
        hubspot_embedding = np.array(hubspot_record['embedding_name_race'])
        query_embedding = hubspot_embedding.astype(np.float32).reshape(1, -1)
        faiss.normalize_L2(query_embedding)
        
        # Search within the specific date + election type partition
        search_actual_start = time.time()
        similarities, indices = faiss_index.search(query_embedding, min(k, len(partition_records)))
        search_actual_duration = time.time() - search_actual_start
        
        # Check if FAISS returned any valid results
        if len(similarities[0]) == 0 or len(indices[0]) == 0:
            self.logger.debug(f"FAISS returned empty results for partition {partition_key}")
            return []
        
        # Convert results with comprehensive bounds checking
        all_candidates = []
        for similarity, idx in zip(similarities[0], indices[0]):
            # Comprehensive safety checks
            if idx < 0 or idx >= len(partition_records):  
                self.logger.warning(f"FAISS returned invalid index {idx} for partition {partition_key} with {len(partition_records)} records")
                continue
            if similarity <= 0:  # Skip non-matches
                continue
                
            ddhq_record = partition_records.iloc[idx]
            all_candidates.append({
                'rank': len(all_candidates) + 1,
                'similarity': float(similarity),
                'ddhq_index': ddhq_record.name,  # Original DataFrame index
                'candidate': ddhq_record['candidate'],
                'race_name': ddhq_record['race_name'],
                'candidate_party': ddhq_record.get('candidate_party', 'N/A'),
                'is_winner': ddhq_record.get('is_winner', 'N/A'),
                'embedding_text': ddhq_record.get('embedding_name_race_text', 'N/A'),
                'ddhq_race_id': ddhq_record.get('race_id', 'N/A'),
                'ddhq_candidate_id': ddhq_record.get('candidate_id', 'N/A'),
                'election_type': ddhq_record.get('election_type', 'N/A'),  # Will always match HubSpot
                'date': ddhq_record.get('date', 'N/A')  # Will always match HubSpot
            })
        
        # Sort by similarity and return top k
        all_candidates.sort(key=lambda x: x['similarity'], reverse=True)
        
        search_total_duration = time.time() - search_start_time
        
        return all_candidates[:k]
    
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
            'name': self._get_candidate_name(hubspot_record),
            'full_name': hubspot_record.get('full_name', 'N/A'),
            'state': hubspot_record.get('state', 'N/A'),
            'city': hubspot_record.get('city', 'N/A'),
            'office': hubspot_record.get('official_office_name', hubspot_record.get('candidate_office', 'N/A')),
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
        
        prompt = f"""You are a political candidate matching expert. Your task is to match LOCAL/MUNICIPAL candidates between HubSpot and DDHQ databases with EXTREME PRECISION to avoid false positives.

NOTE: Federal races (Congress, Senate, President) and State-level races (Governor, State Legislature) have already been filtered out. You are ONLY matching local/municipal races (City Council, Mayor, County offices, School Board, Township, etc.).

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
   - State: DDHQ race MUST be in the same state as HubSpot candidate
   - Jurisdiction: For local races, city/county/municipality must match
   - REJECT: Any cross-state or cross-jurisdiction matches

3. VALIDATION EXAMPLES:

   VALID LOCAL MATCHES (High Confidence):
   - "John Smith" for Berkeley City Council → "John Smith, Berkeley City Council" (exact match)
   - "Bob Johnson" for County Commissioner → "Robert Johnson, County Commissioner" (nickname variation)
   - "Mary O'Connor" for School Board → "Mary O'Connor, School Board" (exact match)

   INVALID LOCAL MATCHES (Must Reject):
   - "Stanley Pokras" → "Susan Kopras" (gender mismatch, different first name)
   - "Test User" → "Ronald Test" (test data, unrelated)
   - "Marco Huerta" → "Erick Huerta" (different first name entirely)
   - "Jane Doe, Arlington City Council" → "Jane Doe, Arlington County Board" (different jurisdiction type)
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
        
        llm_validation_start = time.time()
        
        for attempt in range(max_retries):
            try:
                # Use dedicated thread pool for maximum LLM concurrency
                llm_call_start = time.time()
                response = await asyncio.get_event_loop().run_in_executor(
                    self.thread_pool,
                    lambda: self.llm_client.generate_content(
                        prompt,
                        max_tokens=200  # Keep response short to avoid truncation
                    )
                )
                llm_call_duration = time.time() - llm_call_start
                
                # Handle None response from API failures
                if response is None:
                    raise ValueError("LLM returned None response - likely API failure")
                
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
                    else:
                        # Handle invalid match index
                        self.logger.warning(f"LLM returned invalid best_match index {result['best_match']} for {len(similar_candidates)} candidates")
                        result['best_match'] = None
                        result['confidence'] = 0
                        result['reasoning'] = f"Invalid match index {result['best_match']} for {len(similar_candidates)} candidates"
                
                # Track LLM cost
                stats = self.llm_client.get_usage_stats()
                self.total_cost = stats.get('total_cost', 0.0)
                
                llm_validation_total = time.time() - llm_validation_start
                candidate_name = self._get_candidate_name(hubspot_record)
                self.logger.debug(f"🤖 LLM validation for {candidate_name}: Total={llm_validation_total:.3f}s | Call={llm_call_duration:.3f}s | Attempts={attempt+1}")
                
                return result
                
            except Exception as e:
                # Enhanced error logging with more context
                error_context = {
                    'candidate_name': self._get_candidate_name(hubspot_record),
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

        record_start_time = time.time()

        try:
            office_level = str(hubspot_record.get('office_level', '')).strip().upper()

            if office_level == 'FEDERAL':
                self.logger.debug(f"Record {record_index}: Pre-filtered as FEDERAL race")
                return {
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
                    'hubspot_election_date': hubspot_record.get('election_date', 'N/A'),
                    'hubspot_election_type': hubspot_record.get('election_type', 'N/A'),
                    'llm_best_match': None,
                    'llm_confidence': 100.0,
                    'llm_reasoning': 'Federal race (pre-filtered by office_level field)',
                    'top_10_candidates': 'FEDERAL_RACE',
                    'has_match': False,
                    'ddhq_matched_index': None,
                    'ddhq_candidate': 'FEDERAL_RACE',
                    'ddhq_race_name': 'FEDERAL_RACE',
                    'ddhq_candidate_party': None,
                    'ddhq_is_winner': None,
                    'ddhq_race_id': None,
                    'ddhq_candidate_id': None,
                    'ddhq_election_type': None,
                    'ddhq_date': None,
                    'ddhq_embedding_text': None,
                    'match_similarity': None
                }

            if office_level == 'STATE':
                self.logger.debug(f"Record {record_index}: Pre-filtered as STATE race")
                return {
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
                    'hubspot_election_date': hubspot_record.get('election_date', 'N/A'),
                    'hubspot_election_type': hubspot_record.get('election_type', 'N/A'),
                    'llm_best_match': None,
                    'llm_confidence': 100.0,
                    'llm_reasoning': 'State race (pre-filtered by office_level field)',
                    'top_10_candidates': 'STATE_RACE',
                    'has_match': False,
                    'ddhq_matched_index': None,
                    'ddhq_candidate': 'STATE_RACE',
                    'ddhq_race_name': 'STATE_RACE',
                    'ddhq_candidate_party': None,
                    'ddhq_is_winner': None,
                    'ddhq_race_id': None,
                    'ddhq_candidate_id': None,
                    'ddhq_election_type': None,
                    'ddhq_date': None,
                    'ddhq_embedding_text': None,
                    'match_similarity': None
                }

            # Step 1: Date-partitioned FAISS similarity search
            faiss_start_time = time.time()
            similar_candidates = self._search_similar_candidates(hubspot_record.to_dict(), k=10)
            faiss_duration = time.time() - faiss_start_time
            
            # Step 2: LLM validation
            llm_start_time = time.time()
            llm_result = await self._validate_with_llm(hubspot_record.to_dict(), similar_candidates)
            llm_duration = time.time() - llm_start_time
            
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
                'hubspot_election_date': hubspot_record.get('election_date', 'N/A'),
                'hubspot_election_type': hubspot_record.get('election_type', 'N/A'),
                
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
                if 0 <= best_match_idx < len(similar_candidates):
                    best_candidate = similar_candidates[best_match_idx]
                else:
                    # Handle invalid match index - treat as no match
                    self.logger.warning(f"Invalid best_match index {llm_result['best_match']} for {len(similar_candidates)} candidates in record {record_index}")
                    llm_result['best_match'] = None
                    llm_result['confidence'] = 0
                    best_candidate = None
                
                if best_candidate is not None:
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
            
            # Log detailed timing for performance analysis
            total_duration = time.time() - record_start_time
            candidate_name = self._get_candidate_name(hubspot_record.to_dict())
            self.logger.info(f"⏱️  Record {record_index} ({candidate_name}): TOTAL={total_duration:.3f}s | FAISS={faiss_duration:.3f}s | LLM={llm_duration:.3f}s | Match={llm_result.get('best_match') is not None}")
            
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
        
        # Process in maximum groups for instant launching
        group_size = min(self.max_workers, 200)  # Maximum group size for instant launch
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
            
            # Calculate throughput metrics
            records_per_minute = records_per_second * 60
            projected_10k_time = 10000 / records_per_second / 60 if records_per_second > 0 else float('inf')
            
            self.logger.info(f"✅ Batch {batch_num}/{total_batches} completed ({progress_pct:.1f}%)")
            self.logger.info(f"   📊 {batch_size} records in {duration:.1f}s ({records_per_second:.1f} rec/sec)")
            self.logger.info(f"   🚀 Throughput: {records_per_minute:.0f} rec/min | 10K would take: {projected_10k_time:.1f} min")
            self.logger.info(f"   🎯 Batch matches: {batch_matches}/{len(processed_rows)} ({batch_match_rate:.1f}%)")
            self.logger.info(f"   💰 Running cost: ${self.total_cost:.6f}")
        
        return pd.DataFrame(processed_rows)
    
    async def process_all_records(self) -> pd.DataFrame:
        """Process all HubSpot records with MASSIVE CONCURRENCY for 10k/min target"""
        
        if self.max_records:
            records_to_process = self.hubspot_df.sample(n=min(self.max_records, len(self.hubspot_df)), random_state=123)
            print(f"   🎲 Using random sample of {len(records_to_process):,} records (seed=123 for reproducibility)")
        else:
            records_to_process = self.hubspot_df
        
        print(f"\n🚀 Processing {len(records_to_process):,} HubSpot records with HIGH THROUGHPUT CONCURRENCY...")
        print(f"   Target: 10,000 records/minute = 140 records/second (MATCHED SPAWN RATE)")
        print(f"   Workers: {self.max_workers} (designed for EXTREME 10k/min throughput)")
        
        # SPEED OPTIMIZATION: Process ALL records concurrently (not in sequential batches)
        print(f"   🔥 Creating {len(records_to_process):,} concurrent tasks...")
        
        # Create ALL tasks at once - maximum concurrency
        all_tasks = []
        for idx, (_, row) in enumerate(records_to_process.iterrows()):
            task = self._process_hubspot_record(row, idx)
            all_tasks.append(task)
        
        print(f"   ⚡ Launching {len(all_tasks):,} concurrent LLM calls...")
        
        # Use tqdm for progress tracking
        pbar = tqdm(total=len(all_tasks), desc="Processing records", unit="record")
        
        # BATCH LAUNCHING: Overcome sub-millisecond timing precision limits
        start_time = time.time()
        results = [None] * len(all_tasks)  # Pre-allocate results array
        
        calls_per_second = 140  # 10k records/minute = 140 records/second spawn rate
        batch_size = 140  # Launch 140 calls per batch
        batch_interval = 1.0  # 1 second between batches for 10k/min
        actual_rate = batch_size / batch_interval  # 140 / 1.0 = 140/sec = 10k/min
        
        print(f"   🎯 Target rate: {calls_per_second} calls/second ({calls_per_second * 60}/min) - 10K/MIN SPAWN RATE")
        print(f"   📦 Batch strategy: {batch_size} calls every {batch_interval*1000:.0f}ms (10k/min pacing)")
        print(f"   ⚡ Actual rate: {actual_rate:.0f} calls/second")
        
        # Track running tasks
        running_tasks = {}
        completed_count = 0
        total_launched = 0
        
        # Launch tasks in batches with precise timing
        batch_count = 0
        for batch_start in range(0, len(all_tasks), batch_size):
            batch_tasks = all_tasks[batch_start:batch_start + batch_size]
            batch_count += 1
            
            # Launch entire batch simultaneously
            batch_launch_start = time.time()
            for i, task in enumerate(batch_tasks):
                task_index = batch_start + i
                actual_task = asyncio.create_task(task)
                running_tasks[task_index] = actual_task
                total_launched += 1
            
            batch_launch_time = time.time() - batch_launch_start
            elapsed_total = time.time() - start_time
            current_rate = total_launched / elapsed_total if elapsed_total > 0 else 0
            
            # Log batch timing
            print(f"   📦 Batch {batch_count:2d}: Launched {len(batch_tasks):2d} tasks in {batch_launch_time*1000:.1f}ms | Total: {total_launched:4d} | Rate: {current_rate:.0f}/sec")
            
            # Wait for next batch (only if more batches remain)
            if batch_start + batch_size < len(all_tasks):
                await asyncio.sleep(batch_interval)
            
            # Check for completed tasks every 5 starts to maintain awareness
            if batch_count % 5 == 0:
                # Check for completed tasks
                completed_indices = []
                for task_idx, running_task in list(running_tasks.items()):
                    if running_task.done():
                        try:
                            results[task_idx] = await running_task
                        except Exception as e:
                            results[task_idx] = e
                        completed_indices.append(task_idx)
                        completed_count += 1
                
                # Remove completed tasks
                for idx in completed_indices:
                    del running_tasks[idx]
                
                # Update progress
                elapsed = time.time() - start_time
                actual_rate = total_launched / elapsed if elapsed > 0 else 0
                pbar.update(len(completed_indices))
                pbar.set_postfix({
                    'started': i + 1,
                    'completed': completed_count,
                    'rate': f"{actual_rate:.1f}/sec",
                    'running': len(running_tasks)
                })
        
        print(f"   ✅ All {len(all_tasks)} tasks started")
        print(f"   ⏳ Waiting for remaining {len(running_tasks)} tasks to complete...")
        
        # Wait for all remaining tasks to complete
        while running_tasks:
            completed_indices = []
            for task_idx, running_task in list(running_tasks.items()):
                if running_task.done():
                    try:
                        results[task_idx] = await running_task
                    except Exception as e:
                        results[task_idx] = e
                    completed_indices.append(task_idx)
                    completed_count += 1
            
            # Remove completed tasks
            for idx in completed_indices:
                del running_tasks[idx]
            
            # Update progress
            pbar.update(len(completed_indices))
            pbar.set_postfix({
                'completed': completed_count,
                'remaining': len(running_tasks)
            })
            
            # Small delay to avoid busy waiting
            if running_tasks:
                await asyncio.sleep(0.1)
        
        pbar.close()
        total_duration = time.time() - start_time
        
        # Process results
        processed_rows = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # Handle exceptions
                row = records_to_process.iloc[i].copy()
                error_result = {
                    'hubspot_row_index': i,
                    'hubspot_first_name': row.get('first_name', 'N/A'),
                    'hubspot_last_name': row.get('last_name', 'N/A'),
                    'llm_best_match': None,
                    'llm_confidence': 0,
                    'llm_reasoning': f'Concurrent processing error: {str(result)}',
                    'has_match': False,
                    'top_10_candidates': 'CONCURRENT_ERROR'
                }
                processed_rows.append(error_result)
            else:
                processed_rows.append(result)
        
        # Calculate final performance metrics
        total_records = len(processed_rows)
        records_per_second = total_records / max(0.1, total_duration)
        records_per_minute = records_per_second * 60
        target_achievement = (records_per_minute / 10000) * 100  # 10k/min target
        
        print(f"\n🎯 PERFORMANCE RESULTS:")
        print(f"   Total time: {total_duration:.1f}s")
        print(f"   Records processed: {total_records:,}")
        print(f"   Speed: {records_per_second:.1f} rec/sec = {records_per_minute:.0f} rec/min")
        print(f"   Target achievement: {target_achievement:.1f}% of 10k/min goal")
        print(f"   Matches found: {self.matched_count:,} ({self.matched_count/self.processed_count*100:.1f}%)")
        print(f"   Total LLM cost: ${self.total_cost:.2f}")

        print(f"\n♻️  LAZY-LOADING STATISTICS:")
        print(f"   Partitions built: {self.partition_build_count}")
        print(f"   Partitions GC'd: {self.partition_gc_count}")
        print(f"   Peak partitions in memory: {self.partition_build_count - self.partition_gc_count}")
        print(f"   Memory efficiency: {self.partition_gc_count / max(1, self.partition_build_count) * 100:.1f}% of partitions freed")
        
        return pd.DataFrame(processed_rows)
    
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

        # Calculate match statistics
        total_records = len(results_df)
        local_matches = results_df['has_match'].sum()
        federal_races = (results_df['ddhq_race_name'] == 'FEDERAL_RACE').sum()
        state_races = (results_df['ddhq_race_name'] == 'STATE_RACE').sum()
        no_match = total_records - local_matches - federal_races - state_races

        print(f"\n💾 Results saved to output folder:")
        print(f"   Parquet: {parquet_file} ({parquet_size_mb:.1f} MB)")
        print(f"   TSV: {tsv_file} ({tsv_size_mb:.1f} MB)")
        print(f"\n📊 MATCH STATISTICS:")
        print(f"   Total records: {total_records:,}")
        print(f"   Local/municipal matches: {local_matches:,} ({local_matches/total_records*100:.1f}%)")
        print(f"   Federal races (pre-filtered): {federal_races:,} ({federal_races/total_records*100:.1f}%)")
        print(f"   State races (pre-filtered): {state_races:,} ({state_races/total_records*100:.1f}%)")
        print(f"   No match: {no_match:,} ({no_match/total_records*100:.1f}%)")

        if local_matches > 0:
            matched_df = results_df[results_df['has_match'] == True]
            avg_confidence = matched_df['llm_confidence'].mean()
            print(f"   Average confidence (local matches): {avg_confidence:.1f}%")

        print(f"\n   Latest files also saved for easy access")

        return parquet_file
    
    def cleanup(self):
        """Cleanup resources"""
        if hasattr(self, 'thread_pool'):
            self.thread_pool.shutdown(wait=True)

async def main():
    # Configuration - OPTIMIZED FOR HIGH THROUGHPUT WITH RELIABILITY
    BATCH_SIZE = int(os.getenv('BATCH_SIZE', 1000))  # Process 1000 records concurrently
    MAX_RECORDS = int(os.getenv('MAX_RECORDS')) if os.getenv('MAX_RECORDS') else None
    MAX_WORKERS = int(os.getenv('MAX_WORKERS', 500))  # Balanced for reliability (~5k/min)
    
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