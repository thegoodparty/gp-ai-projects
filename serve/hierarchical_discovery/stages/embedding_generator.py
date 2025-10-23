#!/usr/bin/env python3

import asyncio
import pickle
import uuid
import os
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
import numpy as np
from sklearn.decomposition import PCA
import umap
from concurrent.futures import ThreadPoolExecutor

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    # Look for .env file in project root (two levels up from this file)
    env_path = Path(__file__).parent.parent.parent.parent / '.env'
    if env_path.exists():
        load_dotenv(env_path)
        print(f"Loaded environment variables from {env_path}")
    else:
        print(f"No .env file found at {env_path}")
except ImportError:
    print("python-dotenv not available, using system environment variables")

from shared.llm_gemini import GeminiEmbeddingClient
from shared.logger import get_logger
from ..models import AtomicMessage, EmbeddedMessage, EmbeddingData, PipelineConfig, UsageStats

logger = get_logger(__name__)

class EmbeddingGenerator:
    """Generate embeddings with dimensionality reduction"""

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.embedding_config = config.embeddings

        # Initialize embedding client
        batch_size = self.embedding_config.get("batch_size", 100)
        self.embedding_client = GeminiEmbeddingClient()

        # Thread pool for CPU-intensive operations
        self.thread_pool = ThreadPoolExecutor(max_workers=4)

        # Dimensionality reduction models
        self.pca_model = None
        self.umap_model = None

        logger.debug("EmbeddingGenerator initialized")

    async def generate_embeddings_batch(self, texts: List[str]) -> List[np.ndarray]:
        """Generate embeddings for a batch of texts"""
        batch_size = self.embedding_config.get("batch_size", 100)

        logger.debug(f"Generating embeddings for {len(texts)} texts")

        try:
            # Check if we have the API key
            api_key = os.getenv('GEMINI_API_KEY')
            if not api_key:
                logger.error("GEMINI_API_KEY not found in environment variables")
                raise ValueError("GEMINI_API_KEY not set")

            logger.debug(f"GEMINI_API_KEY found, generating embeddings with {batch_size} batch size")

            # Use the async _create_embeddings_parallel method directly to avoid asyncio.run() conflict
            embeddings = await self.embedding_client._create_embeddings_parallel(
                texts=texts,
                batch_size=batch_size,
                max_concurrent_batches=2
            )

            logger.debug(f"Embeddings generated successfully: {type(embeddings)}, shape: {embeddings.shape if hasattr(embeddings, 'shape') else len(embeddings)}")

            # Convert to list of arrays if it's a 2D numpy array
            if isinstance(embeddings, np.ndarray) and len(embeddings.shape) == 2:
                embeddings = [embeddings[i] for i in range(embeddings.shape[0])]

            # Verify we got real embeddings (not all zeros)
            if isinstance(embeddings, list) and len(embeddings) > 0:
                first_embedding = embeddings[0]
                if isinstance(first_embedding, np.ndarray):
                    if np.allclose(first_embedding, 0):
                        logger.warning("Generated embeddings appear to be all zeros!")
                    else:
                        logger.debug(f"Real embeddings generated! First embedding non-zero sum: {np.sum(np.abs(first_embedding)):.3f}")

            return embeddings
        except Exception as e:
            logger.error(f"Failed to generate embeddings: {e}")
            logger.error(f"Exception type: {type(e)}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            # Add zero embeddings as fallback
            logger.warning(f"Falling back to zero embeddings for {len(texts)} texts")
            return [np.zeros(3072) for _ in texts]

    def fit_dimensionality_reduction(self, embeddings_3072d: np.ndarray) -> Dict[str, Any]:
        """Fit PCA and UMAP models on 3072D embeddings"""
        logger.debug("Fitting dimensionality reduction models...")
        models = {}

        # PCA reduction
        if self.embedding_config.get("pca_dimensions", 100) > 0:
            pca_dims = self.embedding_config.get("pca_dimensions", 100)
            n_samples = embeddings_3072d.shape[0]
            n_features = embeddings_3072d.shape[1]
            max_components = min(n_samples, n_features)

            if pca_dims > max_components:
                logger.warning(f"PCA dimensions ({pca_dims}) exceed dataset size ({max_components}). "
                             f"Reducing to {max_components - 1}")
                pca_dims = max_components - 1

            logger.debug(f"Fitting PCA model for {pca_dims} dimensions")

            self.pca_model = PCA(n_components=pca_dims, random_state=42)
            embeddings_pca = self.pca_model.fit_transform(embeddings_3072d)

            models['pca'] = self.pca_model
            logger.debug(f"PCA explained variance ratio: {self.pca_model.explained_variance_ratio_.sum():.3f}")

        # UMAP reduction (only for visualization - not used in hierarchical clustering)
        if self.embedding_config.get("umap_dimensions", 15) > 0:
            umap_dims = self.embedding_config.get("umap_dimensions", 15)
            umap_params = self.embedding_config.get("umap_params", {})
            dataset_size = embeddings_3072d.shape[0]

            # Skip UMAP for very small datasets (< 15 samples) as it's unreliable and only used for visualization
            if dataset_size < 15:
                logger.warning(f"Skipping UMAP reduction - dataset too small ({dataset_size} samples). "
                             f"UMAP requires at least 15 samples. Only used for visualization anyway.")
            else:
                # Dynamic n_neighbors calculation based on dataset size for optimal clustering
                static_n_neighbors = umap_params.get("n_neighbors", 15)

                # Linear scaling formula: 2.5% of dataset size, bounded between 5-30
                dynamic_n_neighbors = max(5, min(30, int(dataset_size * 0.025)))

                logger.debug(f"UMAP n_neighbors scaling: dataset_size={dataset_size}, "
                           f"static={static_n_neighbors}, dynamic={dynamic_n_neighbors}")
                logger.debug(f"Fitting UMAP model for {umap_dims} dimensions with n_neighbors={dynamic_n_neighbors}")

                self.umap_model = umap.UMAP(
                    n_components=umap_dims,
                    n_neighbors=dynamic_n_neighbors,  # Use dynamic value instead of static config
                    min_dist=umap_params.get("min_dist", 0.1),
                    metric=umap_params.get("metric", "cosine"),
                    verbose=False,
                    n_jobs=4  # Enable multithreading for 2-3x speedup
                )

                # For UMAP, use PCA embeddings if available, otherwise original
                input_embeddings = embeddings_pca if 'pca' in models else embeddings_3072d
                embeddings_umap = self.umap_model.fit_transform(input_embeddings)

                models['umap'] = self.umap_model
                logger.debug(f"UMAP reduction completed with dynamic n_neighbors={dynamic_n_neighbors}")

        return models

    def apply_dimensionality_reduction(self, embeddings_3072d: np.ndarray, models: Dict[str, Any]) -> Dict[str, np.ndarray]:
        """Apply trained dimensionality reduction models"""
        reduced_embeddings = {'3072d': embeddings_3072d}

        # Apply PCA
        if 'pca' in models and self.embedding_config.get("pca_dimensions", 100) > 0:
            logger.debug("Applying PCA reduction...")
            embeddings_pca = models['pca'].transform(embeddings_3072d)
            reduced_embeddings['pca'] = embeddings_pca

        # Apply UMAP
        if 'umap' in models and self.embedding_config.get("umap_dimensions", 15) > 0:
            logger.debug("Applying UMAP reduction...")
            # Use PCA embeddings as input to UMAP if available
            input_embeddings = reduced_embeddings.get('pca', embeddings_3072d)
            embeddings_umap = models['umap'].transform(input_embeddings)
            reduced_embeddings['umap'] = embeddings_umap

        return reduced_embeddings

    async def generate_all_embeddings(self, atomic_messages: List[AtomicMessage]) -> Dict[str, np.ndarray]:
        """Generate 3072d embeddings only (no dimensionality reduction)"""
        if not atomic_messages:
            return {}

        logger.debug("Generating 3072d embeddings (no prefix)...")
        texts = [msg.atomic_text for msg in atomic_messages]

        logger.info(f"EMBEDDING CHECK - First 3 texts being embedded:")
        for i, text in enumerate(texts[:3]):
            logger.info(f"  [{i}] '{text[:100]}...'")

        embeddings_3072d_list = await self.generate_embeddings_batch(texts)
        embeddings_3072d = np.array(embeddings_3072d_list)

        embeddings_by_text = {}
        for i, msg in enumerate(atomic_messages):
            embeddings_by_text[msg.atomic_text] = embeddings_3072d[i]

        return embeddings_by_text

    def create_embedded_messages(self, atomic_messages: List[AtomicMessage],
                               embeddings_by_text: Dict[str, np.ndarray]) -> List[EmbeddedMessage]:
        """Create EmbeddedMessage objects with 3072d embeddings only"""
        embedded_messages = []

        for atomic_message in atomic_messages:
            text = atomic_message.atomic_text

            if text not in embeddings_by_text:
                logger.warning(f"No embedding found for text: {text[:50]}...")
                continue

            embedding_data = EmbeddingData(
                embedding_3072d=embeddings_by_text[text],
                embedding_model=self.embedding_config.get("model", "gemini"),
                generation_timestamp=datetime.now()
            )

            embedded_message = EmbeddedMessage(
                id=str(uuid.uuid4()),
                atomic_message_id=atomic_message.id,
                csv_file=atomic_message.csv_file,
                csv_row_index=atomic_message.csv_row_index,
                text=atomic_message.atomic_text,
                original_text=atomic_message.original_text,
                embeddings=embedding_data,
                campaign_source=atomic_message.campaign_source,
                metadata=atomic_message.metadata,
                created_at=datetime.now()
            )

            if len(embedded_messages) < 5:
                phone = atomic_message.metadata.get("Contact Phone Number", "NOT_FOUND")
                sent_at = atomic_message.metadata.get("Sent At", "NOT_FOUND")
                logger.debug(f"EMBEDDING MESSAGE {len(embedded_messages)} (csv_row={atomic_message.csv_row_index}):")
                logger.debug(f"  phone='{phone}', sent_at='{sent_at}'")
                logger.debug(f"  text_snippet='{atomic_message.atomic_text[:50]}...'")
                logger.debug(f"  atomic_metadata_keys={list(atomic_message.metadata.keys())}")
                logger.debug(f"  embedded_metadata_keys={list(embedded_message.metadata.keys())}")

            embedded_messages.append(embedded_message)

        return embedded_messages

    def generate_embedding_report(self, embedded_messages: List[EmbeddedMessage]) -> str:
        """Generate human-readable embedding report"""
        if not embedded_messages:
            return "Embedding Report: No embedded messages generated"

        total_messages = len(embedded_messages)

        has_3072d = sum(1 for msg in embedded_messages if msg.embeddings.embedding_3072d is not None)

        campaign_stats = {}
        for msg in embedded_messages:
            campaign = msg.campaign_source
            campaign_stats[campaign] = campaign_stats.get(campaign, 0) + 1

        report_lines = [
            "Embedding Generation Report:",
            f"  Total Embedded Messages: {total_messages:,}",
            "",
            "Embedding Dimensions:",
            f"  3072D (Gemini): {has_3072d:,} ({has_3072d/total_messages:.1%})",
            "",
            "Campaign Distribution:"
        ]

        for campaign, count in sorted(campaign_stats.items(), key=lambda x: x[1], reverse=True):
            percentage = (count / total_messages) * 100
            report_lines.append(f"  {campaign}: {count:,} ({percentage:.1f}%)")

        return "\n".join(report_lines)

    def get_usage_stats(self) -> UsageStats:
        """Get embedding generation usage statistics"""
        if hasattr(self.embedding_client, 'get_cost_stats'):
            return self.embedding_client.get_cost_stats()
        elif hasattr(self.embedding_client, 'get_usage_stats'):
            return self.embedding_client.get_usage_stats()
        return {
            'api_call_count': 0,
            'total_tokens': 0,
            'total_cost': 0.0
        }

async def embedding_generator_stage(atomic_messages: List[AtomicMessage], config: PipelineConfig) -> List[EmbeddedMessage]:
    """Main entry point for embedding generation stage"""
    logger.info("=== EMBEDDING GENERATION STAGE ===")

    if not atomic_messages:
        logger.warning("No atomic messages provided")
        return []

    try:
        generator = EmbeddingGenerator(config)

        # Generate embeddings
        embeddings_by_text = await generator.generate_all_embeddings(atomic_messages)

        # Create embedded messages
        embedded_messages = generator.create_embedded_messages(atomic_messages, embeddings_by_text)

        # Generate and log report
        report = generator.generate_embedding_report(embedded_messages)
        logger.info(f"\n{report}")

        # Log usage statistics
        usage_stats = generator.get_usage_stats()
        if usage_stats:
            # Handle both cost_stats (GeminiEmbeddingClient) and usage_stats (GeminiClient) formats
            total_calls = usage_stats.get('total_embeddings_created', usage_stats.get('api_call_count', 0))
            total_tokens = usage_stats.get('total_input_tokens', usage_stats.get('total_tokens', 0))
            total_cost = usage_stats.get('total_cost', 0)

            logger.info(f"Embedding API Usage - Calls: {total_calls}, "
                       f"Tokens: {total_tokens:,}, "
                       f"Cost: ${total_cost:.4f}")

        # Return results with cost information
        return {
            "messages": embedded_messages,
            "cost": usage_stats.get('total_cost', 0) if usage_stats else 0,
            "usage_stats": usage_stats or {}
        }

    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        raise

def embedding_generator_stage_sync(atomic_messages: List[AtomicMessage], config: PipelineConfig) -> List[EmbeddedMessage]:
    """Synchronous wrapper for embedding generation stage"""
    try:
        loop = asyncio.get_running_loop()
        # Already in event loop, run in separate thread
        import concurrent.futures

        def run_in_new_loop():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                return new_loop.run_until_complete(embedding_generator_stage(atomic_messages, config))
            finally:
                new_loop.close()

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(run_in_new_loop)
            return future.result()

    except RuntimeError:
        # No event loop running
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(embedding_generator_stage(atomic_messages, config))
        finally:
            loop.close()