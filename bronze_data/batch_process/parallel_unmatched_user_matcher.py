#!/usr/bin/env python3

"""
High-throughput parallel matching for unmatched Good Party users against L2 voter data.
Uses vector embeddings + LLM validation for political position matching.
"""

import os
import sys
import pandas as pd
import numpy as np
import pickle
import asyncio
import time
from datetime import datetime
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

# Add parent directory to path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), '../../..'))

from shared.llm_gemini import GeminiClient, GeminiModelType, GeminiEmbeddingClient
from shared.logger import get_logger

@dataclass
class EmbeddingMatch:
    l2_district_name: str
    l2_district_type: str
    similarity_score: float
    l2_full_text: str
    state: str

@dataclass
class LLMValidation:
    selected_district_name: str
    selected_district_type: str
    selection_confidence: float
    selection_reasoning: str
    is_matched: bool

class ParallelUnmatchedUserMatcher:
    """High-performance matcher for unmatched Good Party users against L2 voter data"""
    
    def __init__(self, max_workers: int = 1500, batch_size: int = 1000):
        self.logger = get_logger(__name__)
        self.max_workers = max_workers
        self.batch_size = batch_size
        
        print(f"🚀 PARALLEL MATCHER: {max_workers} workers, batch {batch_size}")
        
        # Initialize LLM clients with maximum throughput settings
        self._init_llm_clients()
        
        # Load unmatched user data
        self._load_unmatched_user_data()
        
        # Find and verify vector stores
        self._discover_vector_stores()
        
        # Progress tracking
        self.processed_count = 0
        self.matched_count = 0
        self.total_cost = 0.0
        
        self.thread_pool = ThreadPoolExecutor(max_workers=self.max_workers)
        
        
        print(f"✅ Ready to process {len(self.user_df):,} users")
    
    def _init_llm_clients(self):
        """Initialize LLM clients with maximum throughput settings"""
        target_concurrency = 1200
        self.llm_client = GeminiClient(
            default_model=GeminiModelType.FLASH,
            default_temperature=0.0,
            max_connections=target_concurrency,
            max_keepalive_connections=target_concurrency // 4  # 300 keepalive
        )
        
        # Standard embedding client settings
        self.embedding_client = GeminiEmbeddingClient(
            max_retries=9, 
            base_delay=1.0
        )
        
        print(f"   LLM initialized with {target_concurrency} max connections (DDHQ proven settings - 10k/min target)")
        print(f"   Embedding client configured with DDHQ proven retry settings")
    
    def _load_unmatched_user_data(self):
        """Load the unmatched user data with seriousness assessment filtering"""
        print("📥 Loading unmatched user data with seriousness assessment...")
        
        current_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(current_dir, 'output')
                
        # First try to load seriousness assessment results (look for accepted candidates file first)
        seriousness_files = []
        
        # Look for preprocessing files (new format)
        if os.path.exists(output_dir):
            # First try preprocessing accepted files  
            accepted_files = [f for f in os.listdir(output_dir) if f.startswith('preprocessing_full_dataset') and f.endswith('_accepted.parquet')]
            seriousness_files.extend([os.path.join(output_dir, f) for f in accepted_files])
            
            # Fallback to old candidate_seriousness format
            if not seriousness_files:
                old_accepted_files = [f for f in os.listdir(output_dir) if f.startswith('candidate_seriousness_full_dataset') and f.endswith('_accepted.parquet')]
                seriousness_files.extend([os.path.join(output_dir, f) for f in old_accepted_files])
        
        # Fallback to complete preprocessing files
        if not seriousness_files and os.path.exists(output_dir):
            complete_files = [f for f in os.listdir(output_dir) if f.startswith('preprocessing_full_dataset') and f.endswith('.parquet') and not f.endswith('_accepted.parquet') and not f.endswith('_rejected.parquet')]
            seriousness_files.extend([os.path.join(output_dir, f) for f in complete_files])
            
            # Fallback to old format
            if not seriousness_files:
                old_complete_files = [f for f in os.listdir(output_dir) if f.startswith('candidate_seriousness_full_dataset') and f.endswith('.parquet') and not f.endswith('_accepted.parquet') and not f.endswith('_rejected.parquet')]
                seriousness_files.extend([os.path.join(output_dir, f) for f in old_complete_files])
        
        # Use the most recent file
        seriousness_file = sorted(seriousness_files)[-1] if seriousness_files else os.path.join(output_dir, 'candidate_seriousness_full_dataset_20250904.parquet')
        
        if os.path.exists(seriousness_file):
            print(f"   🎯 Loading preprocessing results: {os.path.basename(seriousness_file)}")
            self.user_df = pd.read_parquet(seriousness_file)
            
            # Handle both pre-filtered accepted files and complete files
            if '_accepted.parquet' not in seriousness_file:
                # Apply filtering on complete preprocessing files
                original_count = len(self.user_df)
                
                if 'preprocessing_category' in self.user_df.columns:
                    self.user_df = self.user_df[self.user_df['preprocessing_category'] == 'Matchable']
                elif 'seriousness_category' in self.user_df.columns:
                    self.user_df = self.user_df[self.user_df['seriousness_category'].isin(['Serious', 'Questionable'])]
                
                filtered_count = original_count - len(self.user_df)
                if filtered_count > 0:
                    print(f"   ✅ Filtered to {len(self.user_df):,} candidates ({filtered_count:,} removed)")
            
            print(f"   📊 Clean candidates loaded: {len(self.user_df):,}")
            
        else:
            print(f"   ⚠️ Preprocessing assessment file not found: {seriousness_file}")
            print("   📁 Falling back to raw motivated users data...")
            
            # Fallback to raw unmatched user data
            data_files = []
            
            # Check current directory first
            data_files = [f for f in os.listdir(current_dir) if f.startswith('motivated_unmatched_users_') and f.endswith('.parquet')]
            
            # If not found, check offline_data subdirectory
            offline_data_dir = os.path.join(current_dir, 'offline_data')
            if not data_files and os.path.exists(offline_data_dir):
                data_files = [f for f in os.listdir(offline_data_dir) if f.startswith('motivated_unmatched_users_') and f.endswith('.parquet')]
                current_dir = offline_data_dir
            
            if not data_files:
                raise FileNotFoundError("No unmatched user data files found")
            
            # Use the most recent file
            data_file = sorted(data_files)[-1]
            data_path = os.path.join(current_dir, data_file)
            
            print(f"   📄 Loading raw data: {data_file}")
            self.user_df = pd.read_parquet(data_path)
        
        # Data quality checks
        total_records = len(self.user_df)
        with_state = (self.user_df['state'] != '').sum()
        with_office = (self.user_df['office'] != '').sum()  
        with_district = (self.user_df['district'] != '').sum()
        
        print(f"   Total records: {total_records:,}")
        print(f"   With state: {with_state:,} ({with_state/total_records*100:.1f}%)")
        print(f"   With office: {with_office:,} ({with_office/total_records*100:.1f}%)")
        print(f"   With district: {with_district:,} ({with_district/total_records*100:.1f}%)")
        
    
    def _discover_vector_stores(self):
        """Find available L2 vector stores"""
        print("🔍 Discovering L2 vector stores...")
        
        # Look for vector stores in the prod_gold_data directory
        current_dir = os.path.dirname(os.path.abspath(__file__))
        vector_store_dir = os.path.join(current_dir, '../../stitch_golden_data/prod_gold_data/vector_store')
        
        if not os.path.exists(vector_store_dir):
            raise FileNotFoundError(f"Vector store directory not found: {vector_store_dir}")
        
        self.vector_store_dir = vector_store_dir
        self.vector_store_cache = {}
        
        # Find available states
        available_states = []
        for filename in os.listdir(vector_store_dir):
            if filename.startswith("l2_embeddings_") and filename.endswith(".pkl"):
                state = filename.replace("l2_embeddings_", "").replace(".pkl", "").upper()
                available_states.append(state)
        
        self.available_states = sorted(available_states)
        print(f"   Found vector stores for {len(self.available_states)} states: {', '.join(self.available_states[:10])}{'...' if len(self.available_states) > 10 else ''}")
        
        # Check which user states have vector stores
        user_states = set(self.user_df['state'].unique())
        missing_states = user_states - set(self.available_states)
        if missing_states:
            print(f"   ⚠️ Missing vector stores for user states: {', '.join(sorted(missing_states))}")
    
    def _load_vector_store(self, state: str) -> Optional[Dict]:
        """Load vector store for a specific state with caching"""
        state_upper = state.upper()
        
        if state_upper in self.vector_store_cache:
            return self.vector_store_cache[state_upper]
        
        filename = f"l2_embeddings_{state.lower()}.pkl"
        filepath = os.path.join(self.vector_store_dir, filename)
        
        if not os.path.exists(filepath):
            return None
        
        try:
            with open(filepath, 'rb') as f:
                data = pickle.load(f)
                self.vector_store_cache[state_upper] = data
                return data
        except Exception as e:
            self.logger.error(f"❌ Error loading {state} vector store: {e}")
            return None
    
    def _extract_location_data(self, row: pd.Series) -> dict:
        """Extract location data with preprocessing fallbacks"""
        def get_field(field, fallback_field=None):
            value = str(row.get(field, '')).strip()
            if not value and fallback_field and row.get(fallback_field):
                value = str(row.get(fallback_field, '')).strip()
            return value if value.lower() not in ['none', ''] else ''
        
        return {
            'office': get_field('office'),
            'state': get_field('state', 'preprocessing_derived_state'),
            'city': get_field('city', 'preprocessing_derived_city'),
            'county': get_field('county', 'preprocessing_derived_county'),
            'district': get_field('district')
        }
    
    def _analyze_office_type(self, office: str, city: str) -> dict:
        """Analyze office type and return classification"""
        office_lower = office.lower()
        return {
            'is_municipal': any(term in office_lower for term in ['council', 'mayor', 'alderman', 'commissioner']),
            'is_federal': any(term in office_lower for term in ['house', 'senate', 'congress']),
            'has_city': city and city.lower() not in ['none', '', 'n/a']
        }
    
    def _create_embedding_query(self, row: pd.Series) -> str:
        """Create embedding query text from user record"""
        data = self._extract_location_data(row)
        query_parts = []
        
        if data['office']:
            is_municipal = any(term in data['office'].lower() for term in ['council', 'mayor', 'alderman', 'commissioner'])
            if is_municipal and data['city']:
                query_parts.append(f"district type: {data['office']}, district name: {data['city']}")
            else:
                query_parts.append(f"district type: {data['office']}")
        
        for field, label in [('city', 'district name'), ('county', 'county'), ('district', 'district')]:
            if data[field]:
                query_parts.append(f"{label}: {data[field]}")
        
        return " | ".join(query_parts) if query_parts else f"position in {data['state']}"
    
    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Calculate cosine similarity between two vectors"""
        return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))
    
    async def get_embeddings_for_queries(self, queries: List[str]) -> List[np.ndarray]:
        """Get embeddings for multiple queries using ULTRA-AGGRESSIVE parallel processing"""
        if not queries:
            return []
        
        # Balanced parallel processing to avoid rate limiting
        embeddings = await asyncio.get_event_loop().run_in_executor(
            self.thread_pool,
            lambda: self.embedding_client.create_embeddings(
                queries,
                parallel=True,
                batch_size=25,  # Back to reasonable size
                max_concurrent_batches=10,  # Reduced from 30 to avoid throttling
                rate_limit_delay=0.05  # Increased to give APIs breathing room
            )
        )
        
        return [embeddings[i] for i in range(len(queries))]
    
    async def get_top_embedding_matches(self, user_record: Dict, top_k: int = 13) -> List[EmbeddingMatch]:
        """Get top embedding matches for a user record with geographic boosting"""
        state = user_record.get('state', '').strip()
        
        if not state or state not in self.available_states:
            return []
        
        # Load vector store for user's state
        vector_store = self._load_vector_store(state)
        if not vector_store:
            return []
        
        embeddings = vector_store['embeddings']
        metadata = vector_store['metadata']
        texts = vector_store['texts']
        
        # Create embedding queries - separate user query and generic state fallback
        user_query = self._create_embedding_query(pd.Series(user_record))
        state_query = "state"  # Generic state fallback (matches production pattern)
        
        # Get embeddings
        query_embeddings = await self.get_embeddings_for_queries([user_query, state_query])
        if not query_embeddings or len(query_embeddings) < 2:
            return []
        
        user_embedding = query_embeddings[0]
        state_embedding = query_embeddings[1]
        
        # Get user's city with preprocessing fallback
        data = self._extract_location_data(pd.Series(user_record))
        user_city = data['city'].upper() if data['city'] else ''
        
        # Calculate similarities with geographic boosting
        user_similarities = []
        for i, l2_embedding in enumerate(embeddings):
            base_similarity = self.cosine_similarity(user_embedding, l2_embedding)
            
            # Geographic boost for city match
            geo_boost = 0.0
            if user_city and user_city in metadata[i]['district_name'].upper():
                geo_boost = 0.15
            
            final_similarity = min(1.0, base_similarity + geo_boost)
            user_similarities.append((final_similarity, i, geo_boost > 0))
        
        # Sort by enhanced similarity
        user_similarities.sort(reverse=True)
        user_results = user_similarities[:top_k]
        
        # Create match objects
        matches = []
        user_indices = set()
        geo_boosted_count = 0
        
        for similarity_score, idx, was_boosted in user_results:
            user_indices.add(idx)
            meta = metadata[idx]
            match = EmbeddingMatch(
                l2_district_name=meta['district_name'],
                l2_district_type=meta['district_type'], 
                similarity_score=similarity_score,
                l2_full_text=texts[idx],
                state=meta['state']
            )
            matches.append(match)
            if was_boosted:
                geo_boosted_count += 1
        
        
        # Add state fallback as 11th result if we have enough matches
        if len(matches) >= 11:
            state_similarities = []
            for i, l2_embedding in enumerate(embeddings):
                if i not in user_indices:  # Don't duplicate
                    similarity = self.cosine_similarity(state_embedding, l2_embedding)
                    state_similarities.append((similarity, i))
            
            if state_similarities:
                state_similarities.sort(reverse=True)
                state_sim, state_idx = state_similarities[0]
                
                state_meta = metadata[state_idx]
                state_match = EmbeddingMatch(
                    l2_district_name=state_meta['district_name'],
                    l2_district_type=state_meta['district_type'],
                    similarity_score=state_sim,
                    l2_full_text=texts[state_idx],
                    state=state_meta['state']
                )
                
                # Insert as 11th result
                matches.insert(10, state_match)
                matches = matches[:top_k]
        
        return matches
    
    async def validate_with_llm(self, user_record: Dict, matches: List[EmbeddingMatch]) -> Optional[LLMValidation]:
        """Use LLM to validate and select best match"""
        if not matches:
            return None
        
        # Get candidate info
        candidate_name = user_record.get('candidate_name', 'Unknown')
        data = self._extract_location_data(pd.Series(user_record))
        
        # Format embedding matches
        match_descriptions = []
        for i, match in enumerate(matches, 1):
            match_descriptions.append(
                f"{i}. {match.l2_district_name} ({match.l2_district_type})"
            )
        
        matches_text = "\n".join(match_descriptions)
        
        # Office type analysis
        office_info = self._analyze_office_type(data['office'], data['city'])
        is_generic_municipal = office_info['is_municipal'] and not office_info['has_city']
        
        # Build prompt for political position matching
        prompt = f"""Match Good Party candidate to best L2 voter district.

