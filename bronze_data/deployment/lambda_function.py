import os
import json
import pickle
import numpy as np
import asyncio
import boto3
import requests
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict
import logging
import time

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Gemini pricing per million tokens (updated from Google's official pricing as of 2025)
GEMINI_PRICING = {
    'gemini-2.5-flash': {'input': 0.30, 'output': 2.50},  # Current 2025 pricing
    'gemini-2.5-pro': {'input': 1.25, 'output': 10.0},   # Up to 200K tokens
    'gemini-embedding-001': {'input': 0.15, 'output': 0.0},  # Only input cost for embeddings
}

@dataclass
class EmbeddingMatch:
    district_name: str
    district_type: str
    state: str
    similarity_score: float
    full_text: str

@dataclass
class PreprocessingResult:
    assessment: str  # "MATCHABLE" or "UNMATCHABLE"
    confidence: float
    reasoning: str
    derived_city: Optional[str] = None
    derived_county: Optional[str] = None
    derived_state: Optional[str] = None

@dataclass 
class LLMMatchResult:
    selected_district_name: str
    selected_district_type: str
    selection_confidence: float
    selection_reasoning: str
    is_match: bool
    alternative_matches: Optional[List[Dict]] = None

class GeminiClient:
    def __init__(self):
        self.api_key = os.environ.get('GEMINI_API_KEY')
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable is required")
        self.total_cost = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
    
    def _calculate_cost(self, model_name: str, input_tokens: int, output_tokens: int) -> float:
        """Calculate cost based on actual token usage."""
        model_key = model_name if model_name in GEMINI_PRICING else 'gemini-2.5-flash'
        pricing = GEMINI_PRICING[model_key]
        input_cost = (input_tokens / 1_000_000) * pricing['input']
        output_cost = (output_tokens / 1_000_000) * pricing['output']
        return input_cost + output_cost
    
    def _estimate_tokens(self, text: str) -> int:
        """Rough estimate: ~4 chars per token for English text."""
        return len(text) // 4
    
    def generate_structured_content(self, prompt, response_schema, model="gemini-2.5-flash", temperature=0.0):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key
        }
        
        data = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "response_mime_type": "application/json",
                "response_schema": response_schema
            }
        }
        
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        
        result = response.json()
        content = result['candidates'][0]['content']['parts'][0]['text']
        
        # Estimate token usage and calculate cost
        input_tokens = self._estimate_tokens(prompt)
        output_tokens = self._estimate_tokens(content)
        cost = self._calculate_cost(model, input_tokens, output_tokens)
        
        # Track usage
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cost += cost
        
        logger.info(f"LLM usage - Model: {model}, Input tokens: {input_tokens}, Output tokens: {output_tokens}, Cost: ${cost:.6f}")
        
        return json.loads(content)
    
    def get_cost_stats(self):
        return {'total_cost': self.total_cost}
    
    async def assess_candidate_seriousness(self, candidate_record: Dict) -> PreprocessingResult:
        """Assess if candidate record is matchable using preprocessing logic"""
        
        # Extract fields with fallbacks
        def get_field(field_name: str) -> str:
            value = str(candidate_record.get(field_name, '')).strip()
            return value if value.lower() not in ['none', ''] else ''
        
        candidate_name = get_field('candidate_name')
        office = get_field('office')
        city = get_field('city')
        state = get_field('state')
        county = get_field('county')
        district = get_field('district')
        zip_code = get_field('zip_code')
        
        # Check what location data we have
        has_city = bool(city)
        has_district = bool(district)
        has_state = bool(state)
        has_county = bool(county)
        has_zip = bool(zip_code)
        
        has_any_location = has_city or has_district or has_state or has_county
        include_zip = not has_any_location and has_zip
        
        # Determine what needs to be derived
        needs_city = not has_city and has_zip
        needs_county = not has_county and has_zip  
        needs_state = not has_state and has_zip
        
        candidate_info = f"""CANDIDATE:
- Name: "{candidate_name}"
- Office: "{office}"  
- City: "{city}"
- State: "{state}"
- County: "{county}"
- District: "{district}\""""
        
        if include_zip:
            candidate_info += f"""
- ZIP Code: "{zip_code}" """
        
        prompt = f"""Assess if this is a MATCHABLE POLITICAL CANDIDATE using two criteria:

{candidate_info}

CRITERIA 1 - POLITICAL LEGITIMACY:
✓ ACCEPT if: Real candidate name + Real political office (Mayor, City Council, Judge, School Board, etc.)
✓ BE FLEXIBLE: "Town Mayor" vs "City Mayor" vs "Village Mayor" are all legitimate mayoral offices
✗ REJECT if: Business names, "Work from home", "Charity", spam, gibberish names/offices

CRITERIA 2 - GEOGRAPHIC MATCHABILITY:
✓ ACCEPT if: Has sufficient geographic identifier for political races:
  - Any valid location combination (city/county/district + state)
  - ZIP code with derivable geographic context
  - Be flexible - "Orange County" as city is valid geographic identifier for county-level races
✗ REJECT if: Missing ALL geographic identifiers OR obviously fake locations OR invalid ZIP codes

GEOGRAPHIC INTELLIGENCE: When ZIP codes are provided, use your knowledge to derive missing location context ONLY when geographic information is insufficient.
- Don't use ZIP to resolve conflicts - focus on whether there's enough geographic information to identify where political races would occur
- Be flexible about minor geographic inconsistencies - if there's a real person + real office + reasonable location, it's likely MATCHABLE

ASSESSMENT: "MATCHABLE" if both criteria met, "UNMATCHABLE" if either fails.

LOCATION DERIVATION: Only provide derived location fields to fill gaps in geographic coverage:
- Provide derived_city ONLY when no city information exists and ZIP allows city lookup
- Provide derived_county ONLY when no county information exists and ZIP allows county lookup  
- Provide derived_state ONLY when no state information exists and ZIP allows state lookup
- Don't duplicate existing geographic information, just fill missing gaps

Examples:
- "John Smith - Mayor (Springfield, IL)" → MATCHABLE (city+state available)
- "David Denune - Martinsburg Town Mayor (Martinsburg, OH)" → MATCHABLE (legitimate mayoral office, don't worry about town vs city vs village classification)
- "Jason Bhardwaj - School Committee (Newton, MI)" → MATCHABLE (real person + real office + sufficient location, minor state inconsistency acceptable)
- "Benjamin Tracy - Upper Arlington City Council (Arlington, MA)" → MATCHABLE (real person + real office + sufficient location context)
- "Jane Doe - Town Meeting (, MA, ZIP: 02467)" → MATCHABLE (ZIP 02467 = Chestnut Hill, MA - ZIP used due to missing city)
- "Bob Wilson - School Board (, TX, ZIP: 78701)" → MATCHABLE (ZIP 78701 = Austin, TX - ZIP used due to missing city)
- "Janet Dee - Orange County Supervisor (Orange County, CA)" → MATCHABLE (county in city field is valid geographic identifier)
- "Work Inc - City Council (, TX)" → UNMATCHABLE (business name, fails political legitimacy)  
- "Mary Johnson - Work from home (Austin, TX)" → UNMATCHABLE (not political office, fails political legitimacy)
- "Janet Dee - jent (Orange County, CA)" → UNMATCHABLE (invalid office, fails political legitimacy)"""

        response_schema = {
            "type": "object", 
            "properties": {
                "assessment": {"type": "string", "enum": ["MATCHABLE", "UNMATCHABLE"]},
                "confidence": {"type": "integer", "minimum": 50, "maximum": 100},
                "reasoning": {"type": "string", "maxLength": 200},
                "derived_city": {"type": "string", "description": "City derived from ZIP code if available"},
                "derived_county": {"type": "string", "description": "County derived from ZIP code if available"},
                "derived_state": {"type": "string", "description": "State derived from ZIP code if available"}
            },
            "required": ["assessment", "confidence", "reasoning"]
        }
        
        # LLM call with retry (5 attempts for preprocessing - faster than matching)
        for attempt in range(5):
            try:
                response = await asyncio.to_thread(
                    self.generate_structured_content,
                    prompt=prompt,
                    response_schema=response_schema,
                    model="gemini-2.5-flash",
                    temperature=0.0
                )
                
                return PreprocessingResult(
                    assessment=response['assessment'],
                    confidence=float(response['confidence']),
                    reasoning=response['reasoning'],
                    derived_city=response.get('derived_city') if needs_city else None,
                    derived_county=response.get('derived_county') if needs_county else None,
                    derived_state=response.get('derived_state') if needs_state else None
                )
                
            except Exception as e:
                if attempt < 4:
                    await asyncio.sleep(1.0 * (2 ** attempt))
                    continue
                else:
                    # Conservative fallback for preprocessing errors
                    logger.error(f"Preprocessing failed after 5 attempts: {e}")
                    return PreprocessingResult(
                        assessment="UNMATCHABLE",
                        confidence=30.0,
                        reasoning=f"Assessment failed: {str(e)[:100]}",
                        derived_city=None,
                        derived_county=None,
                        derived_state=None
                    )

