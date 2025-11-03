#!/usr/bin/env python3

"""
GENERATE EMBEDDINGS FOR CLEANED DATA

Creates embeddings for the cleaned HubSpot and DDHQ datasets
to improve matching quality.

USAGE:
uv run generate_cleaned_embeddings.py

Environment Variables:
- ENVIRONMENT: Set to 'development' for debug logging
- LOG_LEVEL: Override log level (DEBUG, INFO, WARNING, ERROR)
"""

import sys
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime
from typing import List

# Set up environment-aware logging (same as pipeline)
ENVIRONMENT = os.getenv('ENVIRONMENT', 'production').lower()
LOG_LEVEL = os.getenv('LOG_LEVEL', 'DEBUG' if ENVIRONMENT == 'development' else 'INFO').upper()

# Configure logging before importing other modules
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Reduce noise from HTTP libraries in debug mode
if LOG_LEVEL == 'DEBUG':
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from shared.llm_gemini import GeminiEmbeddingClient
from shared.logger import get_logger

class EmbeddingGenerator:
    def __init__(self, batch_size: int = None, max_concurrent_batches: int = None):
        self.logger = get_logger(__name__)
        
        # Use environment variables if provided, otherwise use parameters or defaults
        if batch_size is None:
            batch_size = int(os.getenv('BATCH_SIZE', '50'))  # Default 50 for embeddings (safer)
        
        if max_concurrent_batches is None:
            max_concurrent_batches = int(os.getenv('MAX_WORKERS', '2'))  # Default 2 for embeddings (conservative)
        
        self.batch_size = batch_size
        self.max_concurrent_batches = max_concurrent_batches
        
        # Log environment configuration
        self.logger.info(f"Environment: {ENVIRONMENT.upper()}, Log Level: {LOG_LEVEL}")
        self.logger.debug(f"Batch size: {batch_size}")
        self.logger.debug(f"Max concurrent batches: {max_concurrent_batches}")
        
        # Initialize Gemini embedding client
        self.embedding_client = GeminiEmbeddingClient()
        self.logger.info("Gemini Embedding client initialized")
        
    def create_name_race_embedding_text_hubspot(self, row: pd.Series) -> str:
        """Create name+race focused embedding text for HubSpot"""
        # Name
        name = ""
        if pd.notna(row.get('first_name')) and pd.notna(row.get('last_name')):
            name = f"{row['first_name']} {row['last_name']}"
        
        # Race: Use official_office_name (already has state), or fall back to state + candidate_office
        race = ""
        if pd.notna(row.get('official_office_name')):
            race = row['official_office_name']
        elif pd.notna(row.get('candidate_office')):
            state = row.get('state', '')
            candidate_office = row['candidate_office']
            race = f"{state} {candidate_office}".strip()

        # Format: name: Name | race: Race
        return f"name: {name} | race: {race}"
    
    def create_name_race_embedding_text_ddhq(self, row: pd.Series) -> str:
        """Create name+race focused embedding text for DDHQ"""
        # Candidate name
        name = ""
        if pd.notna(row.get('candidate')):
            name = row['candidate']
        
        # Race name (already includes state and office info)
        race = ""
        if pd.notna(row.get('race_name')):
            race = row['race_name']
        
        
        # Format: name: Name | race: Race
        return f"name: {name} | race: {race}"
    
    def generate_all_embeddings(self, texts: List[str]) -> List[np.ndarray]:
        """Generate embeddings for all texts using parallel processing"""
        self.logger.info(f"Generating embeddings for {len(texts):,} texts with batch_size={self.batch_size}, max_concurrent={self.max_concurrent_batches}")
        try:
            # Use parallel processing for the entire dataset at once
            embeddings = self.embedding_client.create_embeddings(
                texts, 
                parallel=True, 
                batch_size=self.batch_size,
                max_concurrent_batches=self.max_concurrent_batches
            )
            self.logger.info(f"Successfully generated {len(embeddings):,} embeddings")
            return embeddings
        except Exception as e:
            self.logger.error(f"Failed to generate embeddings: {str(e)}")
            self.logger.debug(f"Sample texts from failed batch: {texts[:2] if texts else 'None'}")
            # Return zero embeddings as fallback
            return [np.zeros(3072) for _ in texts]  # Gemini embedding dimension
    
    def process_hubspot_embeddings(self, df: pd.DataFrame) -> pd.DataFrame:
        """Generate embeddings for HubSpot data"""
        self.logger.info(f"🧠 Generating embeddings for {len(df):,} HubSpot records...")
        
        df = df.copy()
        
        # Create embedding texts (only name+race, skip full text)
        self.logger.info("   Creating name+race embedding texts...")
        df['embedding_name_race_text'] = df.apply(self.create_name_race_embedding_text_hubspot, axis=1)
        
        # Log sample embedding texts in debug mode
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug("Sample HubSpot name+race embedding texts:")
            for i in range(min(3, len(df))):
                self.logger.debug(f"  Row {i}: {df['embedding_name_race_text'].iloc[i]}")
        
        # Generate only name+race embeddings (skip full embeddings)
        self.logger.info(f"   Generating name+race embeddings...")
        all_name_race_texts = df['embedding_name_race_text'].tolist()
        name_race_embeddings = self.generate_all_embeddings(all_name_race_texts)
        
        # Convert to list of arrays if it's a 2D numpy array
        if isinstance(name_race_embeddings, np.ndarray) and len(name_race_embeddings.shape) == 2:
            name_race_embeddings = [name_race_embeddings[i] for i in range(name_race_embeddings.shape[0])]
        
        df['embedding_name_race'] = name_race_embeddings
        
        self.logger.info("✅ HubSpot embeddings generated")
        return df
    
    def process_ddhq_embeddings(self, df: pd.DataFrame) -> pd.DataFrame:
        """Generate embeddings for DDHQ data"""
        self.logger.info(f"🧠 Generating embeddings for {len(df):,} DDHQ records...")
        
        df = df.copy()
        
        # Create embedding texts (only name+race, skip full text)
        self.logger.info("   Creating name+race embedding texts...")
        df['embedding_name_race_text'] = df.apply(self.create_name_race_embedding_text_ddhq, axis=1)
        
        # Log sample embedding texts in debug mode
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug("Sample DDHQ name+race embedding texts:")
            for i in range(min(3, len(df))):
                self.logger.debug(f"  Row {i}: {df['embedding_name_race_text'].iloc[i]}")
        
        # Generate only name+race embeddings (skip full embeddings)
        self.logger.info(f"   Generating name+race embeddings...")
        all_name_race_texts = df['embedding_name_race_text'].tolist()
        name_race_embeddings = self.generate_all_embeddings(all_name_race_texts)
        
        # Convert to list of arrays if it's a 2D numpy array
        if isinstance(name_race_embeddings, np.ndarray) and len(name_race_embeddings.shape) == 2:
            name_race_embeddings = [name_race_embeddings[i] for i in range(name_race_embeddings.shape[0])]
        
        df['embedding_name_race'] = name_race_embeddings
        
        self.logger.info("✅ DDHQ embeddings generated")
        return df
    
    def save_embeddings(self, hubspot_df: pd.DataFrame, ddhq_df: pd.DataFrame):
        """Save datasets with embeddings"""
        self.logger.info("💾 Saving datasets with embeddings...")
        
        # Get script directory for consistent file paths
        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")
        os.makedirs(offline_data_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Save HubSpot with embeddings (using names expected by pipeline and matcher)
        hubspot_file = os.path.join(offline_data_dir, f"hubspot_filtered_with_embeddings_{timestamp}.parquet")
        hubspot_latest = os.path.join(offline_data_dir, "hubspot_filtered_with_embeddings_latest.parquet")
        
        hubspot_df.to_parquet(hubspot_file, index=False)
        hubspot_df.to_parquet(hubspot_latest, index=False)
        
        # Save DDHQ with embeddings
        ddhq_file = os.path.join(offline_data_dir, f"ddhq_with_embeddings_cleaned_{timestamp}.parquet")
        ddhq_latest = os.path.join(offline_data_dir, "ddhq_with_embeddings_cleaned_latest.parquet")
        
        ddhq_df.to_parquet(ddhq_file, index=False)
        ddhq_df.to_parquet(ddhq_latest, index=False)
        
        # Calculate file sizes
        hubspot_size = os.path.getsize(hubspot_file) / (1024 * 1024)
        ddhq_size = os.path.getsize(ddhq_file) / (1024 * 1024)
        
        self.logger.info(f"✅ Datasets with embeddings saved:")
        self.logger.info(f"   HubSpot: {hubspot_file} ({hubspot_size:.1f} MB)")
        self.logger.info(f"   DDHQ: {ddhq_file} ({ddhq_size:.1f} MB)")
        
        return hubspot_file, ddhq_file

def main():
    """Main embedding generation process"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Generate embeddings for cleaned data')
    parser.add_argument('--test-rows', type=int, default=None, help='Limit to N rows for testing')
    args = parser.parse_args()
    
    generator = EmbeddingGenerator()  # Uses BATCH_SIZE environment variable or default 50
    
    print("🚀 GENERATING EMBEDDINGS FOR CLEANED DATA")
    print("="*50)
    
    try:
        # Load cleaned data
        print("📥 Loading cleaned datasets...")
        
        # Use absolute paths for consistent loading
        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")
        
        # Load the temporally filtered HubSpot data (output from step 3) 
        # and the cleaned DDHQ data (output from step 2)
        hubspot_df = pd.read_parquet(os.path.join(offline_data_dir, 'hubspot_filtered_to_match_ddhq_dates_latest.parquet'))
        ddhq_df = pd.read_parquet(os.path.join(offline_data_dir, 'ddhq_election_results_cleaned_latest.parquet'))
        
        # Limit rows for testing if specified
        if args.test_rows:
            print(f"🧪 TEST MODE: Limiting to {args.test_rows} rows each")
            hubspot_df = hubspot_df.head(args.test_rows)
            ddhq_df = ddhq_df.head(args.test_rows)
        
        print(f"   HubSpot: {len(hubspot_df):,} records")
        print(f"   DDHQ: {len(ddhq_df):,} records")
        
        # Generate embeddings
        hubspot_with_embeddings = generator.process_hubspot_embeddings(hubspot_df)
        ddhq_with_embeddings = generator.process_ddhq_embeddings(ddhq_df)
        
        # Save results
        hubspot_file, ddhq_file = generator.save_embeddings(hubspot_with_embeddings, ddhq_with_embeddings)
        
        # Get cost summary
        stats = generator.embedding_client.get_cost_stats()
        total_cost = stats.get('total_cost', 0.0)
        
        print(f"\n✅ Embedding generation complete!")
        print(f"   Total cost: ${total_cost:.4f}")
        print(f"   Ready for improved matching with cleaned data")
        print("="*50)
        
    except Exception as e:
        generator.logger.error(f"❌ Embedding generation failed: {str(e)}")
        raise

if __name__ == "__main__":
    main()