Candidate: {candidate_name} - {data['office']} in {data['city'] or 'Not specified'}, {data['state']}

L2 District Options:
{matches_text}

Rules:
- Municipal offices (mayor, council) need city match. Be conservative if no city specified.
- Federal offices (house, senate) can match state-level districts.
- Candidate #11 is typically a generic state-level option - prefer this for federal offices.
- Geographic alignment required: same state mandatory, same city preferred.
- Confidence: 90+ perfect match, 75+ strong, 60+ good, 50+ weak, <50 reject.

{("⚠️ Generic municipal - be VERY conservative" if is_generic_municipal else "")}
{("✅ Federal office - consider state districts" if office_info['is_federal'] else "")}

Return JSON with selected_candidate_number (1-{len(matches)} or 0 for no match), confidence (0-100), and reasoning.
When in doubt, REJECT (return 0)."""

        response_schema = {
            "type": "object",
            "properties": {
                "selected_candidate_number": {"type": "number", "minimum": 0, "maximum": len(matches)},
                "confidence": {"type": "number", "minimum": 0, "maximum": 100},
                "reasoning": {"type": "string"}
            },
            "required": ["selected_candidate_number", "confidence", "reasoning"]
        }
        
        # LLM call with retry (9 attempts for reliability)
        for attempt in range(9):
            try:
                response = await asyncio.get_event_loop().run_in_executor(
                    self.thread_pool,
                    lambda: self.llm_client.generate_structured_content(
                        prompt=prompt,
                        response_schema=response_schema,
                        model=GeminiModelType.FLASH,
                        thinking_budget=200,
                        temperature=0.0
                    )
                )
                break
            except Exception as e:
                if attempt < 8:
                    await asyncio.sleep(1.0 * (2 ** attempt))
                else:
                    return LLMValidation("LLM_ERROR", "LLM_ERROR", 0.0, f"LLM failed after 9 attempts: {e}", False)
        
        # Process LLM response
        try:
            selected_number = int(float(response["selected_candidate_number"]))
            confidence = float(response["confidence"])
            reasoning = response["reasoning"]
            
            # Validate bounds
            if selected_number < 0 or selected_number > len(matches):
                selected_number = 0
                
        except (ValueError, TypeError, KeyError) as e:
            self.logger.warning(f"Invalid LLM response for {candidate_name}: {e}. Defaulting to no match.")
            selected_number = 0
            confidence = 0.0
            reasoning = f"Invalid LLM response: {str(e)}"
        
        # Handle no match case - be more conservative for municipal positions
        office_check = self._analyze_office_type(data['office'], data['city'])
        is_generic_municipal_check = office_check['is_municipal'] and not office_check['has_city']
        
        min_confidence_threshold = 70 if is_generic_municipal_check else 60
        if selected_number == 0 or confidence < min_confidence_threshold:
            return LLMValidation(
                selected_district_name="NOT_MATCHED",
                selected_district_type="NOT_MATCHED",
                selection_confidence=confidence,
                selection_reasoning=reasoning,
                is_matched=False
            )
        
        # Get selected match
        selected_match = matches[selected_number - 1]
        
        return LLMValidation(
            selected_district_name=selected_match.l2_district_name,
            selected_district_type=selected_match.l2_district_type,
            selection_confidence=confidence,
            selection_reasoning=reasoning,
            is_matched=True
        )
    
    async def process_user_record(self, user_record: pd.Series, record_index: int) -> Dict[str, Any]:
        """Process a single user record through the matching pipeline"""
        record_start_time = time.time()
        
        try:
            user_dict = user_record.to_dict()
            candidate_name = user_dict.get('candidate_name', 'Unknown')
            
            # Step 1: Generate embedding query
            embedding_query = self._create_embedding_query(user_record)
            
            # Step 2: Get embedding matches
            embedding_start = time.time()
            embedding_matches = await self.get_top_embedding_matches(user_dict, top_k=13)
            embedding_duration = time.time() - embedding_start
            
            # Step 3: LLM validation
            llm_start = time.time()
            llm_validation = await self.validate_with_llm(user_dict, embedding_matches)
            llm_duration = time.time() - llm_start
            
            # Prepare result with original user data plus matching results
            result = user_dict.copy()  # Keep all original columns
            result.update({
                # Matching metadata
                'embedding_query': embedding_query,
                'vector_store_available': len(embedding_matches) > 0,
                
                # Match results
                'l2_district_name': llm_validation.selected_district_name if llm_validation else 'NO_MATCHES',
                'l2_district_type': llm_validation.selected_district_type if llm_validation else 'NO_MATCHES',
                'is_matched': llm_validation.is_matched if llm_validation else False,
                'match_confidence': llm_validation.selection_confidence if llm_validation else 0.0,
                'match_reasoning': llm_validation.selection_reasoning if llm_validation else 'No embedding matches found',
                
                # Top embedding matches (for analysis)
                'top_similarity_score': embedding_matches[0].similarity_score if embedding_matches else 0.0,
                'embedding_matches_count': len(embedding_matches),
                
                # Processing metadata
                'processed_at': datetime.now().isoformat(),
                'processing_duration_seconds': time.time() - record_start_time
            })
            
            if llm_validation and llm_validation.is_matched:
                self.matched_count += 1
            
            self.processed_count += 1
            
            # Log progress
            total_duration = time.time() - record_start_time
            self.logger.debug(f"⏱️  Record {record_index} ({candidate_name}): {total_duration:.3f}s | Embedding: {embedding_duration:.3f}s | LLM: {llm_duration:.3f}s | Matched: {result['is_matched']}")
            
            return result
            
        except Exception as e:
            self.logger.error(f"❌ Error processing record {record_index}: {str(e)}")
            
            # Error result preserving original data
            error_result = user_record.to_dict()
            error_result.update({
                'embedding_query': 'ERROR',
                'vector_store_available': False,
                'l2_district_name': 'ERROR',
                'l2_district_type': 'ERROR', 
                'is_matched': False,
                'match_confidence': 0.0,
                'match_reasoning': f'Processing error: {str(e)}',
                'top_similarity_score': 0.0,
                'embedding_matches_count': 0,
                'processed_at': datetime.now().isoformat(),
                'processing_duration_seconds': time.time() - record_start_time
            })
            return error_result
    
    async def process_batch(self, batch_df: pd.DataFrame, batch_num: int, total_batches: int) -> pd.DataFrame:
        """Process a batch of user records with ULTRA-AGGRESSIVE parallelism"""
        batch_size = len(batch_df)
        
        progress_pct = (batch_num / total_batches * 100) if total_batches > 0 else 0
        self.logger.info(f"🔥 ULTRA Batch {batch_num}/{total_batches} ({progress_pct:.1f}%) - Processing {batch_size} users")
        
        # Create ALL tasks at once (DDHQ matcher approach)
        tasks = [self.process_user_record(row, idx) for idx, (_, row) in enumerate(batch_df.iterrows())]
        
        start_time = time.time()
        
        # ULTRA-AGGRESSIVE: Process all tasks concurrently (no grouping limits)
        # This matches the DDHQ matcher's approach of maximum concurrency
        self.logger.info(f"   ⚡ Launching {len(tasks):,} fully concurrent tasks...")
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        duration = time.time() - start_time
        
        # Process results and handle exceptions
        processed_rows = []
        batch_matches = 0
        
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # Create error record from original data
                row = batch_df.iloc[i]
                error_result = row.to_dict()
                error_result.update({
                    'embedding_query': 'BATCH_ERROR',
                    'l2_district_name': 'BATCH_ERROR',
                    'l2_district_type': 'BATCH_ERROR',
                    'is_matched': False,
                    'match_confidence': 0.0,
                    'match_reasoning': f'Batch processing error: {str(result)}',
                    'top_similarity_score': 0.0,
                    'embedding_matches_count': 0,
                    'processed_at': datetime.now().isoformat()
                })
                processed_rows.append(error_result)
            else:
                processed_rows.append(result)
                if result.get('is_matched', False):
                    batch_matches += 1
        
        # Performance metrics
        if batch_size > 0:
            records_per_second = batch_size / max(0.1, duration)
            batch_match_rate = (batch_matches / len(processed_rows) * 100) if processed_rows else 0
            
            # Update cost tracking
            llm_stats = self.llm_client.get_usage_stats()
            embedding_stats = self.embedding_client.get_cost_stats()
            self.total_cost = llm_stats.get('total_cost', 0.0) + embedding_stats.get('total_cost', 0.0)
            
            self.logger.info(f"✅ Batch {batch_num}/{total_batches} completed ({progress_pct:.1f}%)")
            self.logger.info(f"   📊 {batch_size} users in {duration:.1f}s ({records_per_second:.1f} users/sec)")
            self.logger.info(f"   🎯 Batch matches: {batch_matches}/{len(processed_rows)} ({batch_match_rate:.1f}%)")
            self.logger.info(f"   💰 Running cost: ${self.total_cost:.6f}")
        
        return pd.DataFrame(processed_rows)
    
    async def process_all_users(self) -> pd.DataFrame:
        """Process all user records with high concurrency"""
        print(f"\n🚀 Processing {len(self.user_df):,} unmatched users...")
        
        all_result_dfs = []
        total_batches = (len(self.user_df) + self.batch_size - 1) // self.batch_size
        
        for batch_start in range(0, len(self.user_df), self.batch_size):
            batch_end = min(batch_start + self.batch_size, len(self.user_df))
            batch_df = self.user_df.iloc[batch_start:batch_end]
            batch_num = (batch_start // self.batch_size) + 1
            
            batch_result_df = await self.process_batch(batch_df, batch_num, total_batches)
            all_result_dfs.append(batch_result_df)
        
        # Combine all results
        final_results_df = pd.concat(all_result_dfs, ignore_index=True)
        
        # Calculate final metrics
        total_matches = final_results_df['is_matched'].sum()
        match_rate = (total_matches / len(final_results_df) * 100) if len(final_results_df) > 0 else 0
        avg_confidence = final_results_df[final_results_df['is_matched']]['match_confidence'].mean() if total_matches > 0 else 0
        
        print(f"\n🎯 PROCESSING COMPLETE:")
        print(f"   Total users processed: {len(final_results_df):,}")
        print(f"   Successful matches: {total_matches:,} ({match_rate:.1f}%)")
        print(f"   Average match confidence: {avg_confidence:.1f}%")
        print(f"   Total cost: ${self.total_cost:.2f}")
        
        return final_results_df
    
    def save_results(self, results_df: pd.DataFrame) -> str:
        """Save results to parquet file"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(current_dir, "output")
        os.makedirs(output_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"unmatched_users_l2_matches_{timestamp}.parquet"
        output_path = os.path.join(output_dir, output_filename)
        
        # Save with all columns
        results_df.to_parquet(output_path, index=False)
        
        file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        
        print(f"\n💾 Results saved:")
        print(f"   File: {output_path}")
        print(f"   Size: {file_size_mb:.1f} MB")
        print(f"   Records: {len(results_df):,}")
        print(f"   Matches: {results_df['is_matched'].sum():,}")
        
        return output_path
    
    def cleanup(self):
        """Cleanup resources"""
        if hasattr(self, 'thread_pool'):
            self.thread_pool.shutdown(wait=True)

async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Parallel Unmatched User L2 Matcher")
    parser.add_argument('--max-workers', '-w', type=int, default=1500,
                       help='Maximum parallel workers (default: 1500 - DDHQ proven setting)')
    parser.add_argument('--batch-size', '-b', type=int, default=1000,
                       help='Batch size for processing (default: 1000 - DDHQ proven setting)')
    parser.add_argument('--limit', '-l', type=int,
                       help='Limit number of records to process (for testing)')
    
    args = parser.parse_args()
    
    matcher = ParallelUnmatchedUserMatcher(
        max_workers=args.max_workers,
        batch_size=args.batch_size
    )
    
    try:
        # Optionally limit records for testing
        if args.limit:
            matcher.user_df = matcher.user_df.head(args.limit)
            print(f"🧪 Testing mode: Processing first {len(matcher.user_df):,} records")
        
        # Process all users
        results_df = await matcher.process_all_users()
        
        # Save results
        output_path = matcher.save_results(results_df)
        
        print(f"\n🎉 Matching complete! Results saved to {output_path}")
        
    finally:
        matcher.cleanup()

if __name__ == "__main__":
    asyncio.run(main())