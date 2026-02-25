"""
OPTIMIZED PRODUCTION MATCHER - HIGH THROUGHPUT VERSION

This matcher is optimized for maximum throughput with Google's 10,000 RPM rate limit.
Aggressive parallelization settings can achieve 50-100+ calls/second.

USAGE EXAMPLES:
# Standard high-throughput processing (recommended)
uv run stitch_golden_data/prod_gold_data/production_matcher.py --state CA --max-workers 1500

# Maximum speed mode for all states
uv run stitch_golden_data/prod_gold_data/production_matcher.py all_states --max-workers 1500 --max-concurrent-states 2

# Conservative mode (if experiencing rate limiting)
uv run stitch_golden_data/prod_gold_data/production_matcher.py --state DE --max-workers 150

PERFORMANCE OPTIMIZATIONS:
- 1500-thread ThreadPoolExecutor for maximum concurrent API calls
- 1200 max HTTP connections to Gemini API (300 keepalive)
- Ultra-low embedding delays (0.02s vs 1.0s default)
- 15 concurrent embedding batches vs 2 default
- 100-200 concurrent record processing vs 10 default
- Optimized retry logic for faster failure handling
"""

import os
import pandas as pd
import numpy as np
import pickle
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
from shared.databricks_client import DatabricksClient
from shared.llm_gemini_3 import Gemini3Client, GeminiModelType, ThinkingLevel
from shared.llm_gemini import GeminiEmbeddingClient
from shared.logger import get_logger
from shared.braintrust import init_braintrust, cache_prompt, build_cached_prompt, flush_logs
from tqdm.asyncio import tqdm
import time


@dataclass
class EmbeddingDistrict:
    l2_district_name: str
    l2_district_type: str
    similarity_score: float
    l2_full_text: str
    state: str

@dataclass
class LLMSelection:
    selected_district_name: str
    selected_district_type: str
    selection_confidence: float
    selection_reasoning: str
    alternative_matches: Optional[List[Dict]] = None

@dataclass
class MatchingStats:
    total_processed: int = 0
    successful_matches: int = 0
    high_confidence: int = 0
    medium_confidence: int = 0
    low_confidence: int = 0
    no_matches: int = 0
    errors: int = 0
    total_cost: float = 0.0
    embedding_cost: float = 0.0
    llm_cost: float = 0.0

