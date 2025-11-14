#!/usr/bin/env python3

"""
HUBSPOT-DDHQ MATCHING PIPELINE RUNNER

Runs all 6 steps of the matching pipeline in order:
1. Data Extraction from Databricks
2. Data Cleaning (name standardization, election expansion)
3. Temporal Filtering (align HubSpot with DDHQ dates)
4. Embedding Generation (semantic embeddings with Gemini)
5. Production Matching (FAISS + LLM with lazy-loading + GC)
6. Runoff Enrichment (discover and match runoffs from DDHQ)

USAGE:
    # Development mode (200 test records)
    ENVIRONMENT=development uv run hubspot_ddhq_match/run_pipeline.py

    # Production mode (full dataset)
    ENVIRONMENT=production uv run hubspot_ddhq_match/run_pipeline.py

    # Skip specific steps
    uv run hubspot_ddhq_match/run_pipeline.py --skip-extraction --skip-cleaning

    # Custom record limit
    MAX_RECORDS=500 uv run hubspot_ddhq_match/run_pipeline.py
"""

import sys
import os
import argparse
import subprocess
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from shared.logger import get_logger

class PipelineRunner:
    def __init__(self, skip_steps=None):
        self.logger = get_logger(__name__)
        self.skip_steps = skip_steps or []
        self.start_time = datetime.now()

        self.environment = os.getenv('ENVIRONMENT', 'production').lower()
        self.max_records = os.getenv('MAX_RECORDS', None)
        self.run_id = os.getenv('RUN_ID', None)
        self.s3_output_prefix = os.getenv('S3_OUTPUT_PREFIX', None)

        self.embedding_batch_size = os.getenv('EMBEDDING_BATCH_SIZE', '100')
        self.embedding_max_workers = os.getenv('EMBEDDING_MAX_WORKERS', '80')
        self.matching_batch_size = os.getenv('MATCHING_BATCH_SIZE', '1000')
        self.matching_max_workers = os.getenv('MATCHING_MAX_WORKERS', '2000')

        print("=" * 70)
        print("🚀 HUBSPOT-DDHQ MATCHING PIPELINE RUNNER")
        print("=" * 70)
        print(f"Environment: {self.environment.upper()}")
        if self.run_id:
            print(f"Run ID: {self.run_id}")
        if self.s3_output_prefix:
            print(f"S3 Output Prefix: {self.s3_output_prefix}")
        if self.max_records:
            print(f"Max Records: {self.max_records}")
        print(f"Embedding: batch_size={self.embedding_batch_size}, max_workers={self.embedding_max_workers}")
        print(f"Matching: batch_size={self.matching_batch_size}, max_workers={self.matching_max_workers}")
        if self.skip_steps:
            print(f"Skipping steps: {', '.join(self.skip_steps)}")
        print(f"Started at: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)
        print()

    def run_step(self, step_num: int, step_name: str, script_path: str, env_vars: dict = None) -> bool:
        """Run a single pipeline step with error handling"""

        if step_name.lower().replace(' ', '_') in self.skip_steps:
            print(f"⏭️  STEP {step_num}: {step_name} - SKIPPED")
            print()
            return True

        print(f"▶️  STEP {step_num}/6: {step_name}")
        print(f"   Script: {script_path}")

        step_start = datetime.now()

        try:
            # Build command (use python directly if in Docker, otherwise use uv)
            if os.path.exists('/.dockerenv') or os.getenv('ECS_CONTAINER_METADATA_URI'):
                cmd = ['python', script_path]
            else:
                cmd = ['uv', 'run', script_path]

            # Set up environment variables
            env = os.environ.copy()
            if env_vars:
                env.update(env_vars)

            # Run the script
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=False,  # Show output in real-time
                text=True
            )

            step_duration = (datetime.now() - step_start).total_seconds()

            if result.returncode == 0:
                print(f"✅ STEP {step_num} COMPLETE ({step_duration:.1f}s)")
                print()
                return True
            else:
                print(f"❌ STEP {step_num} FAILED (exit code: {result.returncode})")
                print()
                return False

        except Exception as e:
            step_duration = (datetime.now() - step_start).total_seconds()
            print(f"❌ STEP {step_num} ERROR: {str(e)} ({step_duration:.1f}s)")
            print()
            return False

    def run_pipeline(self) -> bool:
        """Run all pipeline steps in order"""

        # Get the directory where this script is located
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # Step 1: Data Extraction
        success = self.run_step(
            step_num=1,
            step_name="Data Extraction",
            script_path=os.path.join(script_dir, 'data_extraction.py')
        )
        if not success:
            return False

        # Step 2: Data Cleaning
        success = self.run_step(
            step_num=2,
            step_name="Data Cleaning",
            script_path=os.path.join(script_dir, 'data_cleaning.py')
        )
        if not success:
            return False

        # Step 3: Temporal Filtering
        success = self.run_step(
            step_num=3,
            step_name="Temporal Filtering",
            script_path=os.path.join(script_dir, 'temporal_filtering.py')
        )
        if not success:
            return False

        # Step 4: Embedding Generation
        success = self.run_step(
            step_num=4,
            step_name="Embedding Generation",
            script_path=os.path.join(script_dir, 'generate_cleaned_embeddings.py'),
            env_vars={
                'ENVIRONMENT': self.environment,
                'BATCH_SIZE': self.embedding_batch_size,
                'MAX_WORKERS': self.embedding_max_workers
            }
        )
        if not success:
            return False

        # Step 5: Production Matching
        env_vars = {
            'ENVIRONMENT': self.environment,
            'BATCH_SIZE': self.matching_batch_size,
            'MAX_WORKERS': self.matching_max_workers
        }
        if self.max_records:
            env_vars['MAX_RECORDS'] = self.max_records

        success = self.run_step(
            step_num=5,
            step_name="Production Matching",
            script_path=os.path.join(script_dir, 'parallel_production_matcher.py'),
            env_vars=env_vars
        )
        if not success:
            return False

        # Step 6: Runoff Enrichment
        success = self.run_step(
            step_num=6,
            step_name="Runoff Enrichment",
            script_path=os.path.join(script_dir, 'enrich_runoffs.py'),
            env_vars={
                'ENVIRONMENT': self.environment
            }
        )
        if not success:
            return False

        return True

    def print_summary(self, success: bool):
        """Print pipeline execution summary"""
        total_duration = (datetime.now() - self.start_time).total_seconds()

        print()
        print("=" * 70)
        if success:
            print("🎉 PIPELINE COMPLETE!")
        else:
            print("❌ PIPELINE FAILED")
        print("=" * 70)
        if self.run_id:
            print(f"Run ID: {self.run_id}")
        if self.s3_output_prefix:
            print(f"S3 Output Prefix: {self.s3_output_prefix}")
        print(f"Total Duration: {total_duration:.1f}s ({total_duration/60:.1f} minutes)")
        print(f"Finished at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        if success:
            # Show output file locations
            output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
            print()
            print("📊 Results saved to:")
            print(f"   {output_dir}/matches.parquet (uploaded to S3)")
            print(f"   {output_dir}/discovered_runoffs_latest.parquet (local only)")

        print("=" * 70)

def main():
    parser = argparse.ArgumentParser(description='Run HubSpot-DDHQ matching pipeline')
    parser.add_argument('--skip-extraction', action='store_true', help='Skip data extraction step')
    parser.add_argument('--skip-cleaning', action='store_true', help='Skip data cleaning step')
    parser.add_argument('--skip-temporal', action='store_true', help='Skip temporal filtering step')
    parser.add_argument('--skip-embeddings', action='store_true', help='Skip embedding generation step')
    parser.add_argument('--skip-matching', action='store_true', help='Skip matching step')
    parser.add_argument('--skip-runoff-enrichment', action='store_true', help='Skip runoff enrichment step')

    args = parser.parse_args()

    # Build list of steps to skip
    skip_steps = []
    if args.skip_extraction:
        skip_steps.append('data_extraction')
    if args.skip_cleaning:
        skip_steps.append('data_cleaning')
    if args.skip_temporal:
        skip_steps.append('temporal_filtering')
    if args.skip_embeddings:
        skip_steps.append('embedding_generation')
    if args.skip_matching:
        skip_steps.append('production_matching')
    if args.skip_runoff_enrichment:
        skip_steps.append('runoff_enrichment')

    # Run pipeline
    runner = PipelineRunner(skip_steps=skip_steps)
    success = runner.run_pipeline()
    runner.print_summary(success)

    # Exit with appropriate code
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
