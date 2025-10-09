#!/usr/bin/env python3

"""
EMBEDDING GENERATION FOR HUBSPOT-GOOGLE SHEETS MATCHING

Generates semantic embeddings for race/office name matching:
- HubSpot: Office name embeddings
- Google Sheets: Race name embeddings

Uses Gemini embeddings with high-throughput parallel processing.

USAGE:
# Test mode (50 records)
ENVIRONMENT=test BATCH_SIZE=150 MAX_WORKERS=400 uv run generate_embeddings.py

# Production mode (full dataset)
ENVIRONMENT=production BATCH_SIZE=150 MAX_WORKERS=400 uv run generate_embeddings.py
"""

import sys
import os
import pandas as pd
import numpy as np
from datetime import datetime
from typing import List

sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))
from shared.logger import get_logger
from shared.llm_gemini import GeminiEmbeddingClient

class EmbeddingGenerator:
    def __init__(self, batch_size: int = 150, max_workers: int = 400):
        self.logger = get_logger(__name__)
        self.batch_size = batch_size
        self.max_workers = max_workers

        self.logger.info(f"Initializing embedding generator:")
        self.logger.info(f"   - Batch size: {self.batch_size}")
        self.logger.info(f"   - Max workers: {self.max_workers}")

        self.embedding_client = GeminiEmbeddingClient()

    def create_hubspot_embedding_text(self, row: pd.Series) -> str:
        """Create embedding text for HubSpot office name with city context"""
        office = row.get('office_name', '')
        if pd.isna(office) or office.strip() == '':
            office = row.get('official_office_name', '')

        city = row.get('city', '')

        if pd.notna(city) and city.strip() != '':
            return f"city:{city.strip()} | race:{str(office).strip()}"
        else:
            return str(office).strip()

    def create_google_sheets_embedding_text(self, row: pd.Series) -> str:
        """Create embedding text for Google Sheets race name with consistent format"""
        race = row.get('race_name', '')
        return f"race:{str(race).strip()}"

    def generate_embeddings_batch(self, texts: List[str]) -> np.ndarray:
        """Generate embeddings for a batch of texts"""
        try:
            embeddings = self.embedding_client.create_embeddings(
                texts,
                parallel=True,
                batch_size=self.batch_size,
                max_concurrent_batches=self.max_workers
            )
            return np.array(embeddings)
        except Exception as e:
            self.logger.error(f"Failed to generate embeddings: {str(e)}")
            raise

    def generate_hubspot_embeddings(self, df: pd.DataFrame, limit: int = None) -> pd.DataFrame:
        """Generate embeddings for HubSpot companies"""
        self.logger.info("🔮 Generating HubSpot office embeddings...")

        df = df.copy()

        if limit:
            self.logger.info(f"   - Limiting to first {limit} records for testing")
            df = df.head(limit)

        self.logger.info(f"   - Processing {len(df):,} HubSpot records")

        df['embedding_text'] = df.apply(self.create_hubspot_embedding_text, axis=1)

        texts = df['embedding_text'].tolist()
        self.logger.info(f"   - Generating embeddings for {len(texts):,} texts...")

        embeddings = self.generate_embeddings_batch(texts)

        df['embedding'] = list(embeddings)

        self.logger.info(f"✅ HubSpot embeddings generated: {len(df):,} records")

        return df

    def generate_google_sheets_embeddings(self, df: pd.DataFrame) -> pd.DataFrame:
        """Generate embeddings for Google Sheets races"""
        self.logger.info("🔮 Generating Google Sheets race embeddings...")

        df = df.copy()

        self.logger.info(f"   - Processing {len(df):,} Google Sheets races")

        df['embedding_text'] = df.apply(self.create_google_sheets_embedding_text, axis=1)

        texts = df['embedding_text'].tolist()
        self.logger.info(f"   - Generating embeddings for {len(texts):,} texts...")

        embeddings = self.generate_embeddings_batch(texts)

        df['embedding'] = list(embeddings)

        self.logger.info(f"✅ Google Sheets embeddings generated: {len(df):,} records")

        return df

    def save_data_with_embeddings(self, hubspot_df: pd.DataFrame, google_sheets_df: pd.DataFrame):
        """Save datasets with embeddings"""
        self.logger.info("💾 Saving data with embeddings...")

        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        hubspot_parquet = os.path.join(offline_data_dir, "hubspot_companies_with_embeddings_latest.parquet")
        hubspot_tsv = os.path.join(offline_data_dir, f"hubspot_companies_with_embeddings_{timestamp}.tsv")

        google_parquet = os.path.join(offline_data_dir, "google_sheets_races_with_embeddings_latest.parquet")
        google_tsv = os.path.join(offline_data_dir, f"google_sheets_races_with_embeddings_{timestamp}.tsv")

        hubspot_df_save = hubspot_df.copy()
        hubspot_df_save['embedding_str'] = hubspot_df_save['embedding'].apply(lambda x: str(x.tolist()) if isinstance(x, np.ndarray) else str(x))
        hubspot_df_save.drop('embedding', axis=1).to_csv(hubspot_tsv, sep='\t', index=False)

        hubspot_df.to_parquet(hubspot_parquet, index=False)
        self.logger.info(f"✅ HubSpot data with embeddings saved:")
        self.logger.info(f"   - {hubspot_parquet}")

        google_sheets_df_save = google_sheets_df.copy()
        google_sheets_df_save['embedding_str'] = google_sheets_df_save['embedding'].apply(lambda x: str(x.tolist()) if isinstance(x, np.ndarray) else str(x))
        google_sheets_df_save.drop('embedding', axis=1).to_csv(google_tsv, sep='\t', index=False)

        google_sheets_df.to_parquet(google_parquet, index=False)
        self.logger.info(f"✅ Google Sheets data with embeddings saved:")
        self.logger.info(f"   - {google_parquet}")

    def run(self, test_mode: bool = False):
        """Execute embedding generation pipeline"""
        self.logger.info("🚀 Starting embedding generation...")

        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")

        hubspot_filtered = pd.read_parquet(
            os.path.join(offline_data_dir, "hubspot_companies_filtered_latest.parquet")
        )
        google_sheets_cleaned = pd.read_parquet(
            os.path.join(offline_data_dir, "google_sheets_races_cleaned_latest.parquet")
        )

        self.logger.info(f"   - Loaded {len(hubspot_filtered):,} HubSpot records")
        self.logger.info(f"   - Loaded {len(google_sheets_cleaned):,} Google Sheets races")

        limit = 50 if test_mode else None

        hubspot_with_embeddings = self.generate_hubspot_embeddings(hubspot_filtered, limit=limit)
        google_sheets_with_embeddings = self.generate_google_sheets_embeddings(google_sheets_cleaned)

        self.save_data_with_embeddings(hubspot_with_embeddings, google_sheets_with_embeddings)

        self.logger.info("✅ Embedding generation complete!")
        self.logger.info(f"   - HubSpot records with embeddings: {len(hubspot_with_embeddings):,}")
        self.logger.info(f"   - Google Sheets races with embeddings: {len(google_sheets_with_embeddings):,}")


def main():
    """Main execution"""
    print("="*80)
    print("HUBSPOT-GOOGLE SHEETS EMBEDDING GENERATION")
    print("="*80)

    try:
        batch_size = int(os.getenv('BATCH_SIZE', 150))
        max_workers = int(os.getenv('MAX_WORKERS', 400))

        environment = os.getenv('ENVIRONMENT', '').lower()
        test_mode = environment == 'test'

        if test_mode:
            print("🧪 Running in TEST mode (limited to 50 records)")

        generator = EmbeddingGenerator(batch_size=batch_size, max_workers=max_workers)
        generator.run(test_mode=test_mode)

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        raise


if __name__ == "__main__":
    main()
