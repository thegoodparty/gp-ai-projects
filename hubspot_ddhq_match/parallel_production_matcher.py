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
from collections import OrderedDict
from datetime import datetime
from typing import List, Dict, Any, Optional
from tqdm.asyncio import tqdm
from concurrent.futures import ThreadPoolExecutor
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from shared.braintrust import (
    init_braintrust,
    cache_prompt,
    build_cached_prompt,
)
from shared.llm_gemini_3 import Gemini3Client, GeminiModelType, ThinkingLevel
from shared.logger import get_logger

class ParallelProductionMatcher:
    def __init__(self, batch_size: int = 1000, max_records: Optional[int] = None, max_workers: int = 1500,
                 hubspot_file: Optional[str] = None, ddhq_file: Optional[str] = None):
        self.logger = get_logger(__name__)
        self.batch_size = batch_size
        self.max_records = max_records
        self.max_workers = max_workers
        self.hubspot_file_override = hubspot_file
        self.ddhq_file_override = ddhq_file

        self.logger.info("🚀 PARALLEL PRODUCTION HUBSPOT-DDHQ MATCHER")
        self.logger.info("=" * 50)
        self.logger.info(f"🔧 Configuration: {max_workers} workers, batch size {batch_size}")

        # Load data and initialize lazy-loading
        self._load_data()
        self._init_lazy_loading()
        self._init_llm()
        
        # Initialize progress tracking
        self.processed_count = 0
        self.matched_count = 0
        self.total_cost = 0.0
        
        # Create ThreadPoolExecutor matching semaphore limit
        # Keep high worker count - HTTP connection pool limits actual concurrency
        self.thread_pool = ThreadPoolExecutor(max_workers=self.max_workers)

        # Add semaphore for bounded concurrency to prevent OOM
        self.semaphore = asyncio.Semaphore(self.max_workers)

        records_to_process = min(len(self.hubspot_df), max_records) if max_records else len(self.hubspot_df)
        self.logger.info(f"\n✅ Ready to process {records_to_process:,} temporally-aligned HubSpot records")
    
    def _get_candidate_name(self, hubspot_record: Dict) -> str:
        """Helper method to construct candidate name from HubSpot record"""
        return f"{hubspot_record.get('first_name', '')} {hubspot_record.get('last_name', '')}".strip()

    def _is_missing(self, x) -> bool:
        """Helper to check if value is missing (None, NaN, or blank string)"""
        return pd.isna(x) or (isinstance(x, str) and not x.strip())

    async def _process_hubspot_record_with_semaphore(self, hubspot_record: pd.Series, record_index: int) -> Dict[str, Any]:
        """Wrapper for _process_hubspot_record that uses semaphore to prevent OOM"""
        async with self.semaphore:
            return await self._process_hubspot_record(hubspot_record, record_index)

    def _load_data(self):
        self.logger.info("📥 Loading offline data...")
        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")

        # Use override files if provided, otherwise use defaults
        if self.hubspot_file_override and self.ddhq_file_override:
            hubspot_file = self.hubspot_file_override
            ddhq_file = self.ddhq_file_override
            self.logger.info(f"   Using custom data files:")
            self.logger.info(f"   - HubSpot: {os.path.basename(hubspot_file)}")
            self.logger.info(f"   - DDHQ: {os.path.basename(ddhq_file)}")
        else:
            # Use full embeddings dataset
            hubspot_file = os.path.join(offline_data_dir, 'hubspot_filtered_with_embeddings_latest.parquet')
            ddhq_file = os.path.join(offline_data_dir, 'ddhq_with_embeddings_cleaned_latest.parquet')

        self.hubspot_df = pd.read_parquet(hubspot_file)
        self.ddhq_df = pd.read_parquet(ddhq_file)
        
        # Sort HubSpot data by date + state + election_type for optimal cache performance
        self.logger.info("🔄 Sorting HubSpot data by (date, state, election_type) for lazy-load cache optimization...")
        self.hubspot_df = self.hubspot_df.sort_values(
            ['election_date', 'state', 'election_type'],
            na_position='last'
        )
        
        self.logger.info(f"   HubSpot (filtered by DDHQ dates): {len(self.hubspot_df):,} records")
        self.logger.info(f"   DDHQ (full dataset): {len(self.ddhq_df):,} records")
    
    def _init_lazy_loading(self):
        self.logger.info("🔍 Initializing lazy-loading with LRU cache (Date + State + Election Type)...")

        # MEMORY OPTIMIZATION: Lazy-load partitions with LRU eviction (not pre-build all)
        self.faiss_indices = OrderedDict()  # "{date}_{state}_{election_type}" -> FAISS index (LRU cache)
        self.partition_records = OrderedDict()   # "{date}_{state}_{election_type}" -> DataFrame slice (LRU cache)
        self.empty_partitions = set()  # Track empty partitions to avoid rebuilding
        self.MAX_CACHE_SIZE = 64  # LRU cache limit
        self.partition_build_count = 0  # Statistics
        self.partition_gc_count = 0  # Statistics

        # Thread safety for concurrent partition building
        import threading
        self.partition_lock = threading.Lock()

        self.logger.info(f"   ✅ Lazy-loading initialized with LRU cache (max {self.MAX_CACHE_SIZE} partitions)")
        self.logger.info("   ♻️  LRU eviction enabled - least recently used partitions will be freed automatically")

    def _get_or_build_partition(self, date, state, election_type):
        """Lazy-load partition with LRU eviction (thread-safe)"""
        partition_key = f"{date}_{state}_{election_type}"

        # Fast path: Check if already built or known to be empty (no lock needed)
        if partition_key in self.empty_partitions:
            return None, None
        if partition_key in self.faiss_indices:
            # Mark as recently used (move to end of OrderedDict)
            with self.partition_lock:
                self.faiss_indices.move_to_end(partition_key)
                self.partition_records.move_to_end(partition_key)
            return self.faiss_indices[partition_key], self.partition_records[partition_key]

        # Slow path: Need to build partition (acquire lock to prevent race conditions)
        with self.partition_lock:
            # Double-check after acquiring lock (another thread might have built it)
            if partition_key in self.empty_partitions:
                return None, None
            if partition_key in self.faiss_indices:
                self.faiss_indices.move_to_end(partition_key)
                self.partition_records.move_to_end(partition_key)
                return self.faiss_indices[partition_key], self.partition_records[partition_key]

            # Build the partition
            self.logger.debug(f"🔨 Building partition on-demand: {partition_key}")

            # Filter DDHQ records for this partition (date + state + election_type)
            partition_records = self.ddhq_df[
                (self.ddhq_df['date'] == date) &
                (self.ddhq_df['extracted_state'] == state) &
                (self.ddhq_df['election_type'] == election_type)
            ].copy()

            if len(partition_records) == 0:
                self.logger.debug(f"⚠️  Empty partition: {partition_key}")
                self.empty_partitions.add(partition_key)  # Cache empty result
                return None, None

            # Build FAISS index
            embeddings = np.array(partition_records['embedding_name_race'].tolist(), dtype=np.float32)
            embedding_dim = embeddings.shape[1]
            index = faiss.IndexFlatIP(embedding_dim)
            faiss.normalize_L2(embeddings)
            index.add(embeddings)

            # Cache the results
            self.faiss_indices[partition_key] = index
            self.partition_records[partition_key] = partition_records
            self.partition_build_count += 1

            # LRU eviction: Remove oldest entry if cache is full
            if len(self.faiss_indices) > self.MAX_CACHE_SIZE:
                oldest_key, _ = self.faiss_indices.popitem(last=False)
                self.partition_records.popitem(last=False)
                self.partition_gc_count += 1
                self.logger.debug(f"♻️  LRU eviction {self.partition_gc_count}: {oldest_key} (cache size: {len(self.faiss_indices)})")

            self.logger.debug(f"✅ Built partition {self.partition_build_count}: {partition_key} ({len(partition_records)} records, cache: {len(self.faiss_indices)}/{self.MAX_CACHE_SIZE})")

            return index, partition_records

    def _get_or_build_date_range_partition(self, base_date, state, runoff_type):
        """
        Build a date-range partition for runoff elections that includes ALL races with dates >= base_date.

        Args:
            base_date: The starting date (general_election_date or primary_election_date)
            state: State abbreviation
            runoff_type: 'runoff' (the HubSpot election type)

        Returns:
            tuple: (faiss_index, partition_records) or (None, None) if empty
        """
        partition_key = f"AFTER_{base_date}_{state}_{runoff_type}"

        # Check cache and mark as recently used
        if partition_key in self.faiss_indices:
            with self.partition_lock:
                self.faiss_indices.move_to_end(partition_key)
                self.partition_records.move_to_end(partition_key)
            return self.faiss_indices[partition_key], self.partition_records[partition_key]

        # Build date-range partition if not already cached
        with self.partition_lock:
            # Double-check after lock
            if partition_key in self.faiss_indices:
                self.faiss_indices.move_to_end(partition_key)
                self.partition_records.move_to_end(partition_key)
                return self.faiss_indices[partition_key], self.partition_records[partition_key]
            self.logger.debug(f"🔨 Building DATE-RANGE partition on-demand: {partition_key}")

            # Convert base_date to datetime for comparison
            base_date_dt = pd.to_datetime(base_date)

            # Determine which election types to include based on the base election
            # We need to infer if this is a primary runoff or general runoff
            # Check which election types exist on the base_date in this state
            base_date_elections = self.ddhq_df[
                (self.ddhq_df['date'] == base_date) &
                (self.ddhq_df['extracted_state'] == state)
            ]

            # Determine if this is primary-based or general-based runoff
            has_general_on_base = 'general' in base_date_elections['election_type'].values
            has_primary_on_base = 'primary' in base_date_elections['election_type'].values

            # Include appropriate election types
            if has_general_on_base:
                # General runoff: include general + runoff types
                valid_types = ['general', 'runoff']
                self.logger.debug(f"   Detected GENERAL runoff - including types: {valid_types}")
            elif has_primary_on_base:
                # Primary runoff: include primary + runoff types
                valid_types = ['primary', 'runoff']
                self.logger.debug(f"   Detected PRIMARY runoff - including types: {valid_types}")
            else:
                # Unknown base - include all runoff-related types
                valid_types = ['primary', 'general', 'runoff']
                self.logger.debug(f"   Unknown base election type - including all types: {valid_types}")

            # Filter DDHQ records: date >= base_date AND state = target_state AND election_type matches
            partition_records = self.ddhq_df[
                (pd.to_datetime(self.ddhq_df['date']) >= base_date_dt) &
                (self.ddhq_df['extracted_state'] == state) &
                (self.ddhq_df['election_type'].isin(valid_types))
            ].copy()

            if len(partition_records) == 0:
                self.logger.debug(f"⚠️  Empty DATE-RANGE partition: {partition_key}")
                return None, None

            # Get unique dates included
            unique_dates = partition_records['date'].unique()
            self.logger.debug(f"   Date-range partition includes {len(unique_dates)} unique dates: {sorted(unique_dates)}")

            # Build FAISS index from aggregated records
            embeddings = np.array(partition_records['embedding_name_race'].tolist(), dtype=np.float32)
            embedding_dim = embeddings.shape[1]
            index = faiss.IndexFlatIP(embedding_dim)
            faiss.normalize_L2(embeddings)
            index.add(embeddings)

            # Cache
            self.faiss_indices[partition_key] = index
            self.partition_records[partition_key] = partition_records
            self.partition_build_count += 1

            # LRU eviction: Remove oldest entry if cache is full
            if len(self.faiss_indices) > self.MAX_CACHE_SIZE:
                oldest_key, _ = self.faiss_indices.popitem(last=False)
                self.partition_records.popitem(last=False)
                self.partition_gc_count += 1
                self.logger.debug(f"♻️  LRU eviction {self.partition_gc_count}: {oldest_key} (cache size: {len(self.faiss_indices)})")

            self.logger.debug(f"✅ Built DATE-RANGE partition {self.partition_build_count}: {partition_key} ({len(partition_records)} records from {len(unique_dates)} dates, cache: {len(self.faiss_indices)}/{self.MAX_CACHE_SIZE})")

            return index, partition_records

    def _get_or_build_stateless_partition(self, date, election_type):
        """
        Build a state-less fallback partition for records missing state data.
        Aggregates ALL states for the given date+election_type.

        Args:
            date: Election date
            election_type: Election type (primary, general, runoff)

        Returns:
            tuple: (faiss_index, partition_records) or (None, None) if empty
        """
        partition_key = f"{date}_ALL_STATES_{election_type}"

        # Check cache and mark as recently used
        if partition_key in self.faiss_indices:
            with self.partition_lock:
                self.faiss_indices.move_to_end(partition_key)
                self.partition_records.move_to_end(partition_key)
            return self.faiss_indices[partition_key], self.partition_records[partition_key]

        # Build state-less partition if not already cached
        with self.partition_lock:
            # Double-check after lock
            if partition_key in self.faiss_indices:
                self.faiss_indices.move_to_end(partition_key)
                self.partition_records.move_to_end(partition_key)
                return self.faiss_indices[partition_key], self.partition_records[partition_key]
            self.logger.debug(f"🔨 Building STATE-LESS partition on-demand: {partition_key}")

            # Filter DDHQ records: date = target_date AND election_type = target_type (all states)
            partition_records = self.ddhq_df[
                (self.ddhq_df['date'] == date) &
                (self.ddhq_df['election_type'] == election_type)
            ].copy()

            if len(partition_records) == 0:
                self.logger.debug(f"⚠️  Empty STATE-LESS partition: {partition_key}")
                return None, None

            # Get unique states included
            unique_states = partition_records['extracted_state'].unique()
            self.logger.debug(f"   State-less partition includes {len(unique_states)} states: {sorted(unique_states)}")

            # Build FAISS index from aggregated records
            embeddings = np.array(partition_records['embedding_name_race'].tolist(), dtype=np.float32)
            embedding_dim = embeddings.shape[1]
            index = faiss.IndexFlatIP(embedding_dim)
            faiss.normalize_L2(embeddings)
            index.add(embeddings)

            # Cache
            self.faiss_indices[partition_key] = index
            self.partition_records[partition_key] = partition_records
            self.partition_build_count += 1

            # LRU eviction: Remove oldest entry if cache is full
            if len(self.faiss_indices) > self.MAX_CACHE_SIZE:
                oldest_key, _ = self.faiss_indices.popitem(last=False)
                self.partition_records.popitem(last=False)
                self.partition_gc_count += 1
                self.logger.debug(f"♻️  LRU eviction {self.partition_gc_count}: {oldest_key} (cache size: {len(self.faiss_indices)})")

            self.logger.debug(f"✅ Built STATE-LESS partition {self.partition_build_count}: {partition_key} ({len(partition_records)} records from {len(unique_states)} states, cache: {len(self.faiss_indices)}/{self.MAX_CACHE_SIZE})")

            return index, partition_records

    def _init_llm(self):
        self.logger.info("🤖 Initializing Gemini LLM...")
        # Configure for HIGH THROUGHPUT with reliability (reduced from 1200 to avoid API corruption)
        target_concurrency = 500  # Balanced for throughput + reliability (~5k/min target)
        self.llm_client = Gemini3Client(
            default_model=GeminiModelType.FLASH_3,
            default_temperature=0.0,
            thinking_level=ThinkingLevel.MINIMAL,
            max_connections=target_concurrency,
            max_keepalive_connections=target_concurrency // 4,
            max_retries=9,
            base_delay=1.0
        )
        self.logger.info(f"   Gemini 3 Flash initialized with {target_concurrency} max connections, 9 retries (HIGH THROUGHPUT with reliability)")

        environment = os.getenv("ENVIRONMENT", "local")
        self.logger.info(f"Braintrust environment: {environment}")
        init_braintrust(project="hubspot-ddhq-match")

        self._init_prompt_cache()

    def _init_prompt_cache(self):
        self.logger.info("📝 Loading Braintrust prompt template (one-time)...")
        self._prompt_name = "hubspot-ddhq-match-validator"

        warmup_vars = {
            "hubspot_name": "warmup",
            "hubspot_full_name": "warmup",
            "hubspot_state": "warmup",
            "hubspot_city": "warmup",
            "hubspot_office": "warmup",
            "hubspot_embedding_text": "warmup",
            "candidates_text": "warmup"
        }

        prompt_obj = cache_prompt(self._prompt_name, warmup_variables=warmup_vars)
        if prompt_obj is not None:
            self.logger.info("   ✅ Braintrust prompt cached (subsequent builds are ~0.03ms)")
        else:
            self.logger.warning("   ⚠️ Braintrust prompt not available, using fallback")

    def _build_prompt(self, hubspot_info: Dict, candidates_text: str) -> str:
        variables = {
            "hubspot_name": hubspot_info['name'],
            "hubspot_full_name": hubspot_info['full_name'],
            "hubspot_state": hubspot_info['state'],
            "hubspot_city": hubspot_info['city'],
            "hubspot_office": hubspot_info['office'],
            "hubspot_embedding_text": hubspot_info['embedding_text'],
            "candidates_text": candidates_text
        }

        fallback = f"""TASK: Match a HubSpot candidate to DDHQ candidates and return JSON ONLY.
NOTE: Federal and state races are pre-filtered. Only match local/municipal races.

OUTPUT FORMAT (REQUIRED):
{{"best_match": <number or null>, "confidence": <0-100>, "reasoning": "<brief explanation>"}}

EXAMPLE OUTPUT:
{{"best_match": 1, "confidence": 95, "reasoning": "Exact name, state, and office match."}}
OR if no match:
{{"best_match": null, "confidence": 0, "reasoning": "No valid match found."}}

---

HubSpot Candidate:
- Name: {hubspot_info['name']}
- Full Name: {hubspot_info['full_name']}
- State: {hubspot_info['state']}
- City: {hubspot_info['city']}
- Office: {hubspot_info['office']}
- Embedding: {hubspot_info['embedding_text']}

DDHQ Candidates (ranked by similarity):
{candidates_text}

MATCHING RULES:

1. NAME REQUIREMENTS (MANDATORY):
   - First name: Must be exact match, clear nickname (Bob/Robert), or obvious variant (Jon/John)
   - Last name: Must be exact match or extremely close phonetic variant
   - Suffixes: Jr., Sr., II, III, IV, V are acceptable variations (John Smith Jr. = John Smith)
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
   - "John Smith Jr." for Mayor → "John Smith, Mayor" (suffix variation - acceptable)

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

Be extremely conservative - false positives are worse than false negatives. If uncertain, REJECT.

OUTPUT JSON ONLY (no explanation, no markdown):"""

        result = build_cached_prompt(self._prompt_name, variables, fallback_prompt=fallback)
        return result if result else fallback

    def _search_similar_candidates(self, hubspot_record: Dict, k: int = 5) -> tuple[List[Dict[str, Any]], str]:
        """
        Search for k most similar DDHQ candidates using date + state + election type partitioned FAISS

        Returns:
            tuple: (candidates, partition_type)
                - candidates: List of similar candidates
                - partition_type: 'standard' or 'stateless_fallback'
        """

        # Get target date, state, and election type from HubSpot record
        target_date = hubspot_record.get('election_date')
        target_state = hubspot_record.get('state')
        target_election_type = hubspot_record.get('election_type')

        # Check if we need state-less fallback partition
        partition_type = 'standard'
        if self._is_missing(target_state):
            if pd.isna(target_date) or pd.isna(target_election_type):
                self.logger.warning(f"Missing date and election_type for candidate: {self._get_candidate_name(hubspot_record)}")
                return ([], 'standard')

            # Use state-less fallback partition (all states for this date+type)
            self.logger.debug(f"Using STATE-LESS partition for candidate without state: {self._get_candidate_name(hubspot_record)}")
            faiss_index, partition_records = self._get_or_build_stateless_partition(target_date, target_election_type)
            partition_type = 'stateless_fallback'
        else:
            # Standard partition with date + state + election_type
            if pd.isna(target_date) or pd.isna(target_election_type):
                self.logger.warning(f"Missing date or election_type for candidate: {self._get_candidate_name(hubspot_record)}")
                return ([], 'standard')

            # Lazy-load partition with garbage collection
            faiss_index, partition_records = self._get_or_build_partition(target_date, target_state, target_election_type)

        if faiss_index is None or partition_records is None:
            # Partition is empty (already logged once during build, now cached)
            return ([], partition_type)

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
            partition_key = f"{target_date}_ALL_STATES_{target_election_type}" if self._is_missing(target_state) else f"{target_date}_{target_state}_{target_election_type}"
            self.logger.debug(f"FAISS returned empty results for partition {partition_key}")
            return ([], partition_type)

        # Convert results with comprehensive bounds checking
        all_candidates = []
        for similarity, idx in zip(similarities[0], indices[0]):
            # Comprehensive safety checks
            partition_key = f"{target_date}_ALL_STATES_{target_election_type}" if self._is_missing(target_state) else f"{target_date}_{target_state}_{target_election_type}"
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

        return (all_candidates[:k], partition_type)

    def _search_similar_candidates_with_runoff_fallback(self, hubspot_record: Dict, k: int = 5) -> tuple[List[Dict[str, Any]], str, str]:
        """
        Search for similar candidates with runoff election date-range logic.

        For runoff elections, builds a date-range partition that includes ALL races with dates >= base election date.

        Returns:
            tuple: (candidates, match_source, fallback_date)
                - candidates: List of similar candidates
                - match_source: 'direct', 'date_range_runoff', or 'stateless_fallback'
                - fallback_date: Base date used for date-range (or None for non-runoff)
        """
        election_type = hubspot_record.get('election_type')

        if election_type != 'runoff':
            # Non-runoff elections use standard search (may use stateless fallback if state is missing)
            candidates, partition_type = self._search_similar_candidates(hubspot_record, k)
            # Pass along partition type as match_source
            match_source = 'stateless_fallback' if partition_type == 'stateless_fallback' else 'direct'
            return (candidates, match_source, None)

        # Runoff election: Build date-range partition from base election date onwards

        target_state = hubspot_record.get('state')

        if self._is_missing(target_state):
            self.logger.warning(f"Runoff record missing state information")
            return ([], 'no_match', None)

        # Determine base date: prefer general_election_date, fallback to primary_election_date, then election_date
        general_date = hubspot_record.get('general_election_date')
        primary_date = hubspot_record.get('primary_election_date')
        election_date = hubspot_record.get('election_date')

        if not pd.isna(general_date):
            base_date = general_date
            base_type = 'general'
            self.logger.debug(f"Runoff using general_election_date as base: {base_date}")
        elif not pd.isna(primary_date):
            base_date = primary_date
            base_type = 'primary'
            self.logger.debug(f"Runoff using primary_election_date as base: {base_date}")
        elif not pd.isna(election_date):
            base_date = election_date
            base_type = 'runoff'
            self.logger.debug(f"Runoff using election_date (runoff date itself) as base: {base_date}")
        else:
            self.logger.warning(f"Runoff record missing all date fields (general, primary, election_date)")
            return ([], 'no_match', None)

        # Build date-range partition: all races with dates >= base_date in this state
        faiss_index, partition_records = self._get_or_build_date_range_partition(
            base_date=str(base_date),
            state=target_state,
            runoff_type='runoff'
        )

        if faiss_index is None or partition_records is None or len(partition_records) == 0:
            self.logger.warning(f"Date-range partition is empty for base_date={base_date}, state={target_state}")
            return ([], 'no_match', None)

        # Search in the date-range partition using HubSpot runoff record's embedding
        hubspot_embedding = np.array(hubspot_record['embedding_name_race'])
        query_embedding = hubspot_embedding.astype(np.float32).reshape(1, -1)
        faiss.normalize_L2(query_embedding)

        # Search FAISS
        similarities, indices = faiss_index.search(query_embedding, min(k, len(partition_records)))

        if len(similarities[0]) == 0 or len(indices[0]) == 0:
            self.logger.debug(f"FAISS returned empty results for date-range partition")
            return ([], 'no_match', None)

        # Convert results
        all_candidates = []
        for similarity, idx in zip(similarities[0], indices[0]):
            if idx < 0 or idx >= len(partition_records):
                self.logger.warning(f"FAISS returned invalid index {idx}")
                continue
            if similarity <= 0:
                continue

            ddhq_record = partition_records.iloc[idx]
            all_candidates.append({
                'rank': len(all_candidates) + 1,
                'similarity': float(similarity),
                'ddhq_index': ddhq_record.name,
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

        # Sort by similarity
        all_candidates.sort(key=lambda x: x['similarity'], reverse=True)

        self.logger.debug(f"Runoff date-range search: Found {len(all_candidates)} candidates (base_date={base_date}, {len(partition_records)} total records in range)")

        return (all_candidates[:k], 'date_range_runoff', str(base_date))

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
    
    async def _validate_with_llm(self, hubspot_record: Dict, similar_candidates: List[Dict], match_source: str = 'direct', fallback_date: str = None) -> Dict[str, Any]:
        """Use LLM to validate and select the best match (retries handled by LLM client)"""

        hubspot_info = {
            'name': self._get_candidate_name(hubspot_record),
            'full_name': hubspot_record.get('full_name', 'N/A'),
            'state': hubspot_record.get('state', 'N/A'),
            'city': hubspot_record.get('city', 'N/A'),
            'office': hubspot_record.get('official_office_name', hubspot_record.get('candidate_office', 'N/A')),
            'embedding_text': hubspot_record.get('embedding_name_race_text', 'N/A'),
            'election_type': hubspot_record.get('election_type', 'N/A'),
            'election_date': hubspot_record.get('election_date', 'N/A')
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

        # Build prompt using cached Braintrust template (1 API call at init, local builds thereafter)
        prompt = self._build_prompt(hubspot_info, candidates_text)
        llm_validation_start = time.time()

        try:
            # Use dedicated thread pool for maximum LLM concurrency
            # Retries are handled internally by Gemini3Client (max_retries=9)
            llm_call_start = time.time()

            # Hard timeout to prevent infinite hangs (even if HTTP client stalls)
            async def _llm_call():
                return await asyncio.get_event_loop().run_in_executor(
                    self.thread_pool,
                    lambda: self.llm_client.generate_content(prompt, trace_name="hubspot-ddhq-match")
                )

            try:
                response = await asyncio.wait_for(_llm_call(), timeout=120.0)  # 120s hard limit
            except asyncio.TimeoutError:
                self.logger.warning(f"LLM call timed out after 120s for {hubspot_record['first_name']} {hubspot_record['last_name']}")
                return {
                    'best_match': None,
                    'confidence': 0,
                    'reasoning': 'LLM call timed out after 120 seconds'
                }

            llm_call_duration = time.time() - llm_call_start

            # Handle None response from API failures
            if response is None:
                raise ValueError("LLM returned None response - likely API failure after all retries")

            response_text = response.strip()
            original_response = response_text

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

            # Fix double braces that LLM sometimes adds (mimicking the prompt's escaped braces)
            if response_text.startswith('{{') and response_text.endswith('}}'):
                response_text = response_text[1:-1].strip()

            # Try direct parsing first - most LLM responses are valid JSON
            try:
                result = json.loads(response_text)
            except json.JSONDecodeError:
                # Only if direct parsing fails, try minimal cleaning
                # Remove trailing commas and try again
                import re
                cleaned_text = re.sub(r',(\s*[}\]])', r'\1', response_text)
                result = json.loads(cleaned_text)

            # Store prompt and response for training data
            result['_llm_prompt'] = prompt
            result['_llm_response'] = original_response

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
            self.logger.debug(f"🤖 LLM validation for {candidate_name}: Total={llm_validation_total:.3f}s | Call={llm_call_duration:.3f}s")

            return result

        except Exception as e:
            # Enhanced error logging with context
            error_context = {
                'candidate_name': self._get_candidate_name(hubspot_record),
                'candidate_state': hubspot_record.get('state', 'N/A'),
                'candidate_office': hubspot_record.get('candidate_office', 'N/A'),
                'error_type': type(e).__name__,
                'error_message': str(e),
                'response_length': len(response_text) if 'response_text' in locals() else 0,
                'response_preview': response_text[:200] + '...' if 'response_text' in locals() and len(response_text) > 200 else response_text if 'response_text' in locals() else 'No response captured'
            }

            # Log failure (retries already handled by LLM client)
            self.logger.error(f"LLM validation failed after all retries:")
            self.logger.error(f"  Candidate: {error_context['candidate_name']} ({error_context['candidate_state']}) - {error_context['candidate_office']}")
            self.logger.error(f"  Error Type: {error_context['error_type']}")
            self.logger.error(f"  Error Message: {error_context['error_message']}")
            self.logger.error(f"  Response Length: {error_context['response_length']} chars")
            self.logger.error(f"  Response Preview: {error_context['response_preview']}")

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
                    self.logger.error(f"  Full Response (first 1000 chars): {repr(response_text[:1000])}")

            return {
                "best_match": None,
                "confidence": 0,
                "reasoning": f"LLM validation error after all retries: {error_context['error_type']}: {error_context['error_message']}",
                "_llm_prompt": prompt if 'prompt' in locals() else 'N/A',
                "_llm_response": original_response if 'original_response' in locals() else 'N/A'
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

            # Step 1: Date-partitioned FAISS similarity search with runoff fallback
            faiss_start_time = time.time()
            similar_candidates, match_source, fallback_date = self._search_similar_candidates_with_runoff_fallback(hubspot_record.to_dict(), k=10)
            faiss_duration = time.time() - faiss_start_time

            # Step 2: LLM validation
            llm_start_time = time.time()
            llm_result = await self._validate_with_llm(hubspot_record.to_dict(), similar_candidates, match_source, fallback_date)
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
                'llm_prompt': llm_result.get('_llm_prompt', 'N/A'),
                'llm_response': llm_result.get('_llm_response', 'N/A'),

                # Runoff fallback tracking
                'match_source': match_source,
                'runoff_fallback_date': fallback_date,

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
        # Use semaphore wrapper to prevent OOM and respect concurrency limits
        tasks = [self._process_hubspot_record_with_semaphore(row, idx) for idx, (_, row) in enumerate(batch_df.iterrows())]
        
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
            self.logger.info(f"   🎲 Using random sample of {len(records_to_process):,} records (seed=123 for reproducibility)")
        else:
            records_to_process = self.hubspot_df
        
        self.logger.info(f"\n🚀 Processing {len(records_to_process):,} HubSpot records with HIGH THROUGHPUT CONCURRENCY...")
        self.logger.info(f"   Target: 10,000 records/minute = 140 records/second (MATCHED SPAWN RATE)")
        self.logger.info(f"   Workers: {self.max_workers} (designed for EXTREME 10k/min throughput)")
        
        # CONCURRENT PROCESSING: Launch all tasks, semaphore limits concurrency
        self.logger.info(f"   🔥 Launching {len(records_to_process):,} concurrent tasks (semaphore limits to {self.max_workers})...")

        # Pre-allocate results array
        start_time = time.time()
        results = [None] * len(records_to_process)

        # Create ALL tasks at once - semaphore controls actual concurrency
        all_tasks = []
        for idx, (_, row) in enumerate(records_to_process.iterrows()):
            task = self._process_hubspot_record_with_semaphore(row, idx)
            all_tasks.append(task)

        self.logger.info(f"   ⚡ Executing {len(all_tasks):,} tasks with max {self.max_workers} concurrent...")

        # Execute all tasks concurrently (gather preserves order)
        # The semaphore inside each task limits actual concurrency
        results = await asyncio.gather(*all_tasks, return_exceptions=True)

        elapsed = time.time() - start_time
        rate = len(results) / elapsed if elapsed > 0 else 0
        self.logger.info(f"   ✅ Completed {len(results):,} tasks in {elapsed:.1f}s ({rate:.1f}/sec)")

        unprocessed_indices = [i for i, r in enumerate(results) if r is None or (isinstance(r, Exception) and 'timeout' in str(r).lower())]

        successfully_retried = 0
        still_failed = 0

        if unprocessed_indices:
            self.logger.info(f"\n🔄 RETRY: Found {len(unprocessed_indices)} unprocessed/timed-out rows")
            self.logger.info(f"   Retrying indices: {unprocessed_indices[:10]}{'...' if len(unprocessed_indices) > 10 else ''}")

            total_retries = len(unprocessed_indices)
            group_size = min(200, self.max_workers)
            self.logger.info(f"   Using batch size: {group_size} (parallel processing)")

            retry_tasks = []
            for idx in unprocessed_indices:
                row = records_to_process.iloc[idx]
                # CRITICAL: Use semaphore wrapper to prevent retry concurrency explosion
                task = self._process_hubspot_record_with_semaphore(row, idx)
                retry_tasks.append((idx, task))

            quota_exhausted = False

            retry_results = []
            with tqdm(total=total_retries, desc="Retrying failed tasks", unit="record") as retry_pbar:
                for i in range(0, len(retry_tasks), group_size):
                    if quota_exhausted:
                        for j in range(i, len(retry_tasks)):
                            idx, _ = retry_tasks[j]
                            results[idx] = Exception("Quota exhausted during retry - skipped")
                            retry_pbar.update(1)
                        break

                    group = retry_tasks[i:i + group_size]
                    group_indices = [idx for idx, _ in group]
                    group_tasks = [task for _, task in group]

                    group_results = await asyncio.gather(*group_tasks, return_exceptions=True)

                    for result_idx, (idx, result) in enumerate(zip(group_indices, group_results)):
                        if isinstance(result, Exception):
                            error_str = str(result).lower()

                            if "quota exhausted" in error_str or "resource_exhausted" in error_str:
                                self.logger.error(f"🚨 Quota exhausted during retry - stopping further processing")
                                quota_exhausted = True
                                results[idx] = result
                                still_failed += 1
                            else:
                                self.logger.error(f"❌ Error retrying index {idx}: {result}")
                                results[idx] = result
                                still_failed += 1
                        else:
                            results[idx] = result
                            if result.get('has_match'):
                                successfully_retried += 1
                            elif 'Processing error' in str(result.get('llm_reasoning', '')):
                                still_failed += 1
                            else:
                                successfully_retried += 1

                        retry_pbar.update(1)
                        self.logger.info(f"🔍 DEBUG A: After retry_pbar.update({idx})")

                    self.logger.info(f"🔍 DEBUG B: Exited inner for loop (processed {len(group_indices)} results)")

                    if quota_exhausted:
                        self.logger.info("🔍 DEBUG C: Quota exhausted, breaking")
                        break

                self.logger.info(f"🔍 DEBUG D: Exited outer for loop (processed {len(retry_tasks)} retry batches)")

            self.logger.info("🔍 DEBUG E: About to exit tqdm context manager")

        self.logger.info("🔍 DEBUG F: Successfully exited tqdm context manager")

        self.logger.info(f"   ✅ Retry processing completed: {successfully_retried} successful, {still_failed} failed")
        self.logger.info("🔍 DEBUG G: Printed retry completion message")

        total_duration = time.time() - start_time
        self.logger.info(f"🔍 DEBUG H: Starting results processing loop ({len(results)} results)")

        # Process results incrementally to avoid memory issues with large DataFrame creation
        CHUNK_SIZE = 10000
        processed_dfs = []
        temp_rows = []
        total_rows_count = 0

        for i, result in enumerate(results):
            # Log progress every 10000 records
            if i > 0 and i % CHUNK_SIZE == 0:
                self.logger.info(f"🔍 DEBUG I: Processing result {i}/{len(results)}")

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
                temp_rows.append(error_result)
            else:
                temp_rows.append(result)

            # Create DataFrame chunk every CHUNK_SIZE records
            if len(temp_rows) >= CHUNK_SIZE:
                chunk_df = pd.DataFrame(temp_rows)
                processed_dfs.append(chunk_df)
                total_rows_count += len(temp_rows)
                self.logger.info(f"🔍 DEBUG I-chunk: Created DataFrame chunk {len(processed_dfs)} with {len(temp_rows)} rows (total: {total_rows_count})")
                temp_rows = []

        # Handle remaining rows
        if temp_rows:
            chunk_df = pd.DataFrame(temp_rows)
            processed_dfs.append(chunk_df)
            total_rows_count += len(temp_rows)
            self.logger.info(f"🔍 DEBUG I-final: Created final DataFrame chunk with {len(temp_rows)} rows (total: {total_rows_count})")

        self.logger.info(f"🔍 DEBUG J: Completed results processing loop ({total_rows_count} rows in {len(processed_dfs)} chunks)")

        # Calculate final performance metrics
        total_records = total_rows_count
        records_per_second = total_records / max(0.1, total_duration)
        records_per_minute = records_per_second * 60
        target_achievement = (records_per_minute / 10000) * 100  # 10k/min target

        self.logger.info(f"🔍 DEBUG K: About to print performance results ({total_records} records)")

        self.logger.info(f"\n🎯 PERFORMANCE RESULTS:")
        self.logger.info(f"   Total time: {total_duration:.1f}s")
        self.logger.info(f"   Records processed: {total_records:,}")
        self.logger.info(f"   Speed: {records_per_second:.1f} rec/sec = {records_per_minute:.0f} rec/min")
        self.logger.info(f"   Target achievement: {target_achievement:.1f}% of 10k/min goal")
        self.logger.info(f"   Matches found: {self.matched_count:,} ({self.matched_count/self.processed_count*100:.1f}%)")
        self.logger.info(f"   Total LLM cost: ${self.total_cost:.2f}")

        self.logger.info(f"\n♻️  LAZY-LOADING STATISTICS:")
        self.logger.info(f"   Partitions built: {self.partition_build_count}")
        self.logger.info(f"   Empty partitions (cached): {len(self.empty_partitions)}")
        self.logger.info(f"   Partitions GC'd: {self.partition_gc_count}")
        self.logger.info(f"   Peak partitions in memory: {self.partition_build_count - self.partition_gc_count}")
        self.logger.info(f"   Memory efficiency: {self.partition_gc_count / max(1, self.partition_build_count) * 100:.1f}% of partitions freed")

        self.logger.info(f"🔍 DEBUG L: About to concatenate {len(processed_dfs)} DataFrame chunks into final result")
        result_df = pd.concat(processed_dfs, ignore_index=True)
        self.logger.info(f"🔍 DEBUG M: Successfully created final DataFrame with shape {result_df.shape}")

        return result_df
    
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
            combined_df.to_parquet(intermediate_file, index=False, engine='pyarrow', coerce_timestamps='us')
            self.logger.debug(f"Saved intermediate results: {len(combined_df)} records to {intermediate_file}")
        except Exception as e:
            self.logger.error(f"Failed to save intermediate results: {str(e)}")
    
    def _convert_timestamps_to_compatible_format(self, df: pd.DataFrame) -> pd.DataFrame:
        """Convert nanosecond timestamps to microsecond precision for Parquet compatibility"""
        df = df.copy()
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                # Convert datetime64[ns] to datetime64[us] (microseconds)
                df[col] = df[col].astype('datetime64[us]')
        return df

    def save_results(self, results_df: pd.DataFrame) -> str:
        """Save final results to parquet and TSV files in output folder"""

        current_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(current_dir, "output")

        # Create output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)

        # Convert timestamps to compatible format (fix nanosecond precision issue)
        results_df = self._convert_timestamps_to_compatible_format(results_df)

        # File names (only latest versions)
        latest_parquet_filename = "parallel_hubspot_ddhq_matches_latest.parquet"
        latest_tsv_filename = "parallel_hubspot_ddhq_matches_latest.tsv"

        # File paths
        latest_parquet_file = os.path.join(output_dir, latest_parquet_filename)
        latest_tsv_file = os.path.join(output_dir, latest_tsv_filename)

        # Save latest versions only
        results_df.to_parquet(latest_parquet_file, index=False, engine='pyarrow', coerce_timestamps='us')
        results_df.to_csv(latest_tsv_file, sep='\t', index=False)

        # Calculate file sizes
        parquet_size_mb = os.path.getsize(latest_parquet_file) / (1024 * 1024)
        tsv_size_mb = os.path.getsize(latest_tsv_file) / (1024 * 1024)

        # Calculate match statistics
        total_records = len(results_df)
        local_matches = results_df['has_match'].sum()
        federal_races = (results_df['ddhq_race_name'] == 'FEDERAL_RACE').sum()
        state_races = (results_df['ddhq_race_name'] == 'STATE_RACE').sum()
        no_match = total_records - local_matches - federal_races - state_races

        # Partition usage statistics
        direct_matches = (results_df['match_source'] == 'direct').sum() if 'match_source' in results_df.columns else 0
        runoff_date_range = (results_df['match_source'] == 'date_range_runoff').sum() if 'match_source' in results_df.columns else 0
        stateless_fallback = (results_df['match_source'] == 'stateless_fallback').sum() if 'match_source' in results_df.columns else 0

        self.logger.info(f"\n💾 Results saved to output folder:")
        self.logger.info(f"   Parquet: {latest_parquet_file} ({parquet_size_mb:.1f} MB)")
        self.logger.info(f"   TSV: {latest_tsv_file} ({tsv_size_mb:.1f} MB)")
        self.logger.info(f"\n📊 MATCH STATISTICS:")
        self.logger.info(f"   Total records: {total_records:,}")
        self.logger.info(f"   Local/municipal matches: {local_matches:,} ({local_matches/total_records*100:.1f}%)")
        self.logger.info(f"   Federal races (pre-filtered): {federal_races:,} ({federal_races/total_records*100:.1f}%)")
        self.logger.info(f"   State races (pre-filtered): {state_races:,} ({state_races/total_records*100:.1f}%)")
        self.logger.info(f"   No match: {no_match:,} ({no_match/total_records*100:.1f}%)")

        if runoff_date_range > 0 or stateless_fallback > 0:
            self.logger.info(f"\n🔧 PARTITION USAGE STATISTICS:")
            if direct_matches > 0:
                self.logger.info(f"   Standard partitions (date+state+type): {direct_matches:,}")
            if runoff_date_range > 0:
                self.logger.info(f"   Runoff date-range partitions (date >= base): {runoff_date_range:,}")
            if stateless_fallback > 0:
                self.logger.info(f"   State-less fallback partitions (missing state): {stateless_fallback:,}")
                self.logger.info(f"   (State-less partitions aggregate all states for date+election_type)")

        if local_matches > 0:
            matched_df = results_df[results_df['has_match'] == True]
            avg_confidence = matched_df['llm_confidence'].mean()
            self.logger.info(f"\n   Average confidence (local matches): {avg_confidence:.1f}%")

        self.logger.info(f"\n   Latest files also saved for easy access")

        return latest_parquet_file
    
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

        matcher.logger.info(f"🔍 DEBUG N: Returned from process_all_records() with DataFrame shape {results_df.shape}")

        # Save results
        results_file = matcher.save_results(results_df)

        matcher.logger.info(f"\n🎉 Parallel matching complete! Results saved to {results_file}")

        # Force immediate exit - all work is done, files saved
        matcher.logger.info("💥 Forcing immediate exit to allow S3 upload to proceed...")
        os._exit(0)

    except Exception as e:
        matcher.logger.error(f"❌ Pipeline failed: {e}")
        matcher.logger.error(f"Stack trace:", exc_info=True)
        os._exit(1)

if __name__ == "__main__":
    asyncio.run(main())