class GeminiEmbeddingClient:
    def __init__(self):
        self.api_key = os.environ.get('GEMINI_API_KEY')
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable is required")
        self.total_cost = 0.0
        self.total_tokens = 0
    
    def _estimate_tokens(self, text: str) -> int:
        """Rough estimate: ~4 chars per token for English text."""
        return len(text) // 4
    
    def _calculate_cost(self, tokens: int) -> float:
        """Calculate embedding cost based on token count."""
        pricing = GEMINI_PRICING['gemini-embedding-001']
        return (tokens / 1_000_000) * pricing['input']
    
    def create_embeddings(self, texts, batch=True):
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key
        }
        
        embeddings = []
        total_tokens = 0
        
        for text in texts:
            data = {"model": "models/gemini-embedding-001", "content": {"parts": [{"text": text}]}}
            response = requests.post(url, headers=headers, json=data)
            response.raise_for_status()
            
            result = response.json()
            embedding = result['embedding']['values']
            embeddings.append(np.array(embedding))
            
            # Track tokens and cost
            tokens = self._estimate_tokens(text)
            total_tokens += tokens
        
        # Calculate total cost for this batch
        cost = self._calculate_cost(total_tokens)
        self.total_cost += cost
        self.total_tokens += total_tokens
        
        logger.info(f"Embedding usage - Texts: {len(texts)}, Total tokens: {total_tokens}, Cost: ${cost:.6f}")
        
        return embeddings
    
    def get_cost_stats(self):
        return {'total_cost': self.total_cost}