class ProductionMatcher:
    """Production-ready matcher using pre-built vector stores for all states"""
    
    def __init__(self, catalog="goodparty_data_catalog", br_schema="dbt", br_table="int__enhanced_position", max_workers=1500):
        self.logger = get_logger(__name__)
        self.databricks = DatabricksClient()

        # Configure LLM client with high concurrency matching DDHQ matcher
        target_concurrency = 1200
        self.llm = Gemini3Client(
            default_model=GeminiModelType.FLASH_3,
            default_temperature=0.0,
            thinking_level=ThinkingLevel.MINIMAL,
            max_connections=target_concurrency,
            max_keepalive_connections=target_concurrency // 4,  # 300 keepalive
            max_retries=11,
            base_delay=1.0
        )
        self.embedding_client = GeminiEmbeddingClient(max_retries=11, base_delay=1.0)
        self.max_workers = max_workers
        
        self.catalog = catalog
        self.br_schema = br_schema
        self.br_table = br_table
        self.br_table_path = f"{catalog}.{br_schema}.{br_table}"
        
        # Get the prod_gold_data directory (where this file is located)
        current_file_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Use prod vector store with local offline_data and output
        self.offline_data_dir = os.path.join(current_file_dir, "offline_data")
        self.vector_store_dir = os.path.join(current_file_dir, "vector_store")
        self.output_dir = os.path.join(current_file_dir, "output")
        
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.offline_data_dir, exist_ok=True)
        
        # Cache for loaded vector stores
        self.vector_store_cache = {}
        self.stats = MatchingStats()
        
        # Create ThreadPoolExecutor for maximum concurrency
        self.thread_pool = ThreadPoolExecutor(max_workers=self.max_workers)

        init_braintrust(project="stitch-golden-data")
        self._init_prompt_cache()

    def _init_prompt_cache(self):
        self._prompt_name = "stitch-golden-data-matcher"
        prompt_obj = cache_prompt(self._prompt_name)
        if prompt_obj is not None:
            self.logger.info("Braintrust prompt cached for stitch-golden-data-matcher")
        else:
            self.logger.warning("Braintrust prompt not available, using fallback")

    def get_available_states(self) -> List[str]:
        """Get list of states with available vector stores"""
        states = []
        for filename in os.listdir(self.vector_store_dir):
            if filename.startswith("l2_embeddings_") and filename.endswith(".pkl"):
                state = filename.replace("l2_embeddings_", "").replace(".pkl", "").upper()
                states.append(state)
        return sorted(states)
    
    def get_states_by_record_count(self, ascending: bool = True) -> List[str]:
        """Get states sorted by BR record count (smallest to largest by default)"""
        try:
            # Query to get record counts per state
            query = f"""
            SELECT 
                state,
                COUNT(*) as record_count
            FROM {self.br_table_path}
            GROUP BY state
            ORDER BY record_count {'ASC' if ascending else 'DESC'}
            """
            
            df = self.databricks.execute_query(query)
            if df.empty:
                self.logger.warning("No data returned from BR table, falling back to alphabetical order")
                return self.get_available_states()
            
            # Filter to only include states with vector stores
            available_states = set(self.get_available_states())
            
            # Return states in record count order, filtered to available ones
            ordered_states = []
            for _, row in df.iterrows():
                state = row['state']
                if state in available_states:
                    ordered_states.append(state)
                    
            self.logger.info(f"📊 Ordered {len(ordered_states)} states by record count ({'smallest first' if ascending else 'largest first'})")
            return ordered_states
            
        except Exception as e:
            self.logger.warning(f"Failed to get states by record count: {e}. Falling back to alphabetical order.")
            return self.get_available_states()

    def load_vector_store(self, state: str) -> Optional[Dict]:
        """Load vector store for a specific state with caching"""
        state_upper = state.upper()
        
        if state_upper in self.vector_store_cache:
            return self.vector_store_cache[state_upper]
        
        filename = f"l2_embeddings_{state.lower()}.pkl"
        filepath = os.path.join(self.vector_store_dir, filename)
        
        if not os.path.exists(filepath):
            self.logger.warning(f"⚠️ Vector store not found for state {state}: {filepath}")
            return None
        
        try:
            with open(filepath, 'rb') as f:
                data = pickle.load(f)
                self.vector_store_cache[state_upper] = data
                self.logger.info(f"📁 Loaded {state} vector store: {len(data['embeddings']):,} embeddings")
                return data
        except Exception as e:
            self.logger.error(f"❌ Error loading {state} vector store: {e}")
            return None

    def load_br_data(self, states: Optional[List[str]] = None, limit: Optional[int] = None, sample_fraction: Optional[float] = None) -> pd.DataFrame:
        """Load Ballot Ready data with all columns for comprehensive output"""
        # Create cache filename based on parameters
        if states and len(states) == 1:
            # Single state caching
            cache_filename = f"br_production_data_{states[0].lower()}.parquet"
        else:
            # Multi-state or no filter caching
            cache_filename = "br_production_data.parquet"
        cache_path = os.path.join(self.offline_data_dir, cache_filename)
        
        # Check if we already have cached data for this specific request
        if os.path.exists(cache_path):
            try:
                self.logger.info(f"📁 Loading BR data from cache: {cache_filename}")
                return pd.read_parquet(cache_path)
            except Exception as e:
                self.logger.warning(f"⚠️ Cache read failed, will query fresh: {e}")
        
        # Build query based on parameters
        where_clause = ""
        if states:
            states_str = "', '".join([s.upper() for s in states])
            where_clause = f"WHERE state IN ('{states_str}')"
        
        order_clause = ""
        limit_clause = ""
        
        if sample_fraction:
            order_clause = "ORDER BY RAND()"
            if limit:
                limit_clause = f"LIMIT {limit}"
        elif limit:
            limit_clause = f"LIMIT {limit}"
        
        # Include all BR columns for comprehensive output
        query = f"""
        SELECT *
        FROM {self.br_table_path}
        {where_clause}
        {order_clause}
        {limit_clause}
        """
        
        try:
            self.logger.info(f"🔄 Loading BR data from Databricks...")
            df = self.databricks.execute_query(query)
            
            if df.empty:
                self.logger.warning("⚠️ No BR data returned from query")
                return df
            
            # Cache for future use
            df.to_parquet(cache_path, index=False)
            self.logger.info(f"💾 BR data cached: {len(df):,} rows")
            return df
            
        except Exception as e:
            self.logger.error(f"❌ Error loading BR data: {e}")
            # Try to load from cache if available
            if os.path.exists(cache_path):
                self.logger.info("📁 Loading from cache instead...")
                return pd.read_parquet(cache_path)
            return pd.DataFrame()

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Calculate cosine similarity between two vectors"""
        return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

    async def get_embeddings_for_queries(self, queries: List[str]) -> List[np.ndarray]:
        """Get embeddings for multiple queries - process individually to ensure consistency

        CRITICAL: Gemini API returns different embeddings when batching queries together
        vs sending them individually. To ensure consistency with the vector store
        (which was built with batched embeddings), we process each query individually.
        """
        if not queries:
            return []

        # Process each query individually to avoid batch-dependent embedding variations
        # This ensures consistent results with the vector store embeddings
        embeddings = []
        for query in queries:
            embedding_result = await asyncio.get_event_loop().run_in_executor(
                self.thread_pool,
                lambda q=query: self.embedding_client.create_embeddings(
                    [q],  # Single query at a time
                    parallel=False,
                    batch_size=1
                )
            )
            embeddings.append(embedding_result[0])

        return embeddings

    async def get_top_embedding_matches(self, br_name: str, state: str, top_k: int = 13) -> List[EmbeddingDistrict]:
        """Get top embedding matches using race name and insert generic state search as 11th result"""
        vector_store = self.load_vector_store(state)
        if not vector_store:
            return []
        
        embeddings = vector_store['embeddings']
        metadata = vector_store['metadata']
        texts = vector_store['texts']
        
        # Prepare queries for embedding search - both race name and generic state search
        race_query = f"race name: {br_name}"
        state_query = "state"
        
        # Get query embeddings for both searches
        query_embeddings = await self.get_embeddings_for_queries([race_query, state_query])
        if not query_embeddings or len(query_embeddings) < 2:
            return []
            
        race_query_embedding = query_embeddings[0]
        state_query_embedding = query_embeddings[1]
        
        # Calculate similarities for race name search
        race_similarities = []
        for i, l2_embedding in enumerate(embeddings):
            similarity = self.cosine_similarity(race_query_embedding, l2_embedding)
            race_similarities.append((similarity, i))
        
        # Calculate similarities for generic state search
        state_similarities = []
        for i, l2_embedding in enumerate(embeddings):
            similarity = self.cosine_similarity(state_query_embedding, l2_embedding)
            state_similarities.append((similarity, i))
        
        # Sort race similarities and get top results
        race_similarities.sort(reverse=True)
        race_results = race_similarities[:top_k]
        
        # Sort generic state similarities and get top result
        state_similarities.sort(reverse=True)
        state_top_result = state_similarities[0] if state_similarities else None
        
        # Create district objects for race results
        districts = []
        race_indices = set()
        
        for similarity_score, idx in race_results:
            race_indices.add(idx)
            meta = metadata[idx]
            district = EmbeddingDistrict(
                l2_district_name=meta['district_name'],
                l2_district_type=meta['district_type'],
                similarity_score=similarity_score,
                l2_full_text=texts[idx],
                state=meta['state']
            )
            districts.append(district)
        
        # Insert generic state result as 11th embedding if we have enough results and it's different
        if len(districts) >= 11 and state_top_result:
            state_similarity_score, state_idx = state_top_result
            
            # Only insert if it's not already in the top results
            if state_idx not in race_indices:
                state_meta = metadata[state_idx]
                state_district = EmbeddingDistrict(
                    l2_district_name=state_meta['district_name'],
                    l2_district_type=state_meta['district_type'],
                    similarity_score=state_similarity_score,
                    l2_full_text=texts[state_idx],
                    state=state_meta['state']
                )
                
                # Insert as 11th result (index 10)
                districts.insert(10, state_district)
                
                # Keep only top_k results
                districts = districts[:top_k]
        
        return districts

    async def llm_select_best_match(self, br_name: str, districts: List[EmbeddingDistrict]) -> Optional[LLMSelection]:
        """LLM selection with enhanced context"""
        if not districts:
            return None
        
        # Prepare district descriptions
        district_descriptions = []
        for i, district in enumerate(districts, 1):
            district_descriptions.append(
                f"{i}. {district.l2_district_name} ({district.l2_district_type})"
            )
        
        districts_text = "\n".join(district_descriptions)
        state = districts[0].state if districts else "Unknown"
        num_districts = str(len(districts))

        variables = {
            "br_name": br_name,
            "state": state,
            "districts_text": districts_text,
            "num_districts": num_districts,
        }

        fallback_prompt = f"""
You are analyzing a political position to find the best L2 district match from candidate districts.

BR Position Details:
- Name: "{br_name}"
- State: {state}

Top {num_districts} District Candidates:
{districts_text}

Analyze the BR position and select the BEST matching candidate. Consider:
- Geographic alignment (city/county matching)
- Office type and district type compatibility
- Specific identifiers or numbers in names
- Functional role alignment (e.g., School Board → School Board districts)
- Ignore seats and positions
- if the office is greater than the state level, match to the state level

