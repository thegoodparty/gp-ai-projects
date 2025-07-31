import os
import asyncio
import argparse
import pandas as pd
from datetime import datetime
from typing import List, Optional, Dict
from dataclasses import dataclass, asdict
from .vector_store_generator import VectorStoreGenerator
from .production_matcher import ProductionMatcher
from shared.logger import get_logger

@dataclass
class PipelineConfig:
    """Configuration for the complete BR-L2 matching pipeline"""
    # Vector store generation
    generate_vectors: bool = False
    force_regenerate_vectors: bool = False
    vector_batch_size: int = 100
    states_to_generate: Optional[List[str]] = None
    
    # Production matching
    run_matching: bool = True
    states_to_match: Optional[List[str]] = None
    matching_limit: Optional[int] = None
    matching_batch_size: int = 10
    
    # Output
    output_filename: Optional[str] = None
    include_timestamp: bool = True

@dataclass
class PipelineResults:
    """Results from the complete pipeline execution"""
    # Vector generation results
    vector_generation_completed: bool = False
    states_with_vectors: List[str] = None
    vector_generation_cost: float = 0.0
    vector_generation_time: float = 0.0
    
    # Matching results
    matching_completed: bool = False
    total_records_processed: int = 0
    successful_matches: int = 0
    matching_cost: float = 0.0
    matching_time: float = 0.0
    output_file: str = ""
    
    # Overall
    total_cost: float = 0.0
    total_time: float = 0.0
    success: bool = False

