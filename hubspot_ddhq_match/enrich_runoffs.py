#!/usr/bin/env python3

"""
RUNOFF ENRICHMENT FOR HUBSPOT-DDHQ MATCHING

After initial matching completes, this script:
1. Discovers runoffs from DDHQ for matched candidates
2. Creates synthetic HubSpot runoff records
3. Generates embeddings for synthetic records
4. Matches synthetic runoffs to DDHQ
5. Merges results with original matches

USAGE:
    uv run hubspot_ddhq_match/enrich_runoffs.py
"""

import sys
import os
import asyncio
import pandas as pd
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from shared.logger import get_logger

class RunoffEnricher:
    def __init__(self):
        self.logger = get_logger(__name__)
        self.current_dir = os.path.dirname(os.path.abspath(__file__))

    def determine_runoff_type(self, candidate_elections_df):
        """
        Determine if a runoff is a primary runoff or general runoff.

        Logic:
        - If candidate has primary + runoff (no general) → primary runoff
        - If candidate has general + runoff (no primary) → general runoff
        - If candidate has primary + general + runoff → use dates to determine
        """
        elections = candidate_elections_df.sort_values('date')

        has_primary = 'primary' in elections['election_type'].values
        has_general = 'general' in elections['election_type'].values
        has_runoff = 'runoff' in elections['election_type'].values

        if not has_runoff:
            return None

        runoff_date = elections[elections['election_type'] == 'runoff']['date'].iloc[0]

        if has_primary and not has_general:
            return 'primary'

        if has_general and not has_primary:
            return 'general'

        if has_primary and has_general:
            primary_date = elections[elections['election_type'] == 'primary']['date'].iloc[0]
            general_date = elections[elections['election_type'] == 'general']['date'].iloc[0]

            if primary_date < runoff_date < general_date:
                return 'primary'
            elif runoff_date > general_date:
                return 'general'
            else:
                self.logger.warning(f"Unexpected runoff sequence for candidate {elections['candidate_id'].iloc[0]}")
                return 'general'

        return 'general'

    def discover_runoffs(self, matches_df, ddhq_df):
        """Discover runoffs from DDHQ for all matched candidates"""
        self.logger.info("🔍 Step 1: Discovering runoffs from matched candidates...")

        matched_records = matches_df[matches_df['has_match'] == True].copy()
        self.logger.info(f"   Checking {len(matched_records):,} matched records...")

        discovered_runoffs = []

        for idx, match_row in matched_records.iterrows():
            ddhq_candidate_id = match_row['ddhq_candidate_id']

            if pd.isna(ddhq_candidate_id):
                continue

            candidate_elections = ddhq_df[ddhq_df['candidate_id'] == ddhq_candidate_id]
            runoff_type = self.determine_runoff_type(candidate_elections)

            if runoff_type:
                runoff_record = candidate_elections[candidate_elections['election_type'] == 'runoff'].iloc[0]

                discovered_runoffs.append({
                    'hubspot_gp_candidacy_id': match_row['hubspot_gp_candidacy_id'],
                    'hubspot_full_name': match_row['hubspot_full_name'],
                    'hubspot_first_name': match_row['hubspot_first_name'],
                    'hubspot_last_name': match_row['hubspot_last_name'],
                    'hubspot_state': match_row['hubspot_state'],
                    'hubspot_city': match_row['hubspot_city'],
                    'hubspot_candidate_office': match_row['hubspot_candidate_office'],
                    'hubspot_official_office_name': match_row['hubspot_official_office_name'],
                    'hubspot_party_affiliation': match_row['hubspot_party_affiliation'],
                    'matched_election_type': match_row['hubspot_election_type'],
                    'matched_election_date': match_row['hubspot_election_date'],
                    'discovered_runoff_type': runoff_type,
                    'discovered_runoff_date': runoff_record['date'],
                    'ddhq_candidate_id': ddhq_candidate_id,
                    'ddhq_race_id': runoff_record['race_id'],
                    'ddhq_race_name': runoff_record['race_name'],
                    'ddhq_candidate_name': runoff_record['candidate']
                })

        discovered_df = pd.DataFrame(discovered_runoffs)

        if len(discovered_df) > 0:
            self.logger.info(f"✅ Discovered {len(discovered_df):,} runoffs:")
            runoff_type_counts = discovered_df['discovered_runoff_type'].value_counts()
            for runoff_type, count in runoff_type_counts.items():
                self.logger.info(f"   {runoff_type} runoffs: {count:,}")

            state_counts = discovered_df.groupby('hubspot_state').size().reset_index(name='count').sort_values('count', ascending=False)
            state_summary = ', '.join([f"{row['hubspot_state']}({row['count']})" for _, row in state_counts.head(5).iterrows()])
            self.logger.info(f"   Top states: {state_summary}")
        else:
            self.logger.info("   No runoffs discovered")

        return discovered_df

    def create_synthetic_records(self, discovered_df):
        """Create synthetic HubSpot records for discovered runoffs"""
        self.logger.info("\n📝 Step 2: Creating synthetic HubSpot runoff records...")

        if len(discovered_df) == 0:
            self.logger.info("   No synthetic records to create")
            return pd.DataFrame()

        synthetic_records = []

        for idx, row in discovered_df.iterrows():
            name = ""
            if pd.notna(row.get('hubspot_first_name')) and pd.notna(row.get('hubspot_last_name')):
                name = f"{row['hubspot_first_name']} {row['hubspot_last_name']}"

            race = ""
            if pd.notna(row.get('hubspot_official_office_name')):
                race = row['hubspot_official_office_name']
            elif pd.notna(row.get('hubspot_candidate_office')):
                state = row.get('hubspot_state', '')
                candidate_office = row['hubspot_candidate_office']
                race = f"{state} {candidate_office}".strip()

            embedding_text = f"name: {name} | race: {race}"

            synthetic_records.append({
                'gp_candidacy_id': row['hubspot_gp_candidacy_id'],
                'first_name': row['hubspot_first_name'],
                'last_name': row['hubspot_last_name'],
                'full_name': row['hubspot_full_name'],
                'state': row['hubspot_state'],
                'city': row['hubspot_city'],
                'candidate_office': row['hubspot_candidate_office'],
                'official_office_name': row['hubspot_official_office_name'],
                'party_affiliation': row['hubspot_party_affiliation'],
                'embedding_name_race_text': embedding_text,
                'election_date': row['discovered_runoff_date'],
                'election_type': 'runoff',
                'is_synthetic_runoff': True,
                'discovered_from_ddhq_candidate_id': row['ddhq_candidate_id']
            })

        synthetic_df = pd.DataFrame(synthetic_records)

        self.logger.info(f"✅ Created {len(synthetic_df):,} synthetic runoff records")

        return synthetic_df

    async def generate_embeddings(self, synthetic_df):
        """Generate embeddings for synthetic runoff records"""
        self.logger.info("\n🔢 Step 3: Generating embeddings for synthetic runoffs...")

        if len(synthetic_df) == 0:
            self.logger.info("   No embeddings to generate")
            return synthetic_df

        from shared.llm_gemini import GeminiEmbeddingClient

        embedding_client = GeminiEmbeddingClient()

        texts = synthetic_df['embedding_name_race_text'].tolist()

        self.logger.info(f"   Generating embeddings for {len(texts)} records...")

        # Call the async method directly since we're already in an async context
        embeddings = await embedding_client._create_embeddings_parallel(
            texts,
            batch_size=100,
            max_concurrent_batches=10,
            rate_limit_delay=2.0,
            stagger_delay=0.1
        )

        import numpy as np
        if isinstance(embeddings, np.ndarray) and len(embeddings.shape) == 2:
            embeddings = [embeddings[i] for i in range(embeddings.shape[0])]

        synthetic_df['embedding_name_race'] = embeddings

        self.logger.info(f"✅ Generated {len(embeddings):,} embeddings")

        return synthetic_df

    async def match_synthetic_runoffs(self, synthetic_df, ddhq_df):
        """Match synthetic runoff records to DDHQ using the production matcher logic"""
        self.logger.info("\n🎯 Step 4: Matching synthetic runoffs to DDHQ...")

        if len(synthetic_df) == 0:
            self.logger.info("   No synthetic runoffs to match")
            return pd.DataFrame()

        # Save synthetic data to temp files
        self.logger.info(f"   Saving synthetic data to temp files...")
        offline_data_dir = os.path.join(self.current_dir, "offline_data")

        temp_hubspot_file = os.path.join(offline_data_dir, "hubspot_synthetic_runoffs_TEMP.parquet")
        temp_ddhq_file = os.path.join(offline_data_dir, "ddhq_synthetic_runoffs_TEMP.parquet")

        synthetic_df.to_parquet(temp_hubspot_file, index=False, engine='pyarrow', coerce_timestamps='us')
        ddhq_df.to_parquet(temp_ddhq_file, index=False, engine='pyarrow', coerce_timestamps='us')

        try:
            self.logger.info(f"   Importing matcher from parallel_production_matcher...")

            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "parallel_production_matcher",
                os.path.join(self.current_dir, "parallel_production_matcher.py")
            )
            matcher_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(matcher_module)

            self.logger.info(f"   Creating matcher instance for {len(synthetic_df):,} synthetic runoffs...")
            matcher = matcher_module.ParallelProductionMatcher(
                batch_size=1000,
                max_records=None,
                max_workers=500,
                hubspot_file=temp_hubspot_file,
                ddhq_file=temp_ddhq_file
            )

            self.logger.info(f"   Running matcher on {len(synthetic_df):,} synthetic runoffs...")
            matches = await matcher.process_all_records()

            matched_count = matches['has_match'].sum()
            self.logger.info(f"✅ Matched {matched_count:,} / {len(synthetic_df):,} synthetic runoffs ({matched_count/len(synthetic_df)*100:.1f}%)")

            return matches

        finally:
            # Clean up temp files
            if os.path.exists(temp_hubspot_file):
                os.remove(temp_hubspot_file)
            if os.path.exists(temp_ddhq_file):
                os.remove(temp_ddhq_file)

    def merge_results(self, original_matches_df, synthetic_matches_df):
        """Merge original matches with synthetic runoff matches"""
        self.logger.info("\n🔗 Step 5: Merging results...")

        if len(synthetic_matches_df) == 0:
            self.logger.info("   No synthetic matches to merge")
            return original_matches_df

        # Make copies to avoid modifying originals
        original_matches_copy = original_matches_df.copy()
        synthetic_matches_copy = synthetic_matches_df.copy()

        # Ensure date columns have consistent types before merging (coerce errors for N/A values)
        date_columns = ['hubspot_election_date', 'election_date', 'ddhq_date', 'runoff_fallback_date']
        for col in date_columns:
            if col in synthetic_matches_copy.columns:
                synthetic_matches_copy[col] = pd.to_datetime(synthetic_matches_copy[col], errors='coerce')
            if col in original_matches_copy.columns:
                original_matches_copy[col] = pd.to_datetime(original_matches_copy[col], errors='coerce')

        original_matches_copy['is_synthetic_runoff'] = False
        original_matches_copy['discovered_from_ddhq_candidate_id'] = None

        merged_df = pd.concat([original_matches_copy, synthetic_matches_copy], ignore_index=True)

        self.logger.info(f"✅ Merged results:")
        self.logger.info(f"   Original matches: {len(original_matches_df):,}")
        self.logger.info(f"   Synthetic runoff matches: {len(synthetic_matches_df):,}")
        self.logger.info(f"   Total matches: {len(merged_df):,}")

        total_matched = merged_df['has_match'].sum()
        self.logger.info(f"   Total with matches: {total_matched:,} ({total_matched/len(merged_df)*100:.1f}%)")

        return merged_df

    def _convert_timestamps_to_compatible_format(self, df: pd.DataFrame) -> pd.DataFrame:
        """Convert nanosecond timestamps to microsecond precision for Parquet compatibility"""
        df = df.copy()
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                df[col] = df[col].astype('datetime64[us]')
        return df

    def save_results(self, merged_df, discovered_df):
        """Save enriched results"""
        self.logger.info("\n💾 Saving enriched results...")

        output_dir = os.path.join(self.current_dir, "output")
        os.makedirs(output_dir, exist_ok=True)

        merged_df = self._convert_timestamps_to_compatible_format(merged_df)

        matches_file = os.path.join(output_dir, "matches.parquet")

        merged_df.to_parquet(matches_file, index=False, engine='pyarrow', coerce_timestamps='us')

        self.logger.info(f"   Enriched matches: {matches_file}")

        return matches_file