class LambdaLLMMatching:
    def __init__(self):
        self.s3_client = boto3.client('s3')
        self.bucket_name = os.environ['S3_BUCKET_NAME']
        self.embedding_client = GeminiEmbeddingClient()
        self.llm_client = GeminiClient()
        
        # Cache directory in Lambda /tmp
        self.cache_dir = "/tmp/embeddings"
        os.makedirs(self.cache_dir, exist_ok=True)
    
    def get_cache_path(self, state: str) -> str:
        return os.path.join(self.cache_dir, f"l2_embeddings_{state.lower()}.pkl")
    
    async def load_state_embeddings(self, state: str):
        state_lower = state.lower()
        cache_path = self.get_cache_path(state)
        s3_key = f"embeddings/l2_embeddings_{state_lower}.pkl"
        
        # Check cache first
        if os.path.exists(cache_path):
            logger.info(f"Loading {state} embeddings from cache")
            try:
                with open(cache_path, 'rb') as f:
                    data = pickle.load(f)
                    return data['embeddings'], data['texts'], data['metadata']
            except Exception as e:
                logger.error(f"Cache read error: {e}, falling back to S3")
        
        # Download from S3
        logger.info(f"Downloading {state} embeddings from S3: {s3_key}")
        try:
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=s3_key)
            data = pickle.loads(response['Body'].read())
            
            # Cache for future use
            try:
                with open(cache_path, 'wb') as f:
                    pickle.dump(data, f)
                logger.info(f"Cached {state} embeddings to {cache_path}")
            except Exception as e:
                logger.warning(f"Failed to cache embeddings: {e}")
            
            return data['embeddings'], data['texts'], data['metadata']
            
        except Exception as e:
            logger.error(f"Failed to load embeddings for {state}: {e}")
            raise ValueError(f"Embeddings not found for state {state}")
    
    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))
    
    def _create_embedding_query(self, candidate_record: Dict, preprocessing_result: PreprocessingResult) -> str:
        """Create embedding query from candidate record using our proven logic"""
        
        def get_field(field_name: str) -> str:
            value = str(candidate_record.get(field_name, '')).strip()
            return value if value.lower() not in ['none', ''] else ''
        
        # Use derived data from preprocessing if available
        office = get_field('office')
        city = get_field('city') or preprocessing_result.derived_city or ''
        county = get_field('county') or preprocessing_result.derived_county or ''
        district = get_field('district')
        
        query_parts = []
        
        if office:
            is_municipal = any(term in office.lower() for term in ['council', 'mayor', 'alderman', 'commissioner'])
            if is_municipal and city:
                query_parts.append(f"district type: {office}, district name: {city}")
            else:
                query_parts.append(f"district type: {office}")
        
        for field, label in [('city', 'district name'), ('county', 'county'), ('district', 'district')]:
            field_value = locals().get(field, '')
            if field_value:
                query_parts.append(f"{label}: {field_value}")
        
        return " | ".join(query_parts) if query_parts else f"position in {get_field('state')}"

    async def get_embedding_matches(self, candidate_record: Dict, preprocessing_result: PreprocessingResult, state: str, embeddings, texts, metadata, top_k: int = 13) -> List[EmbeddingMatch]:
        # Create embedding query using our proven logic
        user_query = self._create_embedding_query(candidate_record, preprocessing_result)
        state_query = "state"  # Generic state fallback (matches production pattern)
        
        # Get query embeddings for both searches simultaneously
        query_embeddings = await asyncio.to_thread(
            self.embedding_client.create_embeddings, 
            [user_query, state_query], 
            False
        )
        
        if not query_embeddings or len(query_embeddings) < 2:
            return []
            
        user_embedding = query_embeddings[0]
        state_embedding = query_embeddings[1]
        
        # Get user's city with preprocessing fallback for geographic boosting
        def get_field(field_name: str) -> str:
            value = str(candidate_record.get(field_name, '')).strip()
            return value if value.lower() not in ['none', ''] else ''
        
        user_city = get_field('city') or preprocessing_result.derived_city or ''
        user_city = user_city.upper() if user_city else ''
        
        # Calculate similarities with geographic boosting (matches our proven logic)
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
        
        for similarity_score, idx, was_boosted in user_results:
            user_indices.add(idx)
            meta = metadata[idx]
            match = EmbeddingMatch(
                district_name=meta['district_name'],
                district_type=meta['district_type'], 
                state=meta['state'],
                similarity_score=similarity_score,
                full_text=texts[idx]
            )
            matches.append(match)
        
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
                    district_name=state_meta['district_name'],
                    district_type=state_meta['district_type'],
                    state=state_meta['state'],
                    similarity_score=state_sim,
                    full_text=texts[state_idx]
                )
                
                # Insert as 11th result
                matches.insert(10, state_match)
                matches = matches[:top_k]
        
        return matches
    
    async def llm_select_best_match(self, br_name: str, districts: List[EmbeddingMatch]) -> Optional[LLMMatchResult]:
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
        
        # Enhanced retry logic for data accuracy - retry 9 times (matching production matcher)
        max_retries = 9
        base_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                response = await asyncio.to_thread(
                    self.llm_client.generate_structured_content,
                    prompt=prompt,
                    response_schema=response_schema,
                    model="gemini-2.5-flash",
                    temperature=0.0
                )
                break  # Success, exit retry loop
                
            except Exception as e:
                # For transient errors, retry with exponential backoff
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"LLM attempt {attempt + 1}/{max_retries} failed for {br_name}: {e}. Retrying in {delay:.1f}s")
                    await asyncio.sleep(delay)
                else:
                    # Final attempt failed
                    logger.error(f"LLM failed after {max_retries} attempts for {br_name}: {e}")
                    return LLMMatchResult(
                        selected_district_name="LLM_ERROR",
                        selected_district_type="LLM_ERROR",
                        selection_confidence=0.0,
                        selection_reasoning=f"LLM generation failed after {max_retries} attempts: {str(e)}",
                        is_match=False,
                        alternative_matches=None
                    )
        
        # Handle potential float or invalid response (matching production matcher)
        try:
            selected_number = int(float(response["selected_candidate_number"]))
            # Ensure the number is within valid bounds
            if selected_number < 0 or selected_number > len(districts):
                logger.warning(f"Selected candidate number {selected_number} out of bounds (0-{len(districts)}). Defaulting to 0 (no match).")
                selected_number = 0
        except (ValueError, TypeError, KeyError) as e:
            logger.warning(f"Invalid selected_candidate_number in LLM response: {response.get('selected_candidate_number', 'missing')}. Defaulting to 0 (no match).")
            selected_number = 0
        
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
        
        # Process alternatives with robust error handling (matching production matcher)
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
                    else:
                        logger.warning(f"Alternative match number {alt_num_int} out of bounds (1-{len(districts)}). Skipping.")
                except (ValueError, TypeError) as e:
                    logger.warning(f"Invalid alternative match number: {alt_num}. Skipping.")
        
        return LLMMatchResult(
            selected_district_name=selected_district.district_name,
            selected_district_type=selected_district.district_type,
            selection_confidence=response["selection_confidence"],
            selection_reasoning=response["reasoning"],
            is_match=True,
            alternative_matches=alternative_matches
        )
    
    async def process_matching(self, candidate_record: Dict) -> Dict:
        start_time = time.time()
        
        # Step 1: Preprocessing validation
        preprocessing_start = time.time()
        preprocessing_result = await self.llm_client.assess_candidate_seriousness(candidate_record)
        preprocessing_duration = (time.time() - preprocessing_start) * 1000
        
        preprocessing_cost = self.llm_client.get_cost_stats().get('total_cost', 0.0)
        
        # If unmatchable, return early with consistent response structure
        if preprocessing_result.assessment == "UNMATCHABLE":
            return {
                'success': True,
                'data': {
                    'preprocessing': {
                        'assessment': preprocessing_result.assessment,
                        'confidence': preprocessing_result.confidence,
                        'reasoning': preprocessing_result.reasoning,
                        'derived_city': preprocessing_result.derived_city,
                        'derived_county': preprocessing_result.derived_county,
                        'derived_state': preprocessing_result.derived_state
                    },
                    'matching': None
                },
                'metadata': {
                    'processing_time_ms': round(preprocessing_duration, 2),
                    'costs': {
                        'preprocessing': preprocessing_cost,
                        'embedding': 0,
                        'llm': 0,
                        'total': preprocessing_cost
                    }
                }
            }
        
        # Step 2: L2 Matching (only if MATCHABLE)
        matching_start = time.time()
        
        # Get state - use derived if available
        state = candidate_record.get('state', '') or preprocessing_result.derived_state or ''
        state = state.upper().strip()
        
        if not state:
            return {
                'success': False,
                'error': 'State is required for L2 matching',
                'data': {
                    'preprocessing': asdict(preprocessing_result),
                    'matching': None
                }
            }
        
        # Load embeddings
        try:
            embeddings, texts, metadata = await self.load_state_embeddings(state)
            logger.info(f"Loaded {len(embeddings)} embeddings for {state}")
        except Exception as e:
            return {
                'success': False,
                'error': f'Failed to load embeddings for state {state}: {str(e)}',
                'data': {
                    'preprocessing': asdict(preprocessing_result),
                    'matching': None
                }
            }
        
        # Get embedding matches
        embedding_matches = await self.get_embedding_matches(
            candidate_record, preprocessing_result, state, embeddings, texts, metadata, top_k=13
        )
        
        # LLM selection using candidate name and office for context
        candidate_name = candidate_record.get('candidate_name', 'Unknown')
        office = candidate_record.get('office', 'Unknown Office')
        br_name = f"{candidate_name} - {office}"
        
        llm_result = await self.llm_select_best_match(br_name, embedding_matches)
        
        matching_duration = (time.time() - matching_start) * 1000
        total_duration = (time.time() - start_time) * 1000
        
        # Get final costs
        embedding_cost = self.embedding_client.get_cost_stats().get('total_cost', 0.0) 
        total_llm_cost = self.llm_client.get_cost_stats().get('total_cost', 0.0)
        matching_llm_cost = total_llm_cost - preprocessing_cost
        
        return {
            'success': True,
            'data': {
                'preprocessing': {
                    'assessment': preprocessing_result.assessment,
                    'confidence': preprocessing_result.confidence,
                    'reasoning': preprocessing_result.reasoning,
                    'derived_city': preprocessing_result.derived_city,
                    'derived_county': preprocessing_result.derived_county,
                    'derived_state': preprocessing_result.derived_state
                },
                'matching': {
                    'match_found': llm_result.is_match if llm_result else False,
                    'selected_district': {
                        'name': llm_result.selected_district_name if llm_result else None,
                        'type': llm_result.selected_district_type if llm_result else None,
                        'confidence': llm_result.selection_confidence if llm_result else 0,
                        'reasoning': llm_result.selection_reasoning if llm_result else None
                    } if llm_result else None,
                    'embedding_matches': [asdict(match) for match in embedding_matches],
                    'alternative_matches': llm_result.alternative_matches if llm_result else None
                }
            },
            'metadata': {
                'processing_time_ms': round(total_duration, 2),
                'costs': {
                    'preprocessing': preprocessing_cost,
                    'embedding': embedding_cost,
                    'llm': matching_llm_cost,
                    'total': preprocessing_cost + embedding_cost + matching_llm_cost
                }
            }
        }