class BRMatchingOrchestrator:
    """Orchestrates the complete BR-L2 matching pipeline"""
    
    def __init__(self):
        self.logger = get_logger(__name__)
        self.generator = VectorStoreGenerator()
        self.matcher = ProductionMatcher()
        
        # US states for reference
        self.us_states = {
            'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
            'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
            'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
            'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
            'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY'
        }

    def validate_config(self, config: PipelineConfig) -> bool:
        """Validate pipeline configuration"""
        if not config.generate_vectors and not config.run_matching:
            self.logger.error("❌ Must enable either vector generation or matching")
            return False
        
        # Validate states
        if config.states_to_generate:
            invalid_states = [s for s in config.states_to_generate if s.upper() not in self.us_states]
            if invalid_states:
                self.logger.error(f"❌ Invalid states for vector generation: {invalid_states}")
                return False
        
        if config.states_to_match:
            invalid_states = [s for s in config.states_to_match if s.upper() not in self.us_states]
            if invalid_states:
                self.logger.error(f"❌ Invalid states for matching: {invalid_states}")
                return False
        
        return True

    async def run_pipeline(self, config: PipelineConfig) -> PipelineResults:
        """Execute the complete BR-L2 matching pipeline"""
        import time
        start_time = time.time()
        
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"BR-L2 DISTRICT MATCHING PIPELINE")
        self.logger.info(f"{'='*80}")
        self.logger.info(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        if not self.validate_config(config):
            return PipelineResults(success=False)
        
        results = PipelineResults()
        
        try:
            # Phase 1: Vector Store Generation
            if config.generate_vectors:
                self.logger.info(f"\n🏗️ PHASE 1: VECTOR STORE GENERATION")
                vector_start = time.time()
                
                generation_results = await self.generator.generate_all_states(
                    batch_size=config.vector_batch_size,
                    force_regenerate=config.force_regenerate_vectors,
                    states_to_process=config.states_to_generate
                )
                
                results.vector_generation_completed = True
                results.vector_generation_time = time.time() - vector_start
                results.states_with_vectors = self.generator.get_existing_vector_stores()
                
                # Calculate vector generation cost
                embedding_stats = self.generator.embedding_client.get_cost_stats()
                results.vector_generation_cost = embedding_stats['total_cost']
                
                self.logger.info(f"✅ Vector generation completed in {results.vector_generation_time:.1f}s")
                self.logger.info(f"💰 Vector generation cost: ${results.vector_generation_cost:.6f}")
                
                # Brief pause between phases
                await asyncio.sleep(2)
            
            # Phase 2: Production Matching
            if config.run_matching:
                self.logger.info(f"\n🎯 PHASE 2: PRODUCTION MATCHING")
                matching_start = time.time()
                
                # Generate output filename
                output_filename = config.output_filename
                if not output_filename:
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S') if config.include_timestamp else ""
                    limit_suffix = f"_limit{config.matching_limit}" if config.matching_limit else ""
                    states_suffix = f"_{'_'.join(config.states_to_match)}" if config.states_to_match else "_all_states"
                    output_filename = f"br_l2_matching{states_suffix}{limit_suffix}_{timestamp}.tsv".replace("__", "_")
                
                # Check if we should run individual state processing
                if config.output_filename and "individual" in config.output_filename:
                    # Run individual state processing
                    output_paths_dict = await self.matcher.run_all_states_individual(
                        batch_size=config.matching_batch_size,
                        output_prefix=config.output_filename
                    )
                    # For compatibility, use the first state's output as primary
                    first_state_paths = next(iter(output_paths_dict.values())) if output_paths_dict else {}
                    output_paths = first_state_paths
                else:
                    # Run standard matching
                    output_paths = await self.matcher.run_production_matching(
                        states=config.states_to_match,
                        limit=config.matching_limit,
                        batch_size=config.matching_batch_size,
                        output_filename=output_filename
                    )
                
                results.matching_completed = True
                results.matching_time = time.time() - matching_start
                results.output_file = output_paths.get('parquet', output_paths.get('tsv', ''))
                results.total_records_processed = self.matcher.stats.total_processed
                results.successful_matches = self.matcher.stats.successful_matches
                results.matching_cost = self.matcher.stats.total_cost
                
                self.logger.info(f"✅ Matching completed in {results.matching_time:.1f}s")
                self.logger.info(f"💰 Matching cost: ${results.matching_cost:.6f}")
            
            # Calculate totals
            results.total_cost = results.vector_generation_cost + results.matching_cost
            results.total_time = time.time() - start_time
            results.success = True
            
            # Print final summary
            self.print_pipeline_summary(results)
            
            return results
            
        except Exception as e:
            self.logger.error(f"❌ Pipeline failed: {e}")
            results.success = False
            results.total_time = time.time() - start_time
            return results

    def print_pipeline_summary(self, results: PipelineResults):
        """Print comprehensive pipeline summary"""
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"PIPELINE EXECUTION SUMMARY")
        self.logger.info(f"{'='*80}")
        
        # Vector generation summary
        if results.vector_generation_completed:
            self.logger.info(f"🏗️ VECTOR GENERATION:")
            self.logger.info(f"  - States with vectors: {len(results.states_with_vectors) if results.states_with_vectors else 0}")
            self.logger.info(f"  - Generation time: {results.vector_generation_time:.1f}s")
            self.logger.info(f"  - Generation cost: ${results.vector_generation_cost:.6f}")
        
        # Matching summary
        if results.matching_completed:
            success_rate = (results.successful_matches / max(1, results.total_records_processed)) * 100
            self.logger.info(f"🎯 PRODUCTION MATCHING:")
            self.logger.info(f"  - Records processed: {results.total_records_processed:,}")
            self.logger.info(f"  - Successful matches: {results.successful_matches:,} ({success_rate:.1f}%)")
            self.logger.info(f"  - Matching time: {results.matching_time:.1f}s")
            self.logger.info(f"  - Matching cost: ${results.matching_cost:.6f}")
            self.logger.info(f"  - Output file: {results.output_file}")
        
        # Overall summary
        self.logger.info(f"📊 OVERALL:")
        self.logger.info(f"  - Total time: {results.total_time:.1f}s")
        self.logger.info(f"  - Total cost: ${results.total_cost:.6f}")
        self.logger.info(f"  - Success: {'✅' if results.success else '❌'}")
        
        if results.total_records_processed > 0:
            cost_per_record = results.total_cost / results.total_records_processed
            records_per_second = results.total_records_processed / max(1, results.matching_time)
            self.logger.info(f"  - Cost per record: ${cost_per_record:.6f}")
            self.logger.info(f"  - Processing rate: {records_per_second:.1f} records/second")

    def save_pipeline_config_and_results(self, config: PipelineConfig, results: PipelineResults, filename: str = None):
        """Save pipeline configuration and results for reproducibility"""
        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"pipeline_execution_{timestamp}.json"
        
        output_dir = os.path.join(os.path.dirname(__file__), "output")
        filepath = os.path.join(output_dir, filename)
        
        data = {
            'execution_timestamp': datetime.now().isoformat(),
            'config': asdict(config),
            'results': asdict(results)
        }
        
        import json
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        
        self.logger.info(f"📁 Pipeline metadata saved: {filepath}")

