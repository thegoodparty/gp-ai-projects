import os
import pickle
import argparse
import numpy as np
import asyncio
from typing import List, Dict, Optional
from dataclasses import dataclass
from shared.llm_gemini import GeminiEmbeddingClient
from shared.logger import get_logger

@dataclass
class EmbeddingMatch:
    district_name: str
    district_type: str
    state: str
    similarity_score: float
    full_text: str

class VectorStorePlayground:
    def __init__(self, state: str):
        self.logger = get_logger(__name__)
        self.state = state.upper()
        self.embedding_client = GeminiEmbeddingClient()
        
        current_file_dir = os.path.dirname(os.path.abspath(__file__))
        self.prod_vector_store_dir = os.path.join(current_file_dir, "prod_gold_data", "vector_store")
        self.fallback_vector_store_dir = os.path.join(current_file_dir, "vector_store")
        
        self.embeddings = None
        self.texts = None
        self.metadata = None
        
    def load_state_embeddings(self) -> bool:
        """Load embeddings for the specified state"""
        state_lower = self.state.lower()
        
        prod_file = os.path.join(self.prod_vector_store_dir, f"l2_embeddings_{state_lower}.pkl")
        fallback_file = os.path.join(self.fallback_vector_store_dir, f"l2_embeddings_{state_lower}.pkl")
        
        embeddings_file = None
        if os.path.exists(prod_file):
            embeddings_file = prod_file
            self.logger.info(f"Loading {self.state} embeddings from prod vector store...")
        elif os.path.exists(fallback_file):
            embeddings_file = fallback_file
            self.logger.info(f"Loading {self.state} embeddings from fallback vector store...")
        
        if embeddings_file:
            try:
                with open(embeddings_file, 'rb') as f:
                    data = pickle.load(f)
                    self.embeddings = data['embeddings']
                    self.texts = data['texts']
                    self.metadata = data['metadata']
                
                self.logger.info(f"✅ Loaded {len(self.embeddings)} embeddings for {self.state}")
                return True
            except Exception as e:
                self.logger.error(f"❌ Error loading embeddings: {e}")
                return False
        else:
            self.logger.error(f"❌ No embeddings found for state {self.state}")
            self.logger.info(f"   Checked: {prod_file}")
            self.logger.info(f"   Checked: {fallback_file}")
            return False
    
    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Calculate cosine similarity between two vectors"""
        return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))
    
    async def search_embeddings(self, query: str, top_k: int = 10) -> List[EmbeddingMatch]:
        """Search embeddings for the given query and return top k matches"""
        if self.embeddings is None:
            raise ValueError(f"Embeddings not loaded for state {self.state}")
        
        self.logger.info(f"🔍 Searching for: '{query}' in {self.state} embeddings")
        
        search_query = f"{self.state} political position: {query}"
        query_embeddings = await asyncio.to_thread(
            self.embedding_client.create_embeddings, 
            [search_query], 
            False
        )
        query_embedding = query_embeddings[0]
        
        embedding_stats = self.embedding_client.get_cost_stats()
        self.logger.info(f"💰 Query embedding cost: ${embedding_stats['total_cost']:.6f}")
        
        similarities = []
        for i, district_embedding in enumerate(self.embeddings):
            similarity = self.cosine_similarity(query_embedding, district_embedding)
            similarities.append((similarity, i))
        
        similarities.sort(reverse=True)
        top_matches = similarities[:top_k]
        
        results = []
        for similarity_score, idx in top_matches:
            metadata = self.metadata[idx]
            match = EmbeddingMatch(
                district_name=metadata['district_name'],
                district_type=metadata['district_type'],
                state=metadata['state'],
                similarity_score=similarity_score,
                full_text=self.texts[idx]
            )
            results.append(match)
        
        return results
    
    def display_results(self, query: str, matches: List[EmbeddingMatch]):
        """Display search results in a formatted table"""
        self.logger.info(f"\n{'='*100}")
        self.logger.info(f"TOP 10 MATCHES FOR: '{query}' in {self.state}")
        self.logger.info(f"{'='*100}")
        
        print(f"{'Rank':<4} {'Score':<8} {'District Type':<25} {'District Name':<40} {'Full Text':<30}")
        print(f"{'-'*4} {'-'*8} {'-'*25} {'-'*40} {'-'*30}")
        
        for i, match in enumerate(matches, 1):
            score_str = f"{match.similarity_score:.4f}"
            district_type = match.district_type[:24]
            district_name = match.district_name[:39]
            full_text = match.full_text[:29]
            
            print(f"{i:<4} {score_str:<8} {district_type:<25} {district_name:<40} {full_text:<30}")
        
        print(f"{'='*100}")
    
    async def interactive_mode(self):
        """Run interactive query mode"""
        self.logger.info(f"\n🎮 INTERACTIVE MODE - {self.state} Vector Store Playground")
        self.logger.info(f"{'='*60}")
        self.logger.info(f"Loaded {len(self.embeddings):,} embeddings for {self.state}")
        self.logger.info(f"Type your queries below. Type 'quit' or 'exit' to quit.")
        self.logger.info(f"{'='*60}")
        
        while True:
            try:
                query = input(f"\n🔍 Enter search query for {self.state}: ").strip()
                
                if query.lower() in ['quit', 'exit', 'q']:
                    self.logger.info("👋 Goodbye!")
                    break
                
                if not query:
                    continue
                
                matches = await self.search_embeddings(query)
                self.display_results(query, matches)
                
            except KeyboardInterrupt:
                self.logger.info("\n👋 Goodbye!")
                break
            except Exception as e:
                self.logger.error(f"❌ Error processing query: {e}")

def list_available_states():
    """List all available states in the vector stores"""
    current_file_dir = os.path.dirname(os.path.abspath(__file__))
    prod_vector_store_dir = os.path.join(current_file_dir, "prod_gold_data", "vector_store")
    fallback_vector_store_dir = os.path.join(current_file_dir, "vector_store")
    
    available_states = set()
    
    for vector_dir in [prod_vector_store_dir, fallback_vector_store_dir]:
        if os.path.exists(vector_dir):
            for filename in os.listdir(vector_dir):
                if filename.startswith("l2_embeddings_") and filename.endswith(".pkl"):
                    state = filename.replace("l2_embeddings_", "").replace(".pkl", "").upper()
                    available_states.add(state)
    
    return sorted(list(available_states))

async def main():
    parser = argparse.ArgumentParser(description="Vector Store Playground - Search L2 district embeddings")
    parser.add_argument("--state", "-s", type=str, help="State code (e.g., CA, TX, NY)")
    parser.add_argument("--query", "-q", type=str, help="Single query to search (non-interactive mode)")
    parser.add_argument("--list-states", "-l", action="store_true", help="List all available states")
    
    args = parser.parse_args()
    
    if args.list_states:
        states = list_available_states()
        print(f"\nAvailable states ({len(states)}):")
        for state in states:
            print(f"  {state}")
        return
    
    if not args.state:
        print("❌ Error: Please specify a state with --state or -s")
        print("Use --list-states to see available states")
        return
    
    playground = VectorStorePlayground(args.state)
    
    if not playground.load_state_embeddings():
        print(f"❌ Failed to load embeddings for state {args.state}")
        return
    
    if args.query:
        matches = await playground.search_embeddings(args.query)
        playground.display_results(args.query, matches)
        
        cost_stats = playground.embedding_client.get_cost_stats()
        print(f"\n💰 Total cost: ${cost_stats['total_cost']:.6f}")
    else:
        await playground.interactive_mode()

if __name__ == "__main__":
    asyncio.run(main())