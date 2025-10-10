#!/usr/bin/env python3

"""
COMPLETE HUBSPOT-GOOGLE SHEETS MATCHING PIPELINE

Runs all 5 steps in sequence:
1. Data Extraction
2. Data Cleaning
3. Temporal Filtering
4. Embedding Generation
5. Production Matching

USAGE:
# Test mode (50 HubSpot records for embeddings and matching)
ENVIRONMENT=test uv run run_full_pipeline.py

# Production mode (all records)
ENVIRONMENT=production uv run run_full_pipeline.py
"""

import sys
import os
import subprocess
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))
from shared.logger import get_logger

class PipelineRunner:
    def __init__(self, environment: str = 'production'):
        self.logger = get_logger(__name__)
        self.environment = environment
        self.current_dir = os.path.dirname(os.path.abspath(__file__))

        self.steps = [
            {
                'name': 'Data Extraction',
                'script': 'data_extraction.py',
                'description': 'Extract HubSpot companies and Google Sheets races'
            },
            {
                'name': 'Data Cleaning',
                'script': 'data_cleaning.py',
                'description': 'Clean and normalize both datasets'
            },
            {
                'name': 'Temporal Filtering',
                'script': 'temporal_filtering.py',
                'description': 'Filter HubSpot by dates in Google Sheets'
            },
            {
                'name': 'Embedding Generation',
                'script': 'generate_embeddings.py',
                'description': 'Generate semantic embeddings'
            },
            {
                'name': 'Production Matching',
                'script': 'parallel_production_matcher.py',
                'description': 'FAISS + LLM matching'
            }
        ]

    def run_step(self, step_num: int, step: dict) -> bool:
        """Run a single pipeline step"""
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"STEP {step_num}/5: {step['name']}")
        self.logger.info(f"{'='*80}")
        self.logger.info(f"Description: {step['description']}")
        self.logger.info(f"Script: {step['script']}")
        self.logger.info("")

        script_path = os.path.join(self.current_dir, step['script'])

        if not os.path.exists(script_path):
            self.logger.error(f"Script not found: {script_path}")
            return False

        env = os.environ.copy()
        env['ENVIRONMENT'] = self.environment

        if step['script'] in ['generate_embeddings.py', 'parallel_production_matcher.py']:
            env['BATCH_SIZE'] = os.getenv('BATCH_SIZE', '150' if step['script'] == 'generate_embeddings.py' else '1000')
            env['MAX_WORKERS'] = os.getenv('MAX_WORKERS', '400' if step['script'] == 'generate_embeddings.py' else '1500')

        try:
            result = subprocess.run(
                ['uv', 'run', script_path],
                cwd=self.current_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=3600
            )

            if result.returncode != 0:
                self.logger.error(f"Step {step_num} failed with exit code {result.returncode}")
                self.logger.error(f"STDERR: {result.stderr}")
                return False

            self.logger.info(f"✅ Step {step_num} completed successfully")
            return True

        except subprocess.TimeoutExpired:
            self.logger.error(f"Step {step_num} timed out after 1 hour")
            return False
        except Exception as e:
            self.logger.error(f"Step {step_num} failed with error: {str(e)}")
            return False

    def run(self):
        """Execute complete pipeline"""
        start_time = datetime.now()

        self.logger.info("\n" + "="*80)
        self.logger.info("HUBSPOT-GOOGLE SHEETS MATCHING PIPELINE")
        self.logger.info("="*80)
        self.logger.info(f"Environment: {self.environment.upper()}")
        self.logger.info(f"Start time: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info("")

        for i, step in enumerate(self.steps, 1):
            step_start = datetime.now()

            success = self.run_step(i, step)

            step_duration = (datetime.now() - step_start).total_seconds()
            self.logger.info(f"Step duration: {step_duration:.1f}s")

            if not success:
                self.logger.error(f"\n❌ Pipeline failed at step {i}: {step['name']}")
                self.logger.error("Stopping execution")
                return False

        total_duration = (datetime.now() - start_time).total_seconds()

        self.logger.info("\n" + "="*80)
        self.logger.info("✅ PIPELINE COMPLETE!")
        self.logger.info("="*80)
        self.logger.info(f"Total duration: {total_duration:.1f}s ({total_duration/60:.1f} minutes)")
        self.logger.info(f"End time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        self.logger.info("\n📊 Output files:")
        self.logger.info("   - output/hubspot_googlesheets_race_matches_latest.parquet")
        self.logger.info("   - output/hubspot_googlesheets_race_matches_<timestamp>.tsv")

        return True


def main():
    """Main execution"""
    environment = os.getenv('ENVIRONMENT', 'production').lower()

    if environment == 'test':
        print("🧪 Running in TEST mode")
        print("   - Step 4: 50 HubSpot records for embeddings")
        print("   - Step 5: 50 records for matching")
    else:
        print("🚀 Running in PRODUCTION mode")
        print("   - Processing ALL records")

    print("\nThis will run all 5 pipeline steps:")
    print("  1. Data Extraction")
    print("  2. Data Cleaning")
    print("  3. Temporal Filtering")
    print("  4. Embedding Generation")
    print("  5. Production Matching")
    print("")

    runner = PipelineRunner(environment=environment)
    success = runner.run()

    if success:
        print("\n✅ Pipeline completed successfully!")
        sys.exit(0)
    else:
        print("\n❌ Pipeline failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