def create_preset_configs() -> Dict[str, PipelineConfig]:
    """Create preset configurations for common use cases"""
    presets = {
        'generate_all_vectors': PipelineConfig(
            generate_vectors=True,
            force_regenerate_vectors=False,
            vector_batch_size=100,
            run_matching=False
        ),
        
        'test_small': PipelineConfig(
            generate_vectors=False,
            run_matching=True,
            states_to_match=['CA', 'NY', 'TX'],
            matching_limit=50,
            matching_batch_size=5,
            output_filename="test_small_matching.tsv"
        ),
        
        'test_medium': PipelineConfig(
            generate_vectors=False,
            run_matching=True,
            states_to_match=['CA', 'NY', 'TX', 'FL', 'PA'],
            matching_limit=500,
            matching_batch_size=10,
            output_filename="test_medium_matching.tsv"
        ),
        
        'production_all': PipelineConfig(
            generate_vectors=True,
            force_regenerate_vectors=False,
            run_matching=True,
            matching_batch_size=20,
            output_filename="production_all_states_matching.tsv"
        ),
        
        'production_high_volume_states': PipelineConfig(
            generate_vectors=False,
            run_matching=True,
            states_to_match=['CA', 'TX', 'FL', 'NY', 'PA', 'IL', 'OH', 'GA', 'NC', 'MI'],
            matching_batch_size=15,
            output_filename="production_high_volume_states.tsv"
        ),
        
        'all_states_individual': PipelineConfig(
            generate_vectors=False,
            run_matching=True,
            matching_batch_size=50,  # Aggressive concurrency within each state
            output_filename="individual_state_matching"
        )
    }
    
    return presets

async def main():
    """Main entry point with CLI argument parsing"""
    parser = argparse.ArgumentParser(description='BR-L2 District Matching Pipeline')
    
    # Preset configurations
    parser.add_argument('--preset', choices=['generate_all_vectors', 'test_small', 'test_medium', 'production_all', 'production_high_volume_states', 'all_states_individual'],
                       help='Use a preset configuration')
    
    # Vector generation options
    parser.add_argument('--generate-vectors', action='store_true', help='Generate vector stores')
    parser.add_argument('--force-regenerate', action='store_true', help='Force regenerate existing vectors')
    parser.add_argument('--vector-batch-size', type=int, default=100, help='Batch size for vector generation')
    parser.add_argument('--vector-states', nargs='+', help='States to generate vectors for')
    
    # Matching options
    parser.add_argument('--skip-matching', action='store_true', help='Skip the matching phase')
    parser.add_argument('--states', nargs='+', help='States to process for matching')
    parser.add_argument('--limit', type=int, help='Limit number of records to process')
    parser.add_argument('--batch-size', type=int, default=10, help='Batch size for matching')
    parser.add_argument('--output', help='Output filename')
    
    # Utility options
    parser.add_argument('--list-vectors', action='store_true', help='List available vector stores and exit')
    parser.add_argument('--no-timestamp', action='store_true', help='Do not include timestamp in output filename')
    
    args = parser.parse_args()
    
    orchestrator = BRMatchingOrchestrator()
    
    # Handle utility commands
    if args.list_vectors:
        orchestrator.generator.list_vector_stores()
        return
    
    # Determine configuration
    if args.preset:
        presets = create_preset_configs()
        config = presets[args.preset]
        orchestrator.logger.info(f"🎯 Using preset configuration: {args.preset}")
    else:
        # Build config from CLI args
        config = PipelineConfig(
            generate_vectors=args.generate_vectors,
            force_regenerate_vectors=args.force_regenerate,
            vector_batch_size=args.vector_batch_size,
            states_to_generate=args.vector_states,
            run_matching=not args.skip_matching,
            states_to_match=args.states,
            matching_limit=args.limit,
            matching_batch_size=args.batch_size,
            output_filename=args.output,
            include_timestamp=not args.no_timestamp
        )
    
    # Override specific settings if provided
    if args.states:
        config.states_to_match = args.states
    if args.limit:
        config.matching_limit = args.limit
    if args.output:
        config.output_filename = args.output
    
    # Run pipeline
    results = await orchestrator.run_pipeline(config)
    
    # Save execution metadata
    orchestrator.save_pipeline_config_and_results(config, results)
    
    # Exit with appropriate code
    exit(0 if results.success else 1)

if __name__ == "__main__":
    asyncio.run(main())