async def main():
    enricher = RunoffEnricher()

    print("🚀 RUNOFF ENRICHMENT PIPELINE")
    print("=" * 70)

    try:
        offline_data_dir = os.path.join(enricher.current_dir, "offline_data")
        output_dir = os.path.join(enricher.current_dir, "output")

        print("📥 Loading data...")
        original_matches = pd.read_parquet(os.path.join(output_dir, 'parallel_hubspot_ddhq_matches_latest.parquet'))
        ddhq_df = pd.read_parquet(os.path.join(offline_data_dir, 'ddhq_with_embeddings_cleaned_latest.parquet'))

        print(f"   Original matches: {len(original_matches):,}")
        print(f"   DDHQ records: {len(ddhq_df):,}")
        print()

        discovered_df = enricher.discover_runoffs(original_matches, ddhq_df)

        synthetic_df = enricher.create_synthetic_records(discovered_df)

        if len(synthetic_df) > 0:
            synthetic_df = await enricher.generate_embeddings(synthetic_df)

            synthetic_matches = await enricher.match_synthetic_runoffs(synthetic_df, ddhq_df)

            merged_df = enricher.merge_results(original_matches, synthetic_matches)
        else:
            merged_df = original_matches
            enricher.logger.info("\n   No runoffs to enrich - using original matches")

        output_file = enricher.save_results(merged_df, discovered_df)

        print()
        print("=" * 70)
        print("✅ RUNOFF ENRICHMENT COMPLETE!")
        print(f"   Output: {output_file}")
        print("=" * 70)

    except Exception as e:
        enricher.logger.error(f"❌ Runoff enrichment failed: {str(e)}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