matching_service = LambdaLLMMatching()

def lambda_handler(event, context):
    try:
        # Debug logging for headers
        print(f"DEBUG: All headers: {event.get('headers', {})}")
        print(f"DEBUG: Expected API key: {os.environ.get('API_KEY', 'NOT_SET')}")
        
        # API Key authentication - try multiple header variations
        headers = event.get('headers', {})
        api_key = (headers.get('x-api-key') or 
                   headers.get('X-API-Key') or 
                   headers.get('X-Api-Key') or 
                   headers.get('authorization'))
        print(f"DEBUG: Received API key: {api_key}")
        
        if api_key != os.environ['API_KEY']:
            return {
                'statusCode': 401,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'Unauthorized'})
            }
        
        # Parse request body
        try:
            body = json.loads(event.get('body', '{}'))
            candidate_record = {
                'candidate_name': body.get('candidate_name', ''),
                'office': body.get('office', ''),
                'state': body.get('state', ''),
                'city': body.get('city', ''),
                'county': body.get('county', ''),
                'district': body.get('district', ''),
                'zip_code': body.get('zip_code', '')
            }
        except (json.JSONDecodeError, KeyError, AttributeError) as e:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({
                    'success': False,
                    'error': f'Invalid request body: {str(e)}',
                    'data': None,
                    'metadata': None
                })
            }
        
        # Validate required inputs
        candidate_name = candidate_record.get('candidate_name', '').strip()
        office = candidate_record.get('office', '').strip()
        
        if not candidate_name or not office:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({
                    'success': False,
                    'error': 'candidate_name and office are required',
                    'data': None,
                    'metadata': None
                })
            }
        
        logger.info(f"Processing request: candidate={candidate_name}, office={office}, state={candidate_record.get('state', 'N/A')}")
        
        # Process matching with preprocessing
        result = asyncio.run(matching_service.process_matching(candidate_record))
        
        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps(result)
        }
        
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
                'error': 'Internal server error',
                'message': str(e)
            })
        }