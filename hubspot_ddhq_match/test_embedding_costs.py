#!/usr/bin/env python3

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from shared.llm_gemini import GeminiEmbeddingClient, GeminiClient
from shared.logger import get_logger
from offline_data.load_offline_data import load_both_tables
import pandas as pd
import numpy as np

async def test_embedding_costs():
    """Test actual embedding costs with real API calls"""
    
    logger = get_logger(__name__)
    print("💰 EMBEDDING COST ANALYSIS - REAL API CALLS")
    print("=" * 60)
    
    # Load sample data
    candidacy_df, ddhq_df = load_both_tables()
    
    print(f"📊 DATA LOADED:")
    print(f"   HubSpot: {len(candidacy_df):,} total records")
    print(f"   DDHQ: {len(ddhq_df):,} total records")
    
    # Filter to records with required fields
    hubspot_valid = candidacy_df[
        candidacy_df['full_name'].notna() & 
        candidacy_df['general_election_date'].notna()
    ].copy()
    
    ddhq_valid = ddhq_df[
        ddhq_df['candidate'].notna() & 
        ddhq_df['date'].notna() &
        ddhq_df['race_name'].notna()
    ].copy()
    
    print(f"\n   Valid for embedding:")
    print(f"   HubSpot: {len(hubspot_valid):,} records ({len(hubspot_valid)/len(candidacy_df)*100:.1f}%)")
    print(f"   DDHQ: {len(ddhq_valid):,} records ({len(ddhq_valid)/len(ddhq_df)*100:.1f}%)")
    
    # Text construction functions
    def construct_hubspot_text(row):
        """Construct embedding text for HubSpot record"""
        parts = []
        
        if pd.notna(row.get('full_name')):
            parts.append(f"name:{row['full_name']}")
        
        if pd.notna(row.get('general_election_date')):
            parts.append(f"date:{row['general_election_date']}")
            
        # Race name from state + office
        race_parts = []
        if pd.notna(row.get('state')):
            race_parts.append(str(row['state']))
        if pd.notna(row.get('official_office_name')):
            race_parts.append(str(row['official_office_name']))
        
        if race_parts:
            parts.append(f"race_name:{' '.join(race_parts)}")
            
        return " | ".join(parts) if parts else ""
    
    def construct_ddhq_text(row):
        """Construct embedding text for DDHQ record"""
        parts = []
        
        if pd.notna(row.get('candidate')):
            parts.append(f"name:{row['candidate']}")
            
        if pd.notna(row.get('date')):
            parts.append(f"date:{row['date']}")
            
        if pd.notna(row.get('race_name')):
            parts.append(f"race_name:{row['race_name']}")
            
        return " | ".join(parts) if parts else ""
    
    # Test with small samples first
    print(f"\n🧪 TESTING WITH SAMPLE RECORDS")
    print("-" * 40)
    
    hubspot_sample = hubspot_valid.head(3).copy()
    ddhq_sample = ddhq_valid.head(3).copy()
    
    print("Sample HubSpot embedding texts:")
    hubspot_texts = []
    for i, (_, row) in enumerate(hubspot_sample.iterrows(), 1):
        text = construct_hubspot_text(row)
        hubspot_texts.append(text)
        print(f"   {i}. {text}")
    
    print(f"\nSample DDHQ embedding texts:")
    ddhq_texts = []
    for i, (_, row) in enumerate(ddhq_sample.iterrows(), 1):
        text = construct_ddhq_text(row)
        ddhq_texts.append(text)
        print(f"   {i}. {text}")
    
    # Calculate text statistics
    all_texts = hubspot_texts + ddhq_texts
    avg_length = sum(len(t) for t in all_texts) / len(all_texts)
    max_length = max(len(t) for t in all_texts)
    min_length = min(len(t) for t in all_texts)
    
    print(f"\nText length statistics:")
    print(f"   Average: {avg_length:.0f} characters")
    print(f"   Range: {min_length} - {max_length} characters")
    print(f"   Estimated tokens: ~{avg_length/4:.0f} (assuming 4 chars/token)")
    
    # Test actual embedding API calls
    print(f"\n🚀 REAL API EMBEDDING TESTS")
    print("-" * 40)
    
    try:
        embedding_client = GeminiEmbeddingClient()
        llm_client = GeminiClient()
        
        print("Testing embeddings with real API calls...")
        
        # Test a few embeddings
        test_embeddings = []
        
        for i, text in enumerate(all_texts[:3]):  # Only test 3 to keep costs low
            print(f"\nEmbedding text {i+1}: {text[:50]}...")
            
            # Get single embedding
            embedding = embedding_client.create_embeddings([text])[0]
            test_embeddings.append(embedding)
            
            print(f"   Embedding dimensions: {len(embedding)}")
            print(f"   Total embeddings created so far: {embedding_client.total_embeddings_created}")
            print(f"   Total cost so far: ${embedding_client.total_cost:.6f}")
            
        # Calculate cost per embedding
        total_cost = embedding_client.total_cost
        cost_per_embedding = total_cost / len(test_embeddings) if test_embeddings else 0
        
        print(f"\n📊 EMBEDDING COST ANALYSIS:")
        print(f"   Embeddings tested: {len(test_embeddings)}")
        print(f"   Total cost: ${total_cost:.6f}")
        print(f"   Cost per embedding: ${cost_per_embedding:.6f}")
        
        # Project costs for full dataset
        total_embeddings_needed = len(hubspot_valid) + len(ddhq_valid)
        projected_embedding_cost = total_embeddings_needed * cost_per_embedding
        
        print(f"\n💰 PROJECTED COSTS FOR FULL DATASET:")
        print(f"   HubSpot embeddings needed: {len(hubspot_valid):,}")
        print(f"   DDHQ embeddings needed: {len(ddhq_valid):,}")
        print(f"   Total embeddings: {total_embeddings_needed:,}")
        print(f"   Projected cost: ${projected_embedding_cost:.2f}")
        
        # Test LLM validation approach
        print(f"\n🤖 TESTING LLM VALIDATION APPROACH")
        print("-" * 40)
        
        # Create a mock similarity search result
        print("Simulating FAISS similarity search results...")
        
        # Mock top-K results for one HubSpot record
        hubspot_candidate = hubspot_texts[0]
        mock_matches = [
            {"ddhq_text": ddhq_texts[0], "similarity": 0.95},
            {"ddhq_text": ddhq_texts[1], "similarity": 0.82}, 
            {"ddhq_text": ddhq_texts[2], "similarity": 0.76},
        ]
        
        print(f"HubSpot candidate: {hubspot_candidate}")
        print(f"Top matches from FAISS:")
        for i, match in enumerate(mock_matches, 1):
            print(f"   {i}. Similarity: {match['similarity']:.2f} | {match['ddhq_text']}")
        
        # LLM validation prompt
        validation_prompt = f"""You are matching political candidates between two datasets.

HubSpot Candidate: {hubspot_candidate}

Potential DDHQ Matches:
1. Similarity: {mock_matches[0]['similarity']:.2f} | {mock_matches[0]['ddhq_text']}
2. Similarity: {mock_matches[1]['similarity']:.2f} | {mock_matches[1]['ddhq_text']}
3. Similarity: {mock_matches[2]['similarity']:.2f} | {mock_matches[2]['ddhq_text']}

Determine the best match and provide:
1. Match number (1, 2, 3, or "no match")
2. Confidence score (0-100)
3. Brief reasoning

Respond in JSON format:
{{"match": 1, "confidence": 95, "reasoning": "Perfect name and date match for same race"}}"""

        print(f"\nSending validation prompt to LLM...")
        validation_result = llm_client.chat(validation_prompt)
        
        print(f"LLM validation result:")
        print(f"   {validation_result}")
        
        # Calculate validation costs
        validation_cost = llm_client.total_cost
        
        print(f"\n   Validation cost: ${validation_cost:.6f}")
        
        # Project LLM validation costs
        # Assume we need validation for ~20% of HubSpot records (those with multiple high matches)
        records_needing_validation = int(len(hubspot_valid) * 0.2)
        projected_validation_cost = records_needing_validation * validation_cost
        
        print(f"\n💰 PROJECTED LLM VALIDATION COSTS:")
        print(f"   Records likely needing validation: {records_needing_validation:,} (~20%)")
        print(f"   Cost per validation: ${validation_cost:.6f}")
        print(f"   Projected validation cost: ${projected_validation_cost:.2f}")
        
        # Total projected costs
        total_projected_cost = projected_embedding_cost + projected_validation_cost
        
        print(f"\n🎯 TOTAL PROJECT COSTS:")
        print(f"   Embedding generation: ${projected_embedding_cost:.2f}")
        print(f"   LLM validation: ${projected_validation_cost:.2f}")
        print(f"   TOTAL ESTIMATED: ${total_projected_cost:.2f}")
        
        print(f"\n✅ COST ANALYSIS COMPLETE")
        print(f"   Cost is very reasonable for the matching quality improvement!")
        
    except Exception as e:
        logger.error(f"API test failed: {str(e)}")
        print(f"❌ API test failed: {e}")
        print("Note: Make sure GEMINI_API_KEY is set in your .env file")

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_embedding_costs())