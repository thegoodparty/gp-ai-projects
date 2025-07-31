import os
import pandas as pd
import numpy as np
from typing import List, Dict, Optional
import json
import pickle
import asyncio
from dataclasses import dataclass
from pydantic import BaseModel, Field
from shared.databricks_client import DatabricksClient
from shared.llm_gemini import GeminiClient, GeminiModelType, GeminiEmbeddingClient
from shared.logger import get_logger

@dataclass
class EmbeddingDistrict:
    l2_district_name: str
    l2_district_type: str
    similarity_score: float
    l2_full_text: str

@dataclass
class LLMSelection:
    selected_district_name: str
    selected_district_type: str
    selection_confidence: float
    selection_reasoning: str
    alternative_matches: Optional[List[Dict]] = None

class EmbeddingFirstLLMSecond:
    def __init__(self, catalog="goodparty_data_catalog", br_schema="dbt", br_table="int__enhanced_position", l2_table="l2_districts"):
        self.logger = get_logger(__name__)
        self.databricks = DatabricksClient()
        self.llm = GeminiClient()
        self.embedding_client = GeminiEmbeddingClient()
        
        self.catalog = catalog
        self.br_schema = br_schema
        self.br_table = br_table
        self.l2_table = l2_table
        
        self.br_table_path = f"{catalog}.{br_schema}.{br_table}"
        self.l2_table_path = f"{catalog}.sandbox.{l2_table}"
        
        # Get absolute path to stitch_golden_data directory
        current_file_dir = os.path.dirname(os.path.abspath(__file__))
        
        self.offline_data_dir = os.path.join(current_file_dir, "offline_data")
        # Check prod vector store first, then fall back to main vector store
        self.prod_vector_store_dir = os.path.join(current_file_dir, "prod_gold_data", "vector_store")
        self.fallback_vector_store_dir = os.path.join(current_file_dir, "vector_store")
        self.output_dir = os.path.join(current_file_dir, "output")
        
        os.makedirs(self.offline_data_dir, exist_ok=True)
        os.makedirs(self.prod_vector_store_dir, exist_ok=True)
        os.makedirs(self.fallback_vector_store_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        
        # Storage for embeddings - use existing LA embeddings
        self.l2_embeddings = None
        self.l2_texts = None
        self.l2_metadata = None

    def load_data(self):
        """Load Louisiana-only data from cached files or download fresh"""
        l2_file_path = os.path.join(self.offline_data_dir, "l2_districts_la.parquet")
        br_file_path = os.path.join(self.offline_data_dir, "br_sample_la.parquet")
        
        l2_df = None
        br_df = None
        
        if os.path.exists(l2_file_path):
            l2_df = pd.read_parquet(l2_file_path)
            self.logger.info(f"Loaded cached LA L2 data: {len(l2_df):,} rows")
        else:
            self.logger.info("Downloading LA L2 data...")
            query = f"SELECT * FROM {self.l2_table_path} WHERE state = 'LA'"
            l2_df = self.databricks.execute_query(query)
            l2_df.to_parquet(l2_file_path, index=False)
            self.logger.info(f"LA L2 data saved: {len(l2_df):,} rows")
        
        if os.path.exists(br_file_path):
            br_df = pd.read_parquet(br_file_path)
            self.logger.info(f"Loaded cached LA BR data: {len(br_df):,} rows")
        else:
            self.logger.info("Downloading LA BR sample...")
            query = f"""
            SELECT * FROM {self.br_table_path}
            WHERE state = 'LA'
            ORDER BY RAND()
            LIMIT 1000
            """
            br_df = self.databricks.execute_query(query)
            br_df.to_parquet(br_file_path, index=False)
            self.logger.info(f"LA BR data saved: {len(br_df):,} rows")
        
        return l2_df, br_df

    def create_l2_embedding_texts(self, l2_df: pd.DataFrame) -> List[str]:
        """Create embedding texts from LA L2 data using simplified format"""
        texts = []
        metadata = []
        
        for _, row in l2_df.iterrows():
            district_name = str(row['district_name'])
            district_type = str(row['district_type'])
            state = str(row['state'])
            
            # Use single, well-structured format for embeddings
            text = f"state: {state}, district type: {district_type}, district name: {district_name}"
            texts.append(text)
            metadata.append({
                'district_name': row['district_name'],
                'district_type': row['district_type'],
                'state': row['state']
            })
        
        self.l2_texts = texts
        self.l2_metadata = metadata
        return texts

    async def embed_texts_with_gemini(self, texts: List[str], batch_size: int = 100) -> np.ndarray:
        """Generate embeddings using GeminiEmbeddingClient with parallel processing and retry logic"""
        return await self.embedding_client.create_embeddings_parallel(
            texts, 
            batch_size=batch_size,
            max_concurrent_batches=2,  # Conservative setting to avoid 429s
            rate_limit_delay=2.0
        )

    def save_embeddings(self, embeddings: np.ndarray, filename: str = "l2_embeddings_la.pkl"):
        """Save embeddings and metadata to prod vector store"""
        filepath = os.path.join(self.prod_vector_store_dir, filename)
        data = {
            'embeddings': embeddings,
            'texts': self.l2_texts,
            'metadata': self.l2_metadata
        }
        
        with open(filepath, 'wb') as f:
            pickle.dump(data, f)
        
        self.logger.info(f"LA embeddings saved to {filepath}")

    async def embed_l2_data(self, l2_df: pd.DataFrame, force_regenerate: bool = False):
        """Create and save LA L2 embeddings"""
        if not force_regenerate and self.load_existing_embeddings():
            return
        
        self.logger.info("Creating LA L2 embedding texts...")
        texts = self.create_l2_embedding_texts(l2_df)
        
        self.logger.info("Generating embeddings...")
        embeddings = await self.embed_texts_with_gemini(texts)
        
        self.l2_embeddings = embeddings
        self.save_embeddings(embeddings)

    def load_existing_embeddings(self, force_regenerate: bool = False):
        """Load existing LA embeddings, checking prod vector store first, then fallback"""
        if force_regenerate:
            return False
            
        # Check prod vector store first
        prod_embeddings_file = os.path.join(self.prod_vector_store_dir, "l2_embeddings_la.pkl")
        fallback_embeddings_file = os.path.join(self.fallback_vector_store_dir, "l2_embeddings_la.pkl")
        
        embeddings_file = None
        if os.path.exists(prod_embeddings_file):
            embeddings_file = prod_embeddings_file
            self.logger.info("Loading LA embeddings from prod vector store...")
        elif os.path.exists(fallback_embeddings_file):
            embeddings_file = fallback_embeddings_file
            self.logger.info("Loading LA embeddings from fallback vector store...")
        
        if embeddings_file:
            with open(embeddings_file, 'rb') as f:
                data = pickle.load(f)
                self.l2_embeddings = data['embeddings']
                self.l2_texts = data['texts']
                self.l2_metadata = data['metadata']
            self.logger.info(f"Loaded {len(self.l2_embeddings)} LA embeddings from {embeddings_file}")
            return True
        else:
            self.logger.warning(f"❌ LA embeddings not found in either location")
            self.logger.info(f"   Checked: {prod_embeddings_file}")
            self.logger.info(f"   Checked: {fallback_embeddings_file}")
            self.logger.info("Will create embeddings using embed_l2_data() method")
            return False

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Calculate cosine similarity between two vectors"""
        return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

    async def get_top_embedding_matches(self, br_name: str, top_k: int = 10) -> List[EmbeddingDistrict]:
        """Step 1: Get top 10 embedding matches for BR name"""
        if self.l2_embeddings is None:
            raise ValueError("LA L2 embeddings not loaded. Call load_existing_embeddings() first.")
        
        self.logger.info(f"🔍 Step 1: Getting top {top_k} embedding matches for '{br_name}'")
        
        # Generate embedding for the BR name
        search_query = f"Louisiana political position: {br_name}"
        query_embeddings = await asyncio.to_thread(self.embedding_client.create_embeddings, [search_query], False)
        query_embedding = query_embeddings[0]
        
        # Cost is now tracked automatically by the embedding client
        embedding_stats = self.embedding_client.get_cost_stats()
        self.logger.info(f"💰 Embedding cost: ${embedding_stats['total_cost']:.6f} (total embeddings: {embedding_stats['total_embeddings_created']})")
        
        # Calculate similarities across all embeddings
        similarities = []
        for i, l2_embedding in enumerate(self.l2_embeddings):
            similarity = self.cosine_similarity(query_embedding, l2_embedding)
            similarities.append((similarity, i))
        
        # Sort by similarity and get top candidates
        similarities.sort(reverse=True)
        top_candidates = similarities[:top_k]
        
        # Create district objects
        districts = []
        for similarity_score, idx in top_candidates:
            metadata = self.l2_metadata[idx]
            district = EmbeddingDistrict(
                l2_district_name=metadata['district_name'],
                l2_district_type=metadata['district_type'],
                similarity_score=similarity_score,
                l2_full_text=self.l2_texts[idx]
            )
            districts.append(district)
        
        self.logger.info(f"📊 Got top {len(districts)} embedding matches")
        
        # Debug: Show the top 10 districts
        self.logger.info(f"\n🎯 TOP {len(districts)} EMBEDDING DISTRICTS:")
        for i, district in enumerate(districts, 1):
            self.logger.info(f"  {i:2d}. {district.l2_district_name} ({district.l2_district_type}) - Score: {district.similarity_score:.4f}")
        
        return districts

    async def llm_select_best_match(self, br_name: str, districts: List[EmbeddingDistrict], city_largest: Optional[str], county_name: Optional[str]) -> Optional[LLMSelection]:
        """Step 2: LLM Selection - Have LLM pick best match from embedding districts"""
        if not districts:
            return None

        self.logger.info(f"🤖 Step 2: LLM selection from {len(districts)} districts")
        
        # Prepare district descriptions for LLM
        district_descriptions = []
        for i, district in enumerate(districts, 1):
            district_descriptions.append(
                f"{i}. {district.l2_district_name} ({district.l2_district_type}) - Embedding Score: {district.similarity_score:.3f}"
            )
        
        districts_text = "\n".join(district_descriptions)
        
        prompt = f"""
You are analyzing a Louisiana political position to find the best match from embedding search results.

BR Position Name: "{br_name}"
State: LA
City: {city_largest or "Unknown"}
County: {county_name or "Unknown"}

Top 10 Embedding Matches (ranked by semantic similarity):
{districts_text}

Analyze the BR position name and select the BEST matching candidate from the list above. Consider:
- Semantic similarity between the BR name and L2 district names (higher embedding scores = better similarity)
- Geographic context (city/county alignment)  
- District type appropriateness for the political position
- Specific district numbers/identifiers mentioned in the BR name
- Functional role alignment (e.g., School Board positions match School Board districts)

Return JSON with:
• selected_candidate_number: The number (1-{len(districts)}) of your selected candidate, or 0 if no good match
• selection_confidence: Your confidence in this selection (0-100)
• reasoning: Detailed explanation of why this is the best match OR why no match is suitable
• close_alternatives: Array of candidate numbers that were very close runner-ups (only if top 2-3 options were neck-and-neck)

IMPORTANT: 
- If NO candidate represents a reasonable match based on your qualitative analysis, return selected_candidate_number: 0 and explain why in reasoning
- Only include close_alternatives if multiple candidates were extremely close in quality (within 5-10 confidence points)
- Base your decision on semantic meaning, geographic alignment, and functional appropriateness - not just confidence scores
"""
        
        response_schema = {
            "type": "object",
            "properties": {
                "selected_candidate_number": {"type": "number", "minimum": 0, "maximum": len(districts)},
                "selection_confidence": {"type": "number", "minimum": 0, "maximum": 100},
                "reasoning": {"type": "string"},
                "close_alternatives": {
                    "type": "array",
                    "items": {"type": "number", "minimum": 0, "maximum": len(districts)},
                    "description": "Candidate numbers that were very close runner-ups (only if neck-and-neck)"
                }
            },
            "required": ["selected_candidate_number", "selection_confidence", "reasoning"]
        }
        
        response = await asyncio.to_thread(
            self.llm.generate_structured_content,
            prompt=prompt,
            response_schema=response_schema,
            model=GeminiModelType.FLASH
        )
        
        # Cost is now tracked automatically by the LLM client
        llm_stats = self.llm.get_usage_stats()
        self.logger.info(f"💰 LLM selection cost: ${llm_stats['total_cost']:.6f} ({llm_stats['total_tokens']} tokens)")
        
        selected_number = int(response["selected_candidate_number"])
        
        if selected_number == 0:
            self.logger.warning(f"❌ LLM rejected all embedding districts: {response['reasoning']}")
            # Still return a selection object but mark it as NOT_MATCHED
            selection = LLMSelection(
                selected_district_name="NOT_MATCHED",
                selected_district_type="NOT_MATCHED",
                selection_confidence=response["selection_confidence"],
                selection_reasoning=response["reasoning"],
                alternative_matches=None
            )
            return selection
        
        # Get the selected district
        selected_district = districts[selected_number - 1]
        
        # Process close alternatives if provided
        alternative_matches = None
        if "close_alternatives" in response and response["close_alternatives"]:
            alternative_matches = []
            for alt_num in response["close_alternatives"]:
                if alt_num > 0 and alt_num <= len(districts):
                    alt_district = districts[alt_num - 1]
                    alternative_matches.append({
                        "district_name": alt_district.l2_district_name,
                        "district_type": alt_district.l2_district_type,
                        "similarity_score": alt_district.similarity_score
                    })
        
        selection = LLMSelection(
            selected_district_name=selected_district.l2_district_name,
            selected_district_type=selected_district.l2_district_type,
            selection_confidence=response["selection_confidence"],
            selection_reasoning=response["reasoning"],
            alternative_matches=alternative_matches
        )
        
        self.logger.info(f"✅ LLM selected: {selection.selected_district_name} ({selection.selected_district_type}) - {selection.selection_confidence}% confidence")
        
        return selection

    async def match_br_name(self, br_name: str, city_largest: Optional[str] = None, county_name: Optional[str] = None) -> tuple[Optional[LLMSelection], List[EmbeddingDistrict]]:
        """Complete 2-step workflow: Embedding search + LLM selection"""
        
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"PROCESSING: {br_name}")
        self.logger.info(f"{'='*80}")
        
        # Step 1: Get Top 10 Embedding Matches
        embedding_districts = await self.get_top_embedding_matches(br_name, top_k=10)
        
        # Step 2: LLM Selection
        llm_selection = await self.llm_select_best_match(br_name, embedding_districts, city_largest, county_name)
        
        return llm_selection, embedding_districts
    
    def get_cost_summary(self) -> dict:
        """Get detailed cost breakdown for the current session"""
        embedding_stats = self.embedding_client.get_cost_stats()
        llm_stats = self.llm.get_usage_stats()
        
        total_cost = embedding_stats['total_cost'] + llm_stats['total_cost']
        
        return {
            'total_cost': total_cost,
            'embedding_cost': embedding_stats['total_cost'],
            'llm_cost': llm_stats['total_cost'],
            'total_embeddings': embedding_stats['total_embeddings_created'],
            'total_tokens': llm_stats['total_tokens'],
            'total_prompt_tokens': llm_stats['total_prompt_tokens'],
            'total_completion_tokens': llm_stats['total_completion_tokens']
        }
    
    def print_cost_summary(self):
        """Print a detailed cost summary"""
        cost_summary = self.get_cost_summary()
        print(f"\n{'='*80}")
        self.logger.info(f"💰 COST SUMMARY")
        self.logger.info(f"{'='*80}")
        self.logger.info(f"Total Cost: ${cost_summary['total_cost']:.6f}")
        self.logger.info(f"  - Embedding Cost: ${cost_summary['embedding_cost']:.6f}")
        self.logger.info(f"  - LLM Cost: ${cost_summary['llm_cost']:.6f}")
        self.logger.info(f"Total Embeddings Created: {cost_summary['total_embeddings']:,}")
        self.logger.info(f"Total LLM Tokens Used: {cost_summary['total_tokens']:,}")
        self.logger.info(f"  - Prompt Tokens: {cost_summary['total_prompt_tokens']:,}")
        self.logger.info(f"  - Completion Tokens: {cost_summary['total_completion_tokens']:,}")
        
        if cost_summary['total_tokens'] > 0:
            avg_cost_per_token = cost_summary['llm_cost'] / cost_summary['total_tokens'] * 1_000_000
            self.logger.info(f"Average LLM Cost per 1M Tokens: ${avg_cost_per_token:.2f}")
        
        if cost_summary['total_embeddings'] > 0:
            avg_cost_per_embedding = cost_summary['embedding_cost'] / cost_summary['total_embeddings']
            self.logger.info(f"Average Cost per Embedding: ${avg_cost_per_embedding:.6f}")
        
        self.logger.info(f"{'='*80}")

    async def test_embedding_first_matching(self, br_df: pd.DataFrame, num_samples: int = 10, random_sample: bool = False, batch_size: int = 5):
        """Test the embedding-first approach on sample BR data"""
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"TESTING TOP-10-EMBEDDING LLM-SELECTION APPROACH ON {num_samples} LA BR RECORDS")
        self.logger.info(f"{'='*80}")
        
        if random_sample:
            sample_br = br_df.sample(n=num_samples, random_state=42) # set it to a seed number like 42 for reproducibility
            self.logger.info(f"Using random sample of {num_samples} records")
        else:
            sample_br = br_df.head(num_samples)
            self.logger.info(f"Using first {num_samples} records")
        
        results = {
            'total': 0,
            'high_confidence': 0,  # >80% LLM selection confidence
            'medium_confidence': 0,  # 60-80% LLM selection confidence
            'low_confidence': 0,  # <60% LLM selection confidence
            'no_matches': 0
        }
        
        # Store results for summary table
        match_results = []
        
        # Process records in batches for parallelization
        sample_rows = list(sample_br.iterrows())
        
        for batch_start in range(0, len(sample_rows), batch_size):
            batch_end = min(batch_start + batch_size, len(sample_rows))
            batch_rows = sample_rows[batch_start:batch_end]
            
            self.logger.info(f"\n🔄 Processing batch {batch_start//batch_size + 1}/{(len(sample_rows) + batch_size - 1)//batch_size} ({len(batch_rows)} records)")
            
            # Create async tasks for the batch
            batch_tasks = []
            for (idx, row) in batch_rows:
                br_name = row['name']
                city = row.get('city_largest')
                county = row.get('county_name')
                batch_tasks.append(self._process_single_record(br_name, city, county, idx+1))
            
            # Execute batch in parallel
            batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            
            # Process batch results
            for i, result in enumerate(batch_results):
                results['total'] += 1
                if isinstance(result, Exception):
                    self.logger.error(f"❌ Error processing record {batch_start + i + 1}: {result}")
                    match_results.append({
                        'br_name': batch_rows[i][1]['name'],
                        'picked_district_type': 'ERROR',
                        'picked_district_name': 'ERROR',
                        'confidence': 0.0,
                        'reasoning': f'Error: {str(result)}',
                        'status': 'ERROR',
                        'top_embedding_score': 0.0,
                        'top_embedding_name': 'ERROR',
                        'alternative_matches': ''
                    })
                    results['no_matches'] += 1
                else:
                    match_results.append(result)
                    if result['status'] == 'MATCHED':
                        if result['confidence'] > 80:
                            results['high_confidence'] += 1
                        elif result['confidence'] > 60:
                            results['medium_confidence'] += 1
                        else:
                            results['low_confidence'] += 1
                    else:
                        results['no_matches'] += 1
                
        
        # Print summary statistics
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"TOP-10-EMBEDDING LLM-SELECTION MATCHING SUMMARY (LOUISIANA)")
        self.logger.info(f"{'='*80}")
        self.logger.info(f"Total records: {results['total']}")
        self.logger.info(f"High confidence (>80%): {results['high_confidence']} ({results['high_confidence']/results['total']*100:.1f}%)")
        self.logger.info(f"Medium confidence (60-80%): {results['medium_confidence']} ({results['medium_confidence']/results['total']*100:.1f}%)")
        self.logger.info(f"Low confidence (<60%): {results['low_confidence']} ({results['low_confidence']/results['total']*100:.1f}%)")
        self.logger.info(f"No matches: {results['no_matches']} ({results['no_matches']/results['total']*100:.1f}%)")
        
        successful_matches = results['high_confidence'] + results['medium_confidence'] + results['low_confidence']
        success_rate = (successful_matches / results['total'] * 100) if results['total'] > 0 else 0
        self.logger.info(f"\nOverall Success Rate: {successful_matches}/{results['total']} = {success_rate:.1f}%")
        
        # Print cost summary
        self.print_cost_summary()
        
        # Print detailed results table
        self.print_results_table(match_results)
        
        return match_results
    
    def print_results_table(self, match_results: List[Dict]):
        """Print a clean results table showing BR name, picked district, confidence and reasoning"""
        self.logger.info(f"\n{'='*200}")
        self.logger.info(f"TOP-10-EMBEDDING LLM-SELECTION RESULTS TABLE")
        self.logger.info(f"{'='*200}")
        
        # Table headers
        self.logger.info(f"{'#':<3} {'BR Name':<50} {'Picked District Type':<30} {'Picked District Name':<30} {'Confidence':<12} {'Status':<10}")
        self.logger.info(f"{'-'*3} {'-'*50} {'-'*30} {'-'*30} {'-'*12} {'-'*10}")
        
        # Table rows
        for i, result in enumerate(match_results, 1):
            br_name = result['br_name']
            district_type = result['picked_district_type']
            district_name = result['picked_district_name']
            confidence = f"{result['confidence']:.0f}%" if result['confidence'] > 0 else "N/A"
            status = result['status']
            
            self.logger.info(f"{i:<3} {br_name:<50} {district_type:<30} {district_name:<30} {confidence:<12} {status:<10}")
        
        print(f"{'='*200}")
        
        # Show detailed reasoning
        self.print_reasoning_analysis(match_results)
    
    def print_reasoning_analysis(self, match_results: List[Dict]):
        """Print detailed LLM reasoning for each selection"""
        self.logger.info(f"\n{'='*120}")
        self.logger.info(f"DETAILED LLM SELECTION REASONING")
        self.logger.info(f"{'='*120}")
        
        for i, result in enumerate(match_results, 1):
            if result['status'] == 'MATCHED' and result.get('reasoning'):
                self.logger.info(f"\n{i}. {result['br_name']}")
                self.logger.info(f"   Selected: {result['picked_district_name']} ({result['picked_district_type']}) - {result['confidence']:.0f}% confidence")
                self.logger.info(f"   Reasoning: {result['reasoning']}")
                self.logger.info(f"   {'-'*100}")
            elif result['status'] == 'NO_MATCH':
                self.logger.info(f"\n{i}. {result['br_name']}")
                self.logger.info(f"   Result: NO_MATCH")
                self.logger.info(f"   Available candidates did not include a good match")
                self.logger.info(f"   {'-'*100}")
        
        print(f"{'='*120}")

    def save_results_to_csv(self, match_results: List[Dict], filename: str = "embedding_first_llm_second_results.tsv"):
        """Save match results to TSV file with cost information"""
        # Create DataFrame from results
        df = pd.DataFrame(match_results)
        
        # Add cost summary to the CSV
        cost_summary = self.get_cost_summary()
        
        # Add cost metadata as a comment at the end
        cost_info = [
            f"# Cost Summary",
            f"# Total Cost: ${cost_summary['total_cost']:.6f}",
            f"# Total Tokens: {cost_summary['total_tokens']:,}",
            f"# Prompt Tokens: {cost_summary['total_prompt_tokens']:,}",
            f"# Completion Tokens: {cost_summary['total_completion_tokens']:,}",
            f"# Records Processed: {len(df)}"
        ]
        
        # Save to output directory
        output_path = os.path.join(self.output_dir, filename)
        df.to_csv(output_path, index=False, sep='\t')
        
        # Append cost information to TSV as comments
        with open(output_path, 'a') as f:
            f.write('\n\n')
            f.write('\n'.join(cost_info))
        
        self.logger.info(f"\n📁 Results saved to: {output_path}")
        self.logger.info(f"   Columns: {list(df.columns)}")
        self.logger.info(f"   Total records: {len(df)}")
        self.logger.info(f"   💰 Total cost: ${cost_summary['total_cost']:.6f}")
        
        return output_path
    
    async def _process_single_record(self, br_name: str, city: Optional[str], county: Optional[str], record_num: int) -> Dict:
        """Process a single BR record and return result dictionary"""
        try:
            # Use 2-step workflow
            llm_selection, embedding_districts = await self.match_br_name(br_name, city, county)
            
            # Get top embedding district for comparison
            top_embedding = embedding_districts[0] if embedding_districts else None
            
            if llm_selection and llm_selection.selected_district_name != "NOT_MATCHED":
                # Format alternative matches for CSV
                alt_matches_str = ""
                if llm_selection.alternative_matches:
                    alt_list = [f"{alt['district_name']} ({alt['district_type']})" for alt in llm_selection.alternative_matches]
                    alt_matches_str = "; ".join(alt_list)
                
                return {
                    'br_name': br_name,
                    'picked_district_type': llm_selection.selected_district_type,
                    'picked_district_name': llm_selection.selected_district_name,
                    'confidence': llm_selection.selection_confidence,
                    'reasoning': llm_selection.selection_reasoning,
                    'status': 'MATCHED',
                    'top_embedding_score': top_embedding.similarity_score if top_embedding else 0.0,
                    'top_embedding_name': top_embedding.l2_district_name if top_embedding else 'N/A',
                    'alternative_matches': alt_matches_str
                }
            elif llm_selection and llm_selection.selected_district_name == "NOT_MATCHED":
                return {
                    'br_name': br_name,
                    'picked_district_type': 'NOT_MATCHED',
                    'picked_district_name': 'NOT_MATCHED',
                    'confidence': llm_selection.selection_confidence,
                    'reasoning': llm_selection.selection_reasoning,
                    'status': 'NOT_MATCHED',
                    'top_embedding_score': top_embedding.similarity_score if top_embedding else 0.0,
                    'top_embedding_name': top_embedding.l2_district_name if top_embedding else 'N/A',
                    'alternative_matches': ''
                }
            else:
                return {
                    'br_name': br_name,
                    'picked_district_type': 'ERROR',
                    'picked_district_name': 'ERROR', 
                    'confidence': 0.0,
                    'reasoning': 'No LLM response received',
                    'status': 'ERROR',
                    'top_embedding_score': top_embedding.similarity_score if top_embedding else 0.0,
                    'top_embedding_name': top_embedding.l2_district_name if top_embedding else 'N/A',
                    'alternative_matches': ''
                }
        except Exception as e:
            return {
                'br_name': br_name,
                'picked_district_type': 'ERROR',
                'picked_district_name': 'ERROR',
                'confidence': 0.0,
                'reasoning': f'Error: {str(e)}',
                'status': 'ERROR',
                'top_embedding_score': 0.0,
                'top_embedding_name': 'ERROR',
                'alternative_matches': ''
            }

async def main():
    logger = get_logger(__name__)
    matcher = EmbeddingFirstLLMSecond()
    
    # Load Louisiana-only data
    l2_df, br_df = matcher.load_data()
    
    logger.info(f"\nLouisiana Data Summary:")
    logger.info(f"L2 districts: {len(l2_df):,} rows")
    logger.info(f"BR sample: {len(br_df):,} rows")
    
    # Create or load LA embeddings
    await matcher.embed_l2_data(l2_df, force_regenerate=False)
    
    # Test the top-100-embedding LLM-selection approach with parallel processing
    logger.info(f"\n💰 Starting cost tracking...")
    match_results = await matcher.test_embedding_first_matching(
        br_df, 
        num_samples=10, 
        random_sample=True, 
        batch_size=3  # Process 3 records in parallel to avoid rate limits
    )
    
    # Save results to TSV
    csv_path = matcher.save_results_to_csv(match_results, "top10_embedding_llm_selection_100_parallel.tsv")

if __name__ == "__main__":
    asyncio.run(main())