Return JSON with:
• selected_candidate_number: Number (1-{num_districts}) of your choice, or 0 if no good match
• selection_confidence: Confidence level (0-100)
• reasoning: Detailed explanation of your selection or rejection
• close_alternatives: Array of candidate numbers that were very close (only if multiple options were neck-and-neck)

IMPORTANT: Return 0 if no candidate represents a reasonable match.
There is a real probability that the match does not exist so return 0 if there is no clear match.

Base decisions on semantic meaning, geography, and functional appropriateness.
"""

        prompt = build_cached_prompt(self._prompt_name, variables, fallback_prompt=fallback_prompt)
        
        response_schema = {
            "type": "object",
            "properties": {
                "selected_candidate_number": {"type": "number", "minimum": 0, "maximum": len(districts)},
                "selection_confidence": {"type": "number", "minimum": 0, "maximum": 100},
                "reasoning": {"type": "string"},
                "close_alternatives": {
                    "type": "array",
                    "items": {"type": "number", "minimum": 0, "maximum": len(districts)}
                }
            },
            "required": ["selected_candidate_number", "selection_confidence", "reasoning"]
        }

        try:
            response = await asyncio.get_event_loop().run_in_executor(
                self.thread_pool,
                lambda: self.llm.generate_structured_content(
                    prompt=prompt,
                    response_schema=response_schema,
                    trace_name="stitch-match-selection"
                )
            )
        except Exception as e:
            self.logger.error(f"LLM failed for {br_name}: {e}")
            return LLMSelection(
                selected_district_name="LLM_ERROR",
                selected_district_type="LLM_ERROR",
                selection_confidence=0.0,
                selection_reasoning=f"LLM generation failed: {str(e)}",
                alternative_matches=None
            )
        
        # Handle potential float or invalid response
        try:
            selected_number = int(float(response["selected_candidate_number"]))
            # Ensure the number is within valid bounds
            if selected_number < 0 or selected_number > len(districts):
                self.logger.warning(f"Selected candidate number {selected_number} out of bounds (0-{len(districts)}). Defaulting to 0 (no match).")
                selected_number = 0
        except (ValueError, TypeError, KeyError) as e:
            self.logger.warning(f"Invalid selected_candidate_number in LLM response: {response.get('selected_candidate_number', 'missing')}. Defaulting to 0 (no match).")
            selected_number = 0
        
        if selected_number == 0:
            return LLMSelection(
                selected_district_name="NOT_MATCHED",
                selected_district_type="NOT_MATCHED",
                selection_confidence=response["selection_confidence"],
                selection_reasoning=response["reasoning"],
                alternative_matches=None
            )
        
        # Get selected district
        selected_district = districts[selected_number - 1]
        
        # Process alternatives with robust error handling
        alternative_matches = None
        if "close_alternatives" in response and response["close_alternatives"]:
            alternative_matches = []
            for alt_num in response["close_alternatives"]:
                try:
                    alt_num_int = int(float(alt_num))
                    if 0 < alt_num_int <= len(districts):
                        alt_district = districts[alt_num_int - 1]
                        alternative_matches.append({
                            "district_name": alt_district.l2_district_name,
                            "district_type": alt_district.l2_district_type,
                            "similarity_score": alt_district.similarity_score
                        })
                    else:
                        self.logger.warning(f"Alternative match number {alt_num_int} out of bounds (1-{len(districts)}). Skipping.")
                except (ValueError, TypeError) as e:
                    self.logger.warning(f"Invalid alternative match number: {alt_num}. Skipping.")
        
        return LLMSelection(
            selected_district_name=selected_district.l2_district_name,
            selected_district_type=selected_district.l2_district_type,
            selection_confidence=response["selection_confidence"],
            selection_reasoning=response["reasoning"],
            alternative_matches=alternative_matches
        )

    async def match_single_record(self, row: pd.Series) -> pd.Series:
        """Process a single BR record and return enhanced row with matching results"""
        br_name = row['name']
        state = row['state']
        
        # Create enhanced query term that was used for embedding search
        embedding_queried_term = f"race name: {br_name}"
        
        # Create a copy of the original row to preserve all BR data
        result_row = row.copy()
        result_row['embedding_queried_term'] = embedding_queried_term
        
        try:
            # Step 1: Get top embedding matches
            embedding_districts = await self.get_top_embedding_matches(br_name, state, top_k=13)
            
            if not embedding_districts:
                # No vector store available
                result_row['l2_district_name'] = 'NO_VECTOR_STORE'
                result_row['l2_district_type'] = 'NO_VECTOR_STORE'
                result_row['is_matched'] = False
                result_row['llm_reason'] = f'No vector store available for state {state}'
                result_row['confidence'] = 0.0
                result_row['top_embedding_score'] = 0.0
                result_row['embeddings'] = 'NO_VECTOR_STORE'
                result_row['alternative_matches'] = ''
                return result_row
            
            # Step 2: LLM selection
            llm_selection = await self.llm_select_best_match(br_name, embedding_districts)
            
            # Format all embedding matches (all 10)
            embedding_matches = []
            for district in embedding_districts:
                embedding_matches.append(f"{district.l2_district_type} - {district.l2_district_name} ({district.similarity_score:.3f})")
            embeddings_str = " | ".join(embedding_matches)
            
            if not llm_selection:
                # LLM error
                result_row['l2_district_name'] = 'LLM_ERROR'
                result_row['l2_district_type'] = 'LLM_ERROR'
                result_row['is_matched'] = False
                result_row['llm_reason'] = 'LLM did not return a response'
                result_row['confidence'] = 0.0
                result_row['top_embedding_score'] = embedding_districts[0].similarity_score
                result_row['embeddings'] = embeddings_str
                result_row['alternative_matches'] = ''
                return result_row
            
            # Format alternative matches
            alt_matches_str = ""
            if llm_selection.alternative_matches:
                alt_list = [f"{alt['district_name']} ({alt['district_type']})" for alt in llm_selection.alternative_matches]
                alt_matches_str = "; ".join(alt_list)
            
            # Determine if matched
            is_matched = llm_selection.selected_district_name != "NOT_MATCHED"
            
            # Add matching results to the row
            result_row['l2_district_name'] = llm_selection.selected_district_name
            result_row['l2_district_type'] = llm_selection.selected_district_type
            result_row['is_matched'] = is_matched
            result_row['llm_reason'] = llm_selection.selection_reasoning
            result_row['confidence'] = llm_selection.selection_confidence
            result_row['top_embedding_score'] = embedding_districts[0].similarity_score
            result_row['embeddings'] = embeddings_str
            result_row['alternative_matches'] = alt_matches_str
            
            return result_row
            
        except Exception as e:
            self.logger.error(f"❌ Error processing {br_name} ({state}): {e}")
            # Error case - preserve BR data, add error info
            result_row['l2_district_name'] = 'ERROR'
            result_row['l2_district_type'] = 'ERROR'
            result_row['is_matched'] = False
            result_row['llm_reason'] = f'Processing error: {str(e)}'
            result_row['confidence'] = 0.0
            result_row['top_embedding_score'] = 0.0
            result_row['embeddings'] = 'ERROR'
            result_row['alternative_matches'] = ''
            return result_row

    async def process_batch(self, batch_df: pd.DataFrame, batch_num: int, total_batches: int, state: str = None) -> pd.DataFrame:
        """Process a batch of BR records in parallel and return enhanced DataFrame"""
        batch_size = len(batch_df)
        
        # Enhanced progress logging with state information
        state_info = f" [{state}]" if state else ""
        progress_pct = (batch_num / total_batches * 100) if total_batches > 0 else 0
        self.logger.info(f"🔄 Batch {batch_num}/{total_batches}{state_info} ({progress_pct:.1f}%) - Processing {batch_size} records")
        
        # Create tasks for parallel processing - all records in batch run concurrently
        tasks = [self.match_single_record(row) for _, row in batch_df.iterrows()]
        
        # Execute all tasks in parallel with error handling
        # This enables aggressive concurrency within each batch
        start_time = time.time()
        
        # Process in larger groups for maximum throughput (matching DDHQ matcher)
        group_size = min(self.max_workers, 200)  # Optimized for 10,000 RPM throughput
        results = []
        
        for i in range(0, len(tasks), group_size):
            group_tasks = tasks[i:i + group_size]
            group_results = await asyncio.gather(*group_tasks, return_exceptions=True)
            results.extend(group_results)
        
        duration = time.time() - start_time
        
        # Process results and handle non-quota exceptions
        processed_rows = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                
                # Create error row from original data for other exceptions
                row = batch_df.iloc[i].copy()
                row['embedding_queried_term'] = f"race name: {row['name']}"
                row['l2_district_name'] = 'BATCH_ERROR'
                row['l2_district_type'] = 'BATCH_ERROR'
                row['is_matched'] = False
                row['llm_reason'] = f'Batch processing error: {str(result)}'
                row['confidence'] = 0.0
                row['top_embedding_score'] = 0.0
                row['embeddings'] = 'BATCH_ERROR'
                row['alternative_matches'] = ''
                processed_rows.append(row)
                self.stats.errors += 1
            else:
                processed_rows.append(result)
                self.update_stats_from_row(result)
        
        # Enhanced performance tracking with state context and completion stats
        if batch_size > 0:
            records_per_second = batch_size / max(0.1, duration)
            
            # Calculate completion metrics
            completed_records = batch_num * batch_size if batch_num < total_batches else len(processed_rows) + ((batch_num - 1) * batch_size)
            total_records_estimate = total_batches * batch_size
            completion_pct = (batch_num / total_batches * 100) if total_batches > 0 else 0
            
            # Count successful matches in this batch
            batch_matches = 0
            for row in processed_rows:
                if hasattr(row, 'get'):
                    # DataFrame row
                    if row.get('is_matched', False):
                        batch_matches += 1
                elif hasattr(row, 'is_matched'):
                    # Series row
                    if row.is_matched:
                        batch_matches += 1
            
            batch_match_rate = (batch_matches / len(processed_rows) * 100) if processed_rows else 0
            
            self.logger.info(f"✅ Batch {batch_num}/{total_batches}{state_info} completed ({completion_pct:.1f}%)")
            self.logger.info(f"   📊 {batch_size} records in {duration:.1f}s ({records_per_second:.1f} rec/sec)")
            self.logger.info(f"   🎯 Batch matches: {batch_matches}/{len(processed_rows)} ({batch_match_rate:.1f}%)")
            
            # Show overall progress every few batches or at completion
            if batch_num % 5 == 0 or batch_num == total_batches:
                self.logger.info(f"   📈 Overall: ~{completed_records:,}/{total_records_estimate:,} records processed")
        
        return pd.DataFrame(processed_rows)

    def update_stats_from_row(self, row: pd.Series):
        """Update processing statistics from enhanced row"""
        self.stats.total_processed += 1
        
        if row['is_matched']:
            self.stats.successful_matches += 1
            confidence = row['confidence']
            if confidence > 80:
                self.stats.high_confidence += 1
            elif confidence > 60:
                self.stats.medium_confidence += 1
            else:
                self.stats.low_confidence += 1
        elif row['l2_district_name'] in ['NOT_MATCHED', 'NO_VECTOR_STORE']:
            self.stats.no_matches += 1
        else:
            self.stats.errors += 1

    def update_cost_stats(self):
        """Update cost statistics from clients"""
        embedding_stats = self.embedding_client.get_cost_stats()
        llm_stats = self.llm.get_usage_stats()
        
        self.stats.embedding_cost = embedding_stats['total_cost']
        self.stats.llm_cost = llm_stats['total_cost']
        self.stats.total_cost = self.stats.embedding_cost + self.stats.llm_cost

    async def run_production_matching(self, states: Optional[List[str]] = None, limit: Optional[int] = None, batch_size: int = 1000, output_filename: str = "production_matching_results.tsv") -> str:
        """Run production matching on BR database"""
        
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"PRODUCTION BR-L2 MATCHING")
        self.logger.info(f"{'='*80}")
        
        # Check available vector stores
        available_states_set = set(self.get_available_states())
        self.logger.info(f"📁 Available vector stores: {len(available_states_set)} states")
        
        if states:
            missing_states = [s for s in states if s.upper() not in available_states_set]
            if missing_states:
                self.logger.warning(f"⚠️ Missing vector stores for: {', '.join(missing_states)}")
            states = [s for s in states if s.upper() in available_states_set]
        else:
            # Use natural ordering by record count (smallest first)
            states = self.get_states_by_record_count(ascending=True)
            self.logger.info(f"📊 Using natural order: smallest to largest states")
        
        if not states:
            self.logger.error("❌ No states with vector stores available")
            return ""
        
        # Load BR data
        self.logger.info(f"🎯 Processing states: {', '.join(states)}")
        br_df = self.load_br_data(states=states, limit=limit)
        
        if br_df.empty:
            self.logger.error("❌ No BR data loaded")
            return ""
        
        self.logger.info(f"📊 Loaded {len(br_df):,} BR records")
        
        # Process in batches
        all_result_dfs = []
        total_batches = (len(br_df) + batch_size - 1) // batch_size
        
        for batch_start in range(0, len(br_df), batch_size):
            batch_end = min(batch_start + batch_size, len(br_df))
            batch_df = br_df.iloc[batch_start:batch_end]
            batch_num = (batch_start // batch_size) + 1
            
            batch_result_df = await self.process_batch(batch_df, batch_num, total_batches, "Multi-State")
            all_result_dfs.append(batch_result_df)
            
            # Update and report costs periodically
            self.update_cost_stats()
            if batch_num % 10 == 0 or batch_num == total_batches:
                self.logger.info(f"💰 Running cost: ${self.stats.total_cost:.6f} ({self.stats.total_processed:,} processed)")
        
        # Combine all results into final DataFrame
        final_results_df = pd.concat(all_result_dfs, ignore_index=True)
        
        # Final cost update
        self.update_cost_stats()
        
        # Save results in both formats
        output_paths = self.save_enhanced_results(final_results_df, output_filename)
        
        # Print summary
        self.print_final_summary()
        
        return output_paths

    async def process_single_state(self, state: str, batch_size: int, output_prefix: str) -> Dict[str, Any]:
        """Process a single state with comprehensive matching and output"""
        state_filename = f"{output_prefix}_{state.lower()}"
        parquet_filename = f"{state_filename}.parquet"
        parquet_path = os.path.join(self.output_dir, parquet_filename)
        
        # Check if output already exists
        if os.path.exists(parquet_path):
            self.logger.info(f"⏭️ {state} SKIPPED - Output already exists: {parquet_filename}")
            return {
                'state': state,
                'status': 'SKIPPED',
                'output_paths': {
                    'parquet': parquet_path,
                    'tsv': parquet_path.replace('.parquet', '.tsv')
                },
                'stats': MatchingStats()
            }
        
        # Log prominent state start
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"🚀 STARTING STATE: {state}")
        self.logger.info(f"{'='*60}")
        
        # Reset stats for this state
        state_stats = MatchingStats()
        
        # Load BR data for this state only
        br_df = self.load_br_data(states=[state])
        
        if br_df.empty:
            self.logger.warning(f"⚠️ No BR data for {state}")
            return {
                'state': state,
                'status': 'NO_DATA',
                'output_paths': {},
                'stats': state_stats
            }
        
        self.logger.info(f"📊 {state}: Processing {len(br_df):,} records with batch_size={batch_size}")
        
        # Process all records for this state with batch concurrency
        all_result_dfs = []
        total_batches = (len(br_df) + batch_size - 1) // batch_size
        
        
        for batch_start in range(0, len(br_df), batch_size):
            batch_end = min(batch_start + batch_size, len(br_df))
            batch_df = br_df.iloc[batch_start:batch_end]
            batch_num = (batch_start // batch_size) + 1
            
            batch_result_df = await self.process_batch(batch_df, batch_num, total_batches, state)
            all_result_dfs.append(batch_result_df)
            
            # Update state stats from batch results
            for _, row in batch_result_df.iterrows():
                self.update_stats_from_row_to_stats(row, state_stats)
        
        # Combine results for this state
        state_results_df = pd.concat(all_result_dfs, ignore_index=True)
        
        # Final cost update for this state
        self.update_cost_stats_to_stats(state_stats)
        
        # Save state-specific results
        state_output_paths = self.save_enhanced_results_with_stats(state_results_df, state_filename, state_stats)
        
        success_rate = (state_stats.successful_matches / max(1, state_stats.total_processed) * 100)
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"✅ {state} COMPLETED")
        self.logger.info(f"   Matched: {state_stats.successful_matches:,}/{state_stats.total_processed:,} ({success_rate:.1f}%)")
        self.logger.info(f"   Cost: ${state_stats.total_cost:.6f}")
        self.logger.info(f"{'='*60}\n")
        
        return {
            'state': state,
            'status': 'COMPLETED',
            'output_paths': state_output_paths,
            'stats': state_stats
        }
    
    def update_stats_from_row_to_stats(self, row: pd.Series, stats: MatchingStats):
        """Update specific stats object from enhanced row"""
        stats.total_processed += 1
        
        if row['is_matched']:
            stats.successful_matches += 1
            confidence = row['confidence']
            if confidence > 80:
                stats.high_confidence += 1
            elif confidence > 60:
                stats.medium_confidence += 1
            else:
                stats.low_confidence += 1
        elif row['l2_district_name'] in ['NOT_MATCHED', 'NO_VECTOR_STORE']:
            stats.no_matches += 1
        else:
            stats.errors += 1
    
    def update_cost_stats_to_stats(self, stats: MatchingStats):
        """Update cost statistics to specific stats object"""
        embedding_stats = self.embedding_client.get_cost_stats()
        llm_stats = self.llm.get_usage_stats()
        
        stats.embedding_cost = embedding_stats['total_cost']
        stats.llm_cost = llm_stats['total_cost']
        stats.total_cost = stats.embedding_cost + stats.llm_cost
    
    def save_enhanced_results_with_stats(self, results_df: pd.DataFrame, base_filename: str, stats: MatchingStats) -> Dict[str, str]:
        """Save enhanced results with specific stats object and filtered columns"""
        base_name = base_filename.replace('.tsv', '').replace('.parquet', '')
        
        # File paths
        parquet_filename = f"{base_name}.parquet"
        tsv_filename = f"{base_name}.tsv"
        parquet_path = os.path.join(self.output_dir, parquet_filename)
        tsv_path = os.path.join(self.output_dir, tsv_filename)
        
        # Create copy for saving with filtered columns
        results_df = results_df.copy()
        
        # Define the specific columns to include
        desired_columns = [
            # BR columns
            'name', 'id', 'br_database_id', 'state',
            # L2 columns  
            'l2_district_name', 'l2_district_type',
            # LLM columns
            'is_matched', 'llm_reason', 'confidence', 'embeddings', 'top_embedding_score'
        ]
        
        # Filter to only include desired columns that exist in the DataFrame
        available_columns = [col for col in desired_columns if col in results_df.columns]
        filtered_df = results_df[available_columns]
        
        # Save parquet (primary format with filtered data)
        filtered_df.to_parquet(parquet_path, index=False)
        self.logger.info(f"💾 Parquet results saved: {parquet_path} ({len(available_columns)} columns)")
        
        # Save TSV with metadata comments
        filtered_df.to_csv(tsv_path, index=False, sep='\t')
        
        # Append metadata to TSV
        metadata = [
            f"\n# PRODUCTION MATCHING METADATA",
            f"# Processing Timestamp: {pd.Timestamp.now().isoformat()}",
            f"# Total Records Processed: {stats.total_processed:,}",
            f"# Successful Matches: {stats.successful_matches:,}",
            f"# High Confidence (>80%): {stats.high_confidence:,}",
            f"# Medium Confidence (60-80%): {stats.medium_confidence:,}",
            f"# Low Confidence (<60%): {stats.low_confidence:,}",
            f"# No Matches: {stats.no_matches:,}",
            f"# Errors: {stats.errors:,}",
            f"# Total Cost: ${stats.total_cost:.6f}",
            f"# Embedding Cost: ${stats.embedding_cost:.6f}",
            f"# LLM Cost: ${stats.llm_cost:.6f}",
            f"# Success Rate: {(stats.successful_matches / max(1, stats.total_processed) * 100):.1f}%",
            f"# Columns: {', '.join(available_columns)}"
        ]
        
        with open(tsv_path, 'a') as f:
            f.write('\n'.join(metadata))
        
        self.logger.info(f"💾 TSV results saved: {tsv_path} ({len(available_columns)} columns)")
        
        return {
            'parquet': parquet_path,
            'tsv': tsv_path
        }

    async def run_all_states_individual(self, batch_size: int = 1000, output_prefix: str = "state_matching", max_concurrent_states: int = 3) -> Dict[str, str]:
        """Run production matching for all available states with controlled concurrent processing"""
        
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"PRODUCTION BR-L2 MATCHING - ALL STATES CONCURRENT")
        self.logger.info(f"{'='*80}")
        
        # Get available states sorted by record count (smallest first)
        available_states = self.get_states_by_record_count(ascending=True)
        self.logger.info(f"📁 Processing {len(available_states)} states in size order (smallest first) with max {max_concurrent_states} concurrent")
        
        all_output_paths = {}
        overall_stats = MatchingStats()
        skipped_states = []
        
        # Process states with concurrency control
        semaphore = asyncio.Semaphore(max_concurrent_states)
        
        async def process_state_with_semaphore(state: str) -> Dict[str, Any]:
            async with semaphore:
                try:
                    # Add 2-hour timeout per state to prevent indefinite hangs
                    return await asyncio.wait_for(
                        self.process_single_state(state, batch_size, output_prefix),
                        timeout=3600 * 2  # 2 hours
                    )
                except asyncio.TimeoutError:
                    self.logger.error(f"⏱️ TIMEOUT: {state} exceeded 2 hours processing time - skipping")
                    raise RuntimeError(f"State {state} processing timeout after 2 hours")
                except Exception as e:
                    # Check for quota exhaustion - re-raise to stop all processing
                    error_str = str(e).lower()
                    if ("daily quota exhausted" in error_str or "resource_exhausted" in error_str):
                        self.logger.error(f"🚨 QUOTA EXHAUSTED processing {state} - stopping ALL states")
                        raise
                    else:
                        # Log other errors but don't stop processing
                        self.logger.error(f"❌ Error processing state {state}: {e}")
                        raise
        
        # Execute states in order when max_concurrent_states=1
        if max_concurrent_states == 1:
            # Sequential processing to maintain order
            with tqdm(total=len(available_states), desc="States Progress", unit="state", 
                     position=0, ncols=100, colour="blue", leave=True) as states_pbar:
                
                for state_idx, state in enumerate(available_states, 1):
                    try:
                        # Log state number prominently
                        self.logger.info(f"\n🏳️ STATE {state_idx}/{len(available_states)}: {state}")
                        
                        result = await process_state_with_semaphore(state)
                        
                        state = result['state']
                        status = result['status']
                        
                        if status == 'SKIPPED':
                            skipped_states.append(state)
                            all_output_paths[state] = result['output_paths']
                        elif status == 'COMPLETED':
                            all_output_paths[state] = result['output_paths']
                            # Aggregate stats
                            state_stats = result['stats']
                            overall_stats.total_processed += state_stats.total_processed
                            overall_stats.successful_matches += state_stats.successful_matches
                            overall_stats.high_confidence += state_stats.high_confidence
                            overall_stats.medium_confidence += state_stats.medium_confidence
                            overall_stats.low_confidence += state_stats.low_confidence
                            overall_stats.no_matches += state_stats.no_matches
                            overall_stats.errors += state_stats.errors
                            overall_stats.total_cost += state_stats.total_cost
                            overall_stats.embedding_cost += state_stats.embedding_cost
                            overall_stats.llm_cost += state_stats.llm_cost
                        
                        # Update progress bar
                        states_pbar.update(1)
                        states_pbar.set_postfix({
                            'status': status, 
                            'completed': f"{len([s for s in all_output_paths.keys() if s not in skipped_states])}/{len(available_states)}",
                            'cost': f"${overall_stats.total_cost:.3f}"
                        })
                        
                    except Exception as e:
                        # Check for quota exhaustion - stop all processing
                        error_str = str(e).lower()
                        if ("daily quota exhausted" in error_str or "resource_exhausted" in error_str):
                            self.logger.error(f"🚨 QUOTA EXHAUSTED - stopping all state processing")
                            break
                        else:
                            self.logger.error(f"❌ State processing error: {e}")
                            states_pbar.update(1)
                            continue
        else:
            # Create concurrent tasks for all states
            tasks = [process_state_with_semaphore(state) for state in available_states]
            
            # Execute all states with controlled concurrency
            with tqdm(total=len(available_states), desc="States Progress", unit="state", 
                     position=0, ncols=100, colour="blue", leave=True) as states_pbar:
                
                # Process results as they complete
                for completed_result in asyncio.as_completed(tasks):
                    try:
                        result = await completed_result
                    except Exception as e:
                        # Check for quota exhaustion - cancel all remaining tasks and exit
                        error_str = str(e).lower()
                        if ("daily quota exhausted" in error_str or "resource_exhausted" in error_str):
                            self.logger.error(f"🚨 QUOTA EXHAUSTED - cancelling all remaining state processing")
                            # Cancel all remaining tasks
                            for task in tasks:
                                if not task.done():
                                    task.cancel()
                            raise RuntimeError(f"Processing stopped due to quota exhaustion: {str(e)}")
                        else:
                            self.logger.error(f"❌ State processing error: {e}")
                            continue
                    
                    state = result['state']
                    status = result['status']
                    
                    if status == 'SKIPPED':
                        skipped_states.append(state)
                        all_output_paths[state] = result['output_paths']
                    elif status == 'COMPLETED':
                        all_output_paths[state] = result['output_paths']
                        # Aggregate stats
                        state_stats = result['stats']
                        overall_stats.total_processed += state_stats.total_processed
                        overall_stats.successful_matches += state_stats.successful_matches
                        overall_stats.high_confidence += state_stats.high_confidence
                        overall_stats.medium_confidence += state_stats.medium_confidence
                        overall_stats.low_confidence += state_stats.low_confidence
                        overall_stats.no_matches += state_stats.no_matches
                        overall_stats.errors += state_stats.errors
                        overall_stats.total_cost += state_stats.total_cost
                        overall_stats.embedding_cost += state_stats.embedding_cost
                        overall_stats.llm_cost += state_stats.llm_cost
                    
                    # Update progress bar
                    states_pbar.update(1)
                    states_pbar.set_postfix({
                        'status': status, 
                        'completed': f"{len([s for s in all_output_paths.keys() if s not in skipped_states])}/{len(available_states)}",
                        'cost': f"${overall_stats.total_cost:.3f}"
                    })
        
        # Update stats to overall totals
        self.stats = overall_stats
        
        # Print final summary
        self.print_final_all_states_summary(all_output_paths, skipped_states)
        
        return all_output_paths

    def print_final_all_states_summary(self, all_output_paths: Dict[str, str], skipped_states: List[str]):
        """Print comprehensive summary for all states processing"""
        processed_states = len(all_output_paths) - len(skipped_states)
        success_rate = (self.stats.successful_matches / max(1, self.stats.total_processed) * 100)
        
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"ALL STATES INDIVIDUAL MATCHING FINAL SUMMARY")
        self.logger.info(f"{'='*80}")
        self.logger.info(f"Total States Available: {len(all_output_paths)}")
        self.logger.info(f"States Processed This Run: {processed_states}")
        self.logger.info(f"States Skipped (already exist): {len(skipped_states)}")
        if skipped_states:
            self.logger.info(f"  Skipped: {', '.join(sorted(skipped_states))}")
        
        if self.stats.total_processed > 0:
            self.logger.info(f"\n📊 PROCESSING RESULTS (This Run Only):")
            self.logger.info(f"Total Records: {self.stats.total_processed:,}")
            self.logger.info(f"Successful Matches: {self.stats.successful_matches:,} ({success_rate:.1f}%)")
            self.logger.info(f"  - High Confidence (>80%): {self.stats.high_confidence:,}")
            self.logger.info(f"  - Medium Confidence (60-80%): {self.stats.medium_confidence:,}")
            self.logger.info(f"  - Low Confidence (<60%): {self.stats.low_confidence:,}")
            self.logger.info(f"No Matches: {self.stats.no_matches:,}")
            self.logger.info(f"Errors: {self.stats.errors:,}")
            
            self.logger.info(f"\n💰 COST BREAKDOWN (This Run Only):")
            self.logger.info(f"Total Cost: ${self.stats.total_cost:.6f}")
            self.logger.info(f"  - Embedding Cost: ${self.stats.embedding_cost:.6f}")
            self.logger.info(f"  - LLM Cost: ${self.stats.llm_cost:.6f}")
            
            cost_per_record = self.stats.total_cost / self.stats.total_processed
            self.logger.info(f"Average Cost per Record: ${cost_per_record:.6f}")
        else:
            self.logger.info(f"\n📊 No new records processed this run (all states already completed)")
        
        self.logger.info(f"\n📁 OUTPUT FILES BY STATE (Alphabetical Order):")
        for state in sorted(all_output_paths.keys()):
            paths = all_output_paths[state]
            parquet_file = paths.get('parquet', '').split('/')[-1] if paths.get('parquet') else 'N/A'
            status = "SKIPPED" if state in skipped_states else "PROCESSED"
            self.logger.info(f"  - {state}: {parquet_file} [{status}]")

    def save_enhanced_results(self, results_df: pd.DataFrame, base_filename: str) -> Dict[str, str]:
        """Save enhanced results in both parquet and TSV formats with filtered columns"""
        base_name = base_filename.replace('.tsv', '').replace('.parquet', '')
        
        # File paths
        parquet_filename = f"{base_name}.parquet"
        tsv_filename = f"{base_name}.tsv"
        parquet_path = os.path.join(self.output_dir, parquet_filename)
        tsv_path = os.path.join(self.output_dir, tsv_filename)
        
        # Create copy for saving with filtered columns
        results_df = results_df.copy()
        
        # Define the specific columns to include
        desired_columns = [
            # BR columns
            'name', 'id', 'br_database_id', 'state',
            # L2 columns  
            'l2_district_name', 'l2_district_type',
            # LLM columns
            'is_matched', 'llm_reason', 'confidence', 'embeddings', 'top_embedding_score'
        ]
        
        # Filter to only include desired columns that exist in the DataFrame
        available_columns = [col for col in desired_columns if col in results_df.columns]
        filtered_df = results_df[available_columns]
        
        # Save parquet (primary format with filtered data)
        filtered_df.to_parquet(parquet_path, index=False)
        self.logger.info(f"💾 Parquet results saved: {parquet_path} ({len(available_columns)} columns)")
        
        # Save TSV with metadata comments
        filtered_df.to_csv(tsv_path, index=False, sep='\t')
        
        # Append metadata to TSV
        metadata = [
            f"\n# PRODUCTION MATCHING METADATA",
            f"# Processing Timestamp: {pd.Timestamp.now().isoformat()}",
            f"# Total Records Processed: {self.stats.total_processed:,}",
            f"# Successful Matches: {self.stats.successful_matches:,}",
            f"# High Confidence (>80%): {self.stats.high_confidence:,}",
            f"# Medium Confidence (60-80%): {self.stats.medium_confidence:,}",
            f"# Low Confidence (<60%): {self.stats.low_confidence:,}",
            f"# No Matches: {self.stats.no_matches:,}",
            f"# Errors: {self.stats.errors:,}",
            f"# Total Cost: ${self.stats.total_cost:.6f}",
            f"# Embedding Cost: ${self.stats.embedding_cost:.6f}",
            f"# LLM Cost: ${self.stats.llm_cost:.6f}",
            f"# Success Rate: {(self.stats.successful_matches / max(1, self.stats.total_processed) * 100):.1f}%",
            f"# Columns: {', '.join(available_columns)}"
        ]
        
        with open(tsv_path, 'a') as f:
            f.write('\n'.join(metadata))
        
        self.logger.info(f"💾 TSV results saved: {tsv_path} ({len(available_columns)} columns)")
        
        # Print data summary
        self.print_data_summary(filtered_df)
        
        return {
            'parquet': parquet_path,
            'tsv': tsv_path
        }
    
    def print_data_summary(self, df: pd.DataFrame):
        """Print summary of the enhanced dataset"""
        self.logger.info(f"\n📊 ENHANCED DATASET SUMMARY")
        self.logger.info(f"{'='*60}")
        self.logger.info(f"Total records: {len(df):,}")
        self.logger.info(f"Original BR columns: {len([col for col in df.columns if not col.startswith(('l2_', 'is_matched', 'llm_', 'confidence', 'top_embedding', 'embeddings', 'embedding_queried_term', 'alternative_matches'))])}")
        self.logger.info(f"Added matching columns: {len([col for col in df.columns if col.startswith(('l2_', 'is_matched', 'llm_', 'confidence', 'top_embedding', 'embeddings', 'embedding_queried_term', 'alternative_matches'))])}")
        
        # Matching statistics from data
        matched_count = df['is_matched'].sum()
        match_rate = (matched_count / len(df)) * 100
        avg_confidence = df[df['is_matched']]['confidence'].mean() if matched_count > 0 else 0
        
        self.logger.info(f"\nMatching Results:")
        self.logger.info(f"  - Successful matches: {matched_count:,} ({match_rate:.1f}%)")
        self.logger.info(f"  - Average confidence: {avg_confidence:.1f}%")
        
        if matched_count > 0:
            confidence_breakdown = df[df['is_matched']]['confidence'].describe()
            self.logger.info(f"  - Confidence distribution:")
            self.logger.info(f"    • Min: {confidence_breakdown['min']:.1f}%")
            self.logger.info(f"    • Median: {confidence_breakdown['50%']:.1f}%") 
            self.logger.info(f"    • Max: {confidence_breakdown['max']:.1f}%")
        
        # Top states by volume
        state_counts = df['state'].value_counts().head(5)
        self.logger.info(f"\nTop states by volume:")
        for state, count in state_counts.items():
            state_matches = df[df['state'] == state]['is_matched'].sum()
            state_match_rate = (state_matches / count) * 100
            self.logger.info(f"  - {state}: {count:,} records ({state_matches:,} matched, {state_match_rate:.1f}%)")

    def print_final_summary(self):
        """Print comprehensive final summary"""
        success_rate = (self.stats.successful_matches / max(1, self.stats.total_processed) * 100)
        
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"PRODUCTION MATCHING FINAL SUMMARY")
        self.logger.info(f"{'='*80}")
        self.logger.info(f"Total Records: {self.stats.total_processed:,}")
        self.logger.info(f"Successful Matches: {self.stats.successful_matches:,} ({success_rate:.1f}%)")
        self.logger.info(f"  - High Confidence (>80%): {self.stats.high_confidence:,}")
        self.logger.info(f"  - Medium Confidence (60-80%): {self.stats.medium_confidence:,}")
        self.logger.info(f"  - Low Confidence (<60%): {self.stats.low_confidence:,}")
        self.logger.info(f"No Matches: {self.stats.no_matches:,}")
        self.logger.info(f"Errors: {self.stats.errors:,}")
        self.logger.info(f"\n💰 COST BREAKDOWN")
        self.logger.info(f"Total Cost: ${self.stats.total_cost:.6f}")
        self.logger.info(f"  - Embedding Cost: ${self.stats.embedding_cost:.6f}")
        self.logger.info(f"  - LLM Cost: ${self.stats.llm_cost:.6f}")
        
        if self.stats.total_processed > 0:
            cost_per_record = self.stats.total_cost / self.stats.total_processed
            self.logger.info(f"Average Cost per Record: ${cost_per_record:.6f}")

async def main():
    """Test production matcher with enhanced parquet output"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Production BR-L2 matching tool")
    parser.add_argument('mode', nargs='?', default='test', 
                       choices=['test', 'all_states'], 
                       help='Matching mode: test (sample), all_states (process all), or specify a state code')
    parser.add_argument('--state', '-s', type=str, 
                       help='Process a single state (e.g., DE, CA, NY)')
    parser.add_argument('--states', nargs='+', type=str,
                       help='Process multiple states (e.g., --states HI DE DC)')
    parser.add_argument('--batch-size', '-b', type=int, default=1000,
                       help='Batch size for processing (default: 1000)')
    parser.add_argument('--limit', '-l', type=int,
                       help='Limit number of records to process')
    parser.add_argument('--max-concurrent-states', '-c', type=int, default=3,
                       help='Maximum concurrent states to process (default: 3)')
    parser.add_argument('--max-workers', '-w', type=int, default=1500,
                       help='Maximum parallel workers (default: 1500)')
    
    args = parser.parse_args()
    matcher = ProductionMatcher(max_workers=args.max_workers)
    
    # Check if states arguments are provided
    if args.states:
        states = [s.upper() for s in args.states]
        available_states = matcher.get_available_states()
        
        # Check which states are available
        missing_states = [s for s in states if s not in available_states]
        if missing_states:
            print(f"❌ States not available: {', '.join(missing_states)}")
            print(f"Available states: {', '.join(sorted(available_states))}")
            return
        
        print(f"🎯 Processing multiple states: {', '.join(states)}")
        output_paths = await matcher.run_production_matching(
            states=states,
            limit=args.limit,
            batch_size=args.batch_size,
            output_filename=f"multi_state_matching_{'_'.join([s.lower() for s in states])}"
        )
        
        print(f"\n📁 Results saved for {', '.join(states)}:")
        print(f"  - Parquet: {output_paths.get('parquet', 'N/A')}")
        print(f"  - TSV: {output_paths.get('tsv', 'N/A')}")
        
    elif args.state:
        state = args.state.upper()
        available_states = matcher.get_available_states()
        
        if state not in available_states:
            print(f"❌ State '{state}' not available. Available states: {', '.join(sorted(available_states))}")
            return
        
        print(f"🎯 Processing single state: {state}")
        output_paths = await matcher.run_production_matching(
            states=[state],
            limit=args.limit,
            batch_size=args.batch_size,
            output_filename=f"state_matching_{state.lower()}"
        )
        
        print(f"\n📁 Results saved for {state}:")
        print(f"  - Parquet: {output_paths.get('parquet', 'N/A')}")
        print(f"  - TSV: {output_paths.get('tsv', 'N/A')}")
        
    elif args.mode == "all_states":
        # Run all states with controlled concurrent processing
        all_output_paths = await matcher.run_all_states_individual(
            batch_size=args.batch_size,
            output_prefix="full_state_matching",
            max_concurrent_states=args.max_concurrent_states
        )
        
        print(f"\n📁 Results available for {len(all_output_paths)} states:")
        processed_count = 0
        for state in sorted(all_output_paths.keys()):
            paths = all_output_paths[state]
            parquet_file = paths.get('parquet', '').split('/')[-1] if paths.get('parquet') else 'N/A'
            if os.path.exists(paths.get('parquet', '')):
                print(f"  - {state}: {parquet_file} ✅")
                processed_count += 1
        
        print(f"\n📊 Summary: {processed_count}/{len(all_output_paths)} states have completed output files")
            
    else:
        # Test with a small sample first
        print("Choose matching mode:")
        print("1. Test with sample states (CA, NY, TX) - all records")
        print("2. Process ALL available states individually")
        print("3. Process single state with --state argument")
        
        output_paths = await matcher.run_production_matching(
            states=['CA', 'NY', 'TX'],  # Test with a few states
            limit=args.limit,  # Use specified limit or None for all records
            batch_size=args.batch_size,
            output_filename="production_test_results"
        )
        
        print(f"\n📁 Results saved:")
        print(f"  - Parquet: {output_paths.get('parquet', 'N/A')}")
        print(f"  - TSV: {output_paths.get('tsv', 'N/A')}")
        print(f"\n💡 Load the parquet file for analysis:")
        print(f"   import pandas as pd")
        print(f"   df = pd.read_parquet('{output_paths.get('parquet', '')}')")
        print(f"   print(df[['name', 'state', 'embedding_queried_term', 'l2_district_name', 'is_matched', 'confidence']].head())")
        
        print(f"\n💡 Usage examples:")
        print(f"   # Process Delaware only (high throughput):")
        print(f"   uv run stitch_golden_data/prod_gold_data/production_matcher.py --state DE")
        print(f"   # Process all states with 2 concurrent states:")
        print(f"   uv run stitch_golden_data/prod_gold_data/production_matcher.py all_states --max-concurrent-states 2")
        print(f"   # Process Delaware with custom batch size:")
        print(f"   uv run stitch_golden_data/prod_gold_data/production_matcher.py --state DE --batch-size 1000")
        print(f"   # Process all states with maximum throughput:")
        print(f"   uv run stitch_golden_data/prod_gold_data/production_matcher.py all_states --batch-size 1000 --max-concurrent-states 2 --max-workers 1500")



if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        flush_logs()