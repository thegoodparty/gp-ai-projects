"""
Production Gold Data Generation Package

This package contains the production-ready components for generating
BR-L2 district matching data at scale across all US states + DC.

Key Components:
- vector_store_generator.py: Generate embeddings for all 51 jurisdictions
- production_matcher.py: Production BR-L2 matching using pre-built vector stores
- orchestrator.py: Coordinates the complete pipeline
- cost_tracker.py: Cost tracking utilities for monitoring usage

Usage:
    # Generate vector stores for all states + DC
    uv run stitch_golden_data/prod_gold_data/vector_store_generator.py
    
    # Run production matching
    uv run stitch_golden_data/prod_gold_data/production_matcher.py
    
    # Run complete pipeline
    uv run stitch_golden_data/prod_gold_data/orchestrator.py
"""

from .vector_store_generator import VectorStoreGenerator
from .production_matcher import ProductionMatcher
from .orchestrator import PipelineConfig
from .cost_tracker import CostRecord, CostSummary

__all__ = [
    'VectorStoreGenerator',
    'ProductionMatcher', 
    'PipelineConfig',
    'CostRecord',
    'CostSummary'
]