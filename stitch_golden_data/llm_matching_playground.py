import os
import pickle
import argparse
import numpy as np
import asyncio
from typing import List, Dict, Optional
from dataclasses import dataclass
from shared.llm_gemini import GeminiClient, GeminiModelType, GeminiEmbeddingClient
from shared.logger import get_logger

@dataclass
class EmbeddingMatch:
    district_name: str
    district_type: str
    state: str
    similarity_score: float
    full_text: str

@dataclass 
class LLMMatchResult:
    selected_district_name: str
    selected_district_type: str
    selection_confidence: float
    selection_reasoning: str
    is_match: bool
    alternative_matches: Optional[List[Dict]] = None

class LLMMatchingPlayground:
    def __init__(self, state: str):
        self.logger = get_logger(__name__)
        self.state = state.upper()
        self.embedding_client = GeminiEmbeddingClient()
        self.llm_client = GeminiClient()
        
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
    
    async def get_embedding_matches(self, query: str, top_k: int = 10) -> List[EmbeddingMatch]:
        """Get top embedding matches for the query"""
        if self.embeddings is None:
            raise ValueError(f"Embeddings not loaded for state {self.state}")
        
        self.logger.info(f"🔍 Getting embeddings for: '{query}' in {self.state}")
        
        search_query = f"race name: {query}"
        query_embeddings = await asyncio.to_thread(
            self.embedding_client.create_embeddings, 
            [search_query], 
            False
        )
        query_embedding = query_embeddings[0]
        
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
    
    async def llm_select_best_match(self, br_name: str, districts: List[EmbeddingMatch]) -> Optional[LLMMatchResult]:
        """Use LLM to select the best match from embedding results"""
        if not districts:
            return None
        
        district_descriptions = []
        for i, district in enumerate(districts, 1):
            district_descriptions.append(
                f"{i}. {district.district_name} ({district.district_type})"
            )
        
        districts_text = "\n".join(district_descriptions)
        state = districts[0].state if districts else "Unknown"
        
        prompt = f"""
You are analyzing a political position to find the best L2 district match from candidate districts.

BR Position Details:
- Name: "{br_name}"
- State: {state}

Top {len(districts)} District Candidates:
{districts_text}

Analyze the BR position and select the BEST matching candidate. Consider:
- Geographic alignment (city/county matching)
- Office type and district type compatibility
- Specific identifiers or numbers in names 
- Functional role alignment (e.g., School Board → School Board districts)
- Ignore seats and positions
- if the office is greater than the state level, match to the state level
- if you are unsure, do not be confident in your selection
- if the numbers do not match, do not select
- If the names do not at least semantically match, do not select
- if the geographies are not aligned, do not select
- If the geographic zones do not line up, do not select

The hierarchy of importance is:
1. Geographic alignment
2. Office type and district type compatibility
3. Functional role alignment
4. Specific identifiers or numbers in names
5. Semantic meaning of names

Return JSON with:
• selected_candidate_number: Number (1-{len(districts)}) of your choice, or 0 if no good match
• selection_confidence: Confidence level (0-100)
• reasoning: Detailed explanation of your selection or rejection
• close_alternatives: Array of candidate numbers that were very close (only if multiple options were neck-and-neck)

IMPORTANT: Return 0 if no candidate represents a reasonable match. 
There is a real probability that the match does not exist so return 0 if there is no clear match. 

Base decisions on semantic meaning, geography, and functional appropriateness. 
"""
        
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
            response = await asyncio.to_thread(
                self.llm_client.generate_structured_content,
                prompt=prompt,
                response_schema=response_schema,
                model=GeminiModelType.PRO,
                temperature=0.0,
                thinking_budget=500
            )
            
            selected_number = int(float(response["selected_candidate_number"]))
            if selected_number < 0 or selected_number > len(districts):
                selected_number = 0
                
        except Exception as e:
            self.logger.error(f"LLM error: {e}")
            return LLMMatchResult(
                selected_district_name="LLM_ERROR",
                selected_district_type="LLM_ERROR",
                selection_confidence=0.0,
                selection_reasoning=f"LLM generation failed: {str(e)}",
                is_match=False,
                alternative_matches=None
            )
        
        if selected_number == 0:
            return LLMMatchResult(
                selected_district_name="NOT_MATCHED",
                selected_district_type="NOT_MATCHED",
                selection_confidence=response["selection_confidence"],
                selection_reasoning=response["reasoning"],
                is_match=False,
                alternative_matches=None
            )
        
        selected_district = districts[selected_number - 1]
        
        alternative_matches = None
        if "close_alternatives" in response and response["close_alternatives"]:
            alternative_matches = []
            for alt_num in response["close_alternatives"]:
                try:
                    alt_num_int = int(float(alt_num))
                    if 0 < alt_num_int <= len(districts):
                        alt_district = districts[alt_num_int - 1]
                        alternative_matches.append({
                            "district_name": alt_district.district_name,
                            "district_type": alt_district.district_type,
                            "similarity_score": alt_district.similarity_score
                        })
                except (ValueError, TypeError):
                    continue
        
        return LLMMatchResult(
            selected_district_name=selected_district.district_name,
            selected_district_type=selected_district.district_type,
            selection_confidence=response["selection_confidence"],
            selection_reasoning=response["reasoning"],
            is_match=True,
            alternative_matches=alternative_matches
        )
    
    async def process_query(self, query: str, top_k: int = 10) -> Dict:
        """Process a query through the full pipeline: embeddings + LLM matching"""
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"PROCESSING QUERY: '{query}' in {self.state}")
        self.logger.info(f"{'='*80}")
        
        embedding_matches = await self.get_embedding_matches(query, top_k)
        
        self.logger.info(f"\n📊 TOP {len(embedding_matches)} EMBEDDING MATCHES:")
        for i, match in enumerate(embedding_matches, 1):
            self.logger.info(f"  {i}. {match.district_name} ({match.district_type}) - {match.similarity_score:.3f}")
        
        llm_result = await self.llm_select_best_match(query, embedding_matches)
        
        self.logger.info(f"\n🤖 LLM MATCHING RESULT:")
        if llm_result:
            if llm_result.is_match:
                self.logger.info(f"  ✅ MATCH: {llm_result.selected_district_name} ({llm_result.selected_district_type})")
                self.logger.info(f"  🎯 Confidence: {llm_result.selection_confidence}%")
            else:
                self.logger.info(f"  ❌ NO MATCH: {llm_result.selected_district_name}")
                self.logger.info(f"  🎯 Confidence: {llm_result.selection_confidence}%")
            
            self.logger.info(f"\n💭 REASONING:")
            reasoning_lines = llm_result.selection_reasoning.split('\n')
            for line in reasoning_lines:
                if line.strip():
                    self.logger.info(f"  {line.strip()}")
            
            if llm_result.alternative_matches:
                self.logger.info(f"\n🔄 ALTERNATIVE MATCHES:")
                for alt in llm_result.alternative_matches:
                    self.logger.info(f"  • {alt['district_name']} ({alt['district_type']}) - {alt['similarity_score']:.3f}")
        else:
            self.logger.info(f"  ❌ LLM failed to process")
        
        embedding_cost = self.embedding_client.get_cost_stats()['total_cost']
        llm_cost = self.llm_client.get_usage_stats()['total_cost']
        total_cost = embedding_cost + llm_cost
        
        self.logger.info(f"\n💰 COST BREAKDOWN:")
        self.logger.info(f"  Embedding: ${embedding_cost:.6f}")
        self.logger.info(f"  LLM: ${llm_cost:.6f}")
        self.logger.info(f"  Total: ${total_cost:.6f}")
        
        return {
            'query': query,
            'state': self.state,
            'embedding_matches': embedding_matches,
            'llm_result': llm_result,
            'costs': {
                'embedding': embedding_cost,
                'llm': llm_cost,
                'total': total_cost
            }
        }
    
    async def interactive_mode(self):
        """Run interactive LLM matching mode"""
        self.logger.info(f"\n🎮 INTERACTIVE LLM MATCHING PLAYGROUND - {self.state}")
        self.logger.info(f"{'='*70}")
        self.logger.info(f"Loaded {len(self.embeddings):,} embeddings for {self.state}")
        self.logger.info(f"Pipeline: Query → Embedding Search → LLM Match Analysis")
        self.logger.info(f"Type your queries below. Type 'quit' or 'exit' to quit.")
        self.logger.info(f"{'='*70}")
        
        total_cost = 0.0
        query_count = 0
        
        while True:
            try:
                query = input(f"\n🔍 Enter BR position name for {self.state}: ").strip()
                
                if query.lower() in ['quit', 'exit', 'q']:
                    break
                
                if not query:
                    continue
                
                query_count += 1
                result = await self.process_query(query)
                total_cost = result['costs']['total']
                
                self.logger.info(f"\n📈 SESSION STATS:")
                self.logger.info(f"  Queries processed: {query_count}")
                self.logger.info(f"  Total cost: ${total_cost:.6f}")
                self.logger.info(f"  Average per query: ${total_cost/query_count:.6f}")
                
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.logger.error(f"❌ Error processing query: {e}")
        
        self.logger.info(f"\n👋 Session complete! Total cost: ${total_cost:.6f} for {query_count} queries")

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
    parser = argparse.ArgumentParser(description="LLM Matching Playground - Embeddings + LLM Analysis")
    parser.add_argument("--state", "-s", type=str, help="State code (e.g., CA, TX, NY)")
    parser.add_argument("--query", "-q", type=str, help="Single query to process (non-interactive mode)")
    parser.add_argument("--list-states", "-l", action="store_true", help="List all available states")
    parser.add_argument("--top-k", "-k", type=int, default=10, help="Number of embedding matches to consider (default: 10)")
    
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
    
    playground = LLMMatchingPlayground(args.state)
    
    if not playground.load_state_embeddings():
        print(f"❌ Failed to load embeddings for state {args.state}")
        return
    
    if args.query:
        result = await playground.process_query(args.query, args.top_k)
        
        print(f"\n💡 Single query processed!")
        print(f"Total cost: ${result['costs']['total']:.6f}")
        
        if result['llm_result'] and result['llm_result'].is_match:
            print(f"Result: MATCH - {result['llm_result'].selected_district_name}")
        else:
            print(f"Result: NO MATCH")
    else:
        await playground.interactive_mode()

if __name__ == "__main__":
    asyncio.run(main())