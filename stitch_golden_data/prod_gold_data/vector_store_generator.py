import os
import pandas as pd
import numpy as np
import pickle
import asyncio
from typing import List, Dict, Optional, Set
from dataclasses import dataclass
from shared.databricks_client import DatabricksClient
from shared.llm_gemini import GeminiEmbeddingClient
from shared.logger import get_logger

@dataclass
class StateProcessingResult:
    state: str
    total_districts: int
    embeddings_created: int
    cost: float
    processing_time: float
    success: bool
    error_message: Optional[str] = None

class VectorStoreGenerator:
    """Generate and manage vector stores for all 50 states + DC systematically"""
    
    def __init__(self, catalog="goodparty_data_catalog", l2_table="l2_districts"):
        self.logger = get_logger(__name__)
        self.databricks = DatabricksClient()
        self.embedding_client = GeminiEmbeddingClient()
        
        self.catalog = catalog
        self.l2_table = l2_table
        self.l2_table_path = f"{catalog}.sandbox.{l2_table}"
        
        # Get the prod_gold_data directory (where this file is located)
        current_file_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Use prod_gold_data vector store, but shared offline_data
        parent_dir = os.path.dirname(current_file_dir)  # stitch_golden_data
        self.offline_data_dir = os.path.join(parent_dir, "offline_data")
        self.vector_store_dir = os.path.join(current_file_dir, "vector_store")
        
        os.makedirs(self.offline_data_dir, exist_ok=True)
        os.makedirs(self.vector_store_dir, exist_ok=True)
        
        # US state codes + DC for systematic processing
        self.us_states = {
            'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'DC', 'FL', 'GA',
            'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
            'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
            'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
            'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY'
        }

    def get_existing_vector_stores(self) -> Set[str]:
        """Get list of states that already have vector stores"""
        existing_states = set()
        for filename in os.listdir(self.vector_store_dir):
            if filename.startswith("l2_embeddings_") and filename.endswith(".pkl"):
                state = filename.replace("l2_embeddings_", "").replace(".pkl", "").lower()
                existing_states.add(state.upper())
        return existing_states

    def load_state_data(self, state: str, force_reload: bool = False) -> Optional[pd.DataFrame]:
        """Load L2 data for a specific state, using cache when possible"""
        state_lower = state.lower()
        cache_file = os.path.join(self.offline_data_dir, f"l2_districts_{state_lower}.parquet")
        
        if not force_reload and os.path.exists(cache_file):
            df = pd.read_parquet(cache_file)
            self.logger.info(f"📁 Loaded cached {state} L2 data: {len(df):,} rows")
            return df
        
        try:
            self.logger.info(f"🔄 Downloading {state} L2 data from Databricks...")
            query = f"SELECT * FROM {self.l2_table_path} WHERE state = '{state}'"
            df = self.databricks.execute_query(query)
            
            if df.empty:
                self.logger.warning(f"⚠️ No data found for state {state}")
                return None
            
            df.to_parquet(cache_file, index=False)
            self.logger.info(f"💾 {state} L2 data saved: {len(df):,} rows")
            return df
            
        except Exception as e:
            self.logger.error(f"❌ Error loading {state} data: {e}")
            return None

    def create_embedding_texts(self, df: pd.DataFrame, state: str) -> List[Dict]:
        """Create embedding texts and metadata for a state's L2 data"""
        texts = []
        metadata = []
        
        for _, row in df.iterrows():
            district_name = str(row['district_name'])
            district_type = str(row['district_type'])
            
            # Use single, well-structured format for embeddings
            text = f"state: {state}, district type: {district_type}, district name: {district_name}"
            texts.append(text)
            metadata.append({
                'district_name': row['district_name'],
                'district_type': row['district_type'],
                'state': row['state']
            })
        
        return texts, metadata

    async def generate_state_embeddings(self, state: str, batch_size: int = 100, force_regenerate: bool = False) -> StateProcessingResult:
        """Generate embeddings for a single state"""
        import time
        start_time = time.time()
        
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"PROCESSING STATE: {state}")
        self.logger.info(f"{'='*60}")
        
        try:
            # Check if already exists
            vector_file = os.path.join(self.vector_store_dir, f"l2_embeddings_{state.lower()}.pkl")
            if not force_regenerate and os.path.exists(vector_file):
                self.logger.info(f"✅ {state} vector store already exists, skipping")
                return StateProcessingResult(
                    state=state,
                    total_districts=0,
                    embeddings_created=0,
                    cost=0.0,
                    processing_time=time.time() - start_time,
                    success=True
                )
            
            # Load state data
            df = self.load_state_data(state)
            if df is None or df.empty:
                return StateProcessingResult(
                    state=state,
                    total_districts=0,
                    embeddings_created=0,
                    cost=0.0,
                    processing_time=time.time() - start_time,
                    success=False,
                    error_message="No data available"
                )
            
            # Create embedding texts
            self.logger.info(f"📝 Creating embedding texts for {len(df):,} {state} districts...")
            texts, metadata = self.create_embedding_texts(df, state)
            
            # Generate embeddings
            self.logger.info(f"🔄 Generating embeddings (batch_size={batch_size})...")
            initial_cost = self.embedding_client.get_cost_stats()['total_cost']
            
            embeddings = await asyncio.to_thread(
                self.embedding_client.create_embeddings,
                texts,
                parallel=True,
                batch_size=batch_size,
                max_concurrent_batches=2,
                rate_limit_delay=2.0
            )
            
            final_cost = self.embedding_client.get_cost_stats()['total_cost']
            state_cost = final_cost - initial_cost
            
            # Save vector store
            self.save_state_vector_store(state, embeddings, texts, metadata)
            
            processing_time = time.time() - start_time
            self.logger.info(f"✅ {state} completed in {processing_time:.1f}s, cost: ${state_cost:.6f}")
            
            return StateProcessingResult(
                state=state,
                total_districts=len(df),
                embeddings_created=len(embeddings),
                cost=state_cost,
                processing_time=processing_time,
                success=True
            )
            
        except Exception as e:
            self.logger.error(f"❌ Error processing {state}: {e}")
            return StateProcessingResult(
                state=state,
                total_districts=0,
                embeddings_created=0,
                cost=0.0,
                processing_time=time.time() - start_time,
                success=False,
                error_message=str(e)
            )

    def save_state_vector_store(self, state: str, embeddings: np.ndarray, texts: List[str], metadata: List[Dict]):
        """Save state vector store with standardized naming"""
        filename = f"l2_embeddings_{state.lower()}.pkl"
        filepath = os.path.join(self.vector_store_dir, filename)
        
        data = {
            'state': state,
            'embeddings': embeddings,
            'texts': texts,
            'metadata': metadata,
            'created_at': pd.Timestamp.now().isoformat(),
            'total_districts': len(embeddings)
        }
        
        with open(filepath, 'wb') as f:
            pickle.dump(data, f)
        
        self.logger.info(f"💾 {state} vector store saved: {filepath}")

    async def generate_all_states(self, batch_size: int = 100, force_regenerate: bool = False, states_to_process: Optional[List[str]] = None):
        """Generate vector stores for all 50 states + DC"""
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"GENERATING VECTOR STORES FOR ALL US STATES + DC")
        self.logger.info(f"{'='*80}")
        
        # Determine which states to process
        if states_to_process:
            states = [s.upper() for s in states_to_process if s.upper() in self.us_states]
        else:
            states = sorted(list(self.us_states))
        
        if not force_regenerate:
            existing = self.get_existing_vector_stores()
            states = [s for s in states if s not in existing]
            self.logger.info(f"📁 Found {len(existing)} existing vector stores")
        
        self.logger.info(f"🎯 Processing {len(states)} states: {', '.join(states)}")
        
        if not states:
            self.logger.info("✅ All vector stores already exist!")
            return []
        
        results = []
        total_cost = 0.0
        total_districts = 0
        
        for i, state in enumerate(states, 1):
            self.logger.info(f"\n🏛️ Processing state {i}/{len(states)}: {state}")
            
            result = await self.generate_state_embeddings(state, batch_size, force_regenerate)
            results.append(result)
            
            if result.success:
                total_cost += result.cost
                total_districts += result.total_districts
                self.logger.info(f"💰 Running total: ${total_cost:.6f} ({total_districts:,} districts)")
            
            # Brief pause between states to be respectful to APIs
            if i < len(states):
                await asyncio.sleep(1)
        
        # Print final summary
        self.print_generation_summary(results, total_cost, total_districts)
        return results

    def print_generation_summary(self, results: List[StateProcessingResult], total_cost: float, total_districts: int):
        """Print comprehensive summary of vector store generation"""
        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"VECTOR STORE GENERATION SUMMARY")
        self.logger.info(f"{'='*80}")
        self.logger.info(f"Total states processed: {len(results)}")
        self.logger.info(f"Successful: {len(successful)}")
        self.logger.info(f"Failed: {len(failed)}")
        self.logger.info(f"Total districts embedded: {total_districts:,}")
        self.logger.info(f"Total cost: ${total_cost:.6f}")
        
        if successful:
            avg_cost_per_state = total_cost / len(successful)
            avg_districts_per_state = total_districts / len(successful)
            self.logger.info(f"Average cost per state: ${avg_cost_per_state:.6f}")
            self.logger.info(f"Average districts per state: {avg_districts_per_state:.0f}")
        
        if failed:
            self.logger.info(f"\n❌ Failed states:")
            for result in failed:
                self.logger.info(f"  - {result.state}: {result.error_message}")
        
        # Show existing vector stores
        existing = self.get_existing_vector_stores()
        self.logger.info(f"\n📁 Total vector stores available: {len(existing)}")
        self.logger.info(f"States: {', '.join(sorted(existing))}")

    def list_vector_stores(self):
        """List all available vector stores with metadata"""
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"AVAILABLE VECTOR STORES")
        self.logger.info(f"{'='*80}")
        
        vector_files = [f for f in os.listdir(self.vector_store_dir) if f.startswith("l2_embeddings_") and f.endswith(".pkl")]
        
        if not vector_files:
            self.logger.info("No vector stores found.")
            return
        
        total_districts = 0
        for filename in sorted(vector_files):
            filepath = os.path.join(self.vector_store_dir, filename)
            try:
                with open(filepath, 'rb') as f:
                    data = pickle.load(f)
                    state = data.get('state', filename.replace('l2_embeddings_', '').replace('.pkl', '').upper())
                    districts = data.get('total_districts', len(data.get('embeddings', [])))
                    created_at = data.get('created_at', 'Unknown')
                    
                    self.logger.info(f"  {state}: {districts:,} districts (created: {created_at})")
                    total_districts += districts
            except Exception as e:
                self.logger.error(f"  {filename}: Error reading file - {e}")
        
        self.logger.info(f"\nTotal districts across all states: {total_districts:,}")

async def main():
    """Generate vector stores for all US states + DC"""
    generator = VectorStoreGenerator()
    
    # List existing vector stores
    generator.list_vector_stores()
    
    # Generate vector stores for all states (skip existing ones)
    results = await generator.generate_all_states(
        batch_size=100,
        force_regenerate=False  # Set to True to regenerate all
    )

if __name__ == "__main__":
    asyncio.run(main())