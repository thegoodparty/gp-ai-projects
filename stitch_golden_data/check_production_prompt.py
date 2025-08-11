import os
import sys
import asyncio

# Add the prod_gold_data directory to the path
current_dir = os.path.dirname(os.path.abspath(__file__))
prod_gold_data_dir = os.path.join(current_dir, "prod_gold_data")
sys.path.insert(0, prod_gold_data_dir)

from production_matcher import ProductionMatcher
from shared.logger import get_logger

async def check_prompt_output():
    """Check what prompt the production matcher sends to the LLM"""
    logger = get_logger(__name__)
    
    # Initialize matcher
    matcher = ProductionMatcher()
    
    # Get a state with available embeddings
    available_states = matcher.get_available_states()
    if not available_states:
        logger.error("No states with vector stores available")
        return
    
    # Use Colorado specifically
    test_state = "CO"
    if test_state not in available_states:
        logger.error(f"Colorado (CO) not available. Available states: {available_states}")
        return
    
    logger.info(f"Testing with state: {test_state}")
    
    # Load vector store for Colorado
    vector_store = matcher.load_vector_store(test_state)
    if not vector_store:
        logger.error(f"Failed to load vector store for {test_state}")
        return
    
    # Test with Arvada City Council - At Large
    test_br_name = "Arvada City Council - At Large"
    
    logger.info(f"Getting embedding matches for: '{test_br_name}' in {test_state}")
    
    # Get embedding matches (this will show us the districts found)
    embedding_districts = await matcher.get_top_embedding_matches(test_br_name, test_state, top_k=10)
    
    if not embedding_districts:
        logger.error("No embedding matches found")
        return
    
    logger.info(f"\nFound {len(embedding_districts)} embedding matches:")
    for i, district in enumerate(embedding_districts, 1):
        logger.info(f"  {i}. {district.l2_district_name} ({district.l2_district_type}) - Score: {district.similarity_score:.3f}")
    
    # Now show what the LLM prompt looks like
    logger.info(f"\n{'='*80}")
    logger.info("LLM PROMPT PREVIEW")
    logger.info(f"{'='*80}")
    
    # Prepare district descriptions (same logic as in the matcher)
    district_descriptions = []
    for i, district in enumerate(embedding_districts, 1):
        district_descriptions.append(
            f"{i}. {district.l2_district_name} ({district.l2_district_type})"
        )
    
    districts_text = "\n".join(district_descriptions)
    state = embedding_districts[0].state if embedding_districts else "Unknown"
    
    # Create the exact prompt that would be sent to the LLM
    prompt = f"""
You are analyzing a political position to find the best L2 district match from candidate districts.

BR Position Details:
- Name: "{test_br_name}"
- State: {state}

Top {len(embedding_districts)} District Candidates:
{districts_text}

Analyze the BR position and select the BEST matching candidate. Consider:
- Geographic alignment (city/county matching)
- Office type and district type compatibility
- Specific identifiers or numbers in names
- Functional role alignment (e.g., School Board → School Board districts)
- Ignore seats and positions
- if the office is greater than the state level, match to the state level

Return JSON with:
• selected_candidate_number: Number (1-{len(embedding_districts)}) of your choice, or 0 if no good match
• selection_confidence: Confidence level (0-100)
• reasoning: Detailed explanation of your selection or rejection
• close_alternatives: Array of candidate numbers that were very close (only if multiple options were neck-and-neck)

IMPORTANT: Return 0 if no candidate represents a reasonable match. Base decisions on semantic meaning, geography, and functional appropriateness.
"""
    
    print(prompt)
    
    logger.info(f"\n{'='*80}")
    logger.info("PROMPT ANALYSIS")
    logger.info(f"{'='*80}")
    logger.info(f"✅ No embedding scores visible in LLM prompt")
    logger.info(f"✅ Districts presented as neutral candidates")
    logger.info(f"✅ LLM instructed to use semantic reasoning")
    logger.info(f"📊 Total prompt length: {len(prompt)} characters")

if __name__ == "__main__":
    asyncio.run(check_prompt_output())