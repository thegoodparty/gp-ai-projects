#!/usr/bin/env python3

"""
PARALLEL PRODUCTION MATCHER FOR HUBSPOT-GOOGLE SHEETS

High-performance matching using:
- Date + State + Election Type partitioned FAISS indices
- Semantic similarity on race/office names
- LLM validation with confidence scoring
- ThreadPoolExecutor for maximum concurrency

USAGE:
# Test mode (50 test records)
ENVIRONMENT=test BATCH_SIZE=1000 MAX_WORKERS=1500 uv run parallel_production_matcher.py

# Production mode (all records)
ENVIRONMENT=production BATCH_SIZE=1000 MAX_WORKERS=1500 uv run parallel_production_matcher.py
"""

import sys
import os
import pandas as pd
import numpy as np
import faiss
import asyncio
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))
from shared.logger import get_logger
from shared.llm_gemini import GeminiClient, GeminiModelType
from pydantic import BaseModel

class LLMMatchResponse(BaseModel):
    match_index: Optional[int]
    confidence: float
    reasoning: str

@dataclass
class MatchResult:
    hubspot_company_id: str
    candidate_name: str
    candidate_office: str
    state: str
    city: str
    district: str
    election_date: str
    election_type: str
    matched_race_id: Optional[int]
    matched_race_name: Optional[str]
    match_confidence: float
    match_reasoning: str
    partition_key: str
    candidate_races_considered: str

class ProductionMatcher:
    def __init__(self, batch_size: int = 1000, max_workers: int = 1500):
        self.logger = get_logger(__name__)
        self.batch_size = batch_size
        self.max_workers = max_workers

        self.logger.info(f"Initializing production matcher:")
        self.logger.info(f"   - Batch size: {self.batch_size}")
        self.logger.info(f"   - Max workers: {self.max_workers}")

        self.thread_pool = ThreadPoolExecutor(max_workers=self.max_workers)

        self.llm_client = GeminiClient(
            default_model=GeminiModelType.FLASH,
            default_temperature=0.0,
            thinking_budget=0,
            max_connections=self.max_workers,
            max_keepalive_connections=self.max_workers // 4
        )

        self.faiss_partitions = {}
        self.partition_data = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.thread_pool.shutdown(wait=True)
        return False

    def build_partition_key(self, date, state, election_type) -> str:
        """Build partition key from date, state, and election type"""
        date_str = pd.to_datetime(date).strftime('%Y-%m-%d')
        return f"{date_str}_{state}_{election_type}"

    def build_faiss_partitions(self, google_sheets_df: pd.DataFrame):
        """Pre-build all FAISS indices partitioned by date + state + election type"""
        self.logger.info("🔨 Building FAISS partitions (date + state + election type)...")

        grouped = google_sheets_df.groupby(['date', 'state', 'election_type'])

        self.logger.info(f"   - Total partitions to build: {len(grouped)}")

        for (date, state, election_type), group in grouped:
            partition_key = self.build_partition_key(date, state, election_type)

            embeddings = np.vstack(group['embedding'].values).astype('float32')

            dimension = embeddings.shape[1]
            index = faiss.IndexFlatL2(dimension)
            index.add(embeddings)

            self.faiss_partitions[partition_key] = index

            group_with_race_id = group.copy()
            group_with_race_id['original_race_id'] = group.index
            self.partition_data[partition_key] = group_with_race_id.reset_index(drop=True)

        self.logger.info(f"✅ Built {len(self.faiss_partitions)} FAISS partitions")

        partition_sizes = {k: len(v) for k, v in self.partition_data.items()}
        top_partitions = sorted(partition_sizes.items(), key=lambda x: x[1], reverse=True)[:10]

        self.logger.info(f"   Top 10 partitions by size:")
        for key, size in top_partitions:
            self.logger.info(f"      - {key}: {size} races")

    def search_partition(self, hubspot_record: pd.Series, top_k: int = 5) -> Optional[pd.DataFrame]:
        """Search for matching races in the relevant FAISS partition"""
        partition_key = self.build_partition_key(
            hubspot_record['election_date'],
            hubspot_record['state'],
            hubspot_record['election_type']
        )

        if partition_key not in self.faiss_partitions:
            return None

        index = self.faiss_partitions[partition_key]
        partition_df = self.partition_data[partition_key]

        if len(partition_df) == 0:
            return None

        query_embedding = hubspot_record['embedding'].reshape(1, -1).astype('float32')

        k = min(top_k, len(partition_df))
        distances, indices = index.search(query_embedding, k)

        candidates = partition_df.iloc[indices[0]].copy()
        candidates['similarity_distance'] = distances[0]

        return candidates

    async def validate_match_with_llm(
        self,
        hubspot_record: pd.Series,
        candidate_races: pd.DataFrame
    ) -> MatchResult:
        """Validate match using LLM with race/office name matching"""
        races_considered = ""

        if candidate_races is None or len(candidate_races) == 0:
            return MatchResult(
                hubspot_company_id=hubspot_record['company_id'],
                candidate_name=hubspot_record.get('candidate_name', ''),
                candidate_office=hubspot_record['office_name'],
                state=hubspot_record['state'],
                city=hubspot_record.get('city', ''),
                district=hubspot_record.get('district', ''),
                election_date=str(hubspot_record['election_date']),
                election_type=hubspot_record['election_type'],
                matched_race_id=None,
                matched_race_name=None,
                match_confidence=0.0,
                match_reasoning="No candidate races found in partition",
                partition_key=self.build_partition_key(
                    hubspot_record['election_date'],
                    hubspot_record['state'],
                    hubspot_record['election_type']
                ),
                candidate_races_considered=""
            )

        prompt = f"""Match this HubSpot candidate to the correct election race.

HubSpot Candidate:
- Office: {hubspot_record['office_name']}
- State: {hubspot_record['state']}
- City: {hubspot_record.get('city', 'N/A')}
- District: {hubspot_record.get('district', 'N/A')}
- Election Date: {hubspot_record['election_date']}
- Election Type: {hubspot_record['election_type']}

Google Sheets Races (top {len(candidate_races)} semantic matches):
"""

        races_list = []
        for idx, (_, race) in enumerate(candidate_races.iterrows(), 1):
            prompt += f"{idx}. {race['race_name']}\n"
            races_list.append(f"{idx}. {race['race_name']}")

        races_considered = " | ".join(races_list)

        prompt += """
Does this candidate's office have an EXACT MATCH in the race list below?

CRITICAL MATCHING RULES - EVERY COMPONENT MUST MATCH EXACTLY:
1. Municipality/jurisdiction names MUST be identical (city, town, borough, county, school district name)
2. ALL numeric identifiers MUST match exactly (district #, USD #, ward #, position #)
3. Directional qualifiers MUST match exactly (North, South, East, West, etc.)
4. Office type must match exactly (council, mayor, board, commission, etc.)
5. If there are MULTIPLE identifiers (e.g., Ward 6 + Position 11), ALL must match
6. If ANY single component is different, return null with confidence 0

Examples of NO MATCH (return null):
- "Aberdeen City Council Ward 6 Position 11" vs "Aberdeen City Council Ward 2 Position 4" (Ward 6≠2, Position 11≠4)
- "Bainbridge Island City Council District 7 North Ward" vs "Bainbridge Island City Council South Ward 3" (North≠South, 7≠3)
- "Bethel School District 52 Position 5" vs "Cascade School District 5 Position 5" (Bethel≠Cascade, 52≠5)
- "Winfield City Commission" vs "Arkansas City Commission" (Winfield≠Arkansas City)
- "Frontenac USD 249 School Board" vs "USD 315 Board of Education" (249≠315)

CRITICAL:
- Do NOT return "closest match" or "best semantic match"
- ONLY return a match if EVERY component is identical
- When in doubt, return null
- Most candidates will have NO MATCH - that's expected and correct

Return JSON with null if no exact match:
{
  "match_index": null,
  "confidence": 0,
  "reasoning": "<brief explanation why no match>"
}

OR if exact match found:
{
  "match_index": <1-based index>,
  "confidence": <70-100>,
  "reasoning": "<brief explanation>"
}
"""

        try:
            loop = asyncio.get_event_loop()
            result: LLMMatchResponse = await loop.run_in_executor(
                self.thread_pool,
                lambda: self.llm_client.generate_structured_content(
                    prompt=prompt,
                    response_schema=LLMMatchResponse
                )
            )

            match_index = result.match_index
            confidence = result.confidence
            reasoning = result.reasoning

            if match_index is not None and confidence >= 70:
                matched_race = candidate_races.iloc[match_index - 1]

                return MatchResult(
                    hubspot_company_id=hubspot_record['company_id'],
                    candidate_name=hubspot_record.get('candidate_name', ''),
                    candidate_office=hubspot_record['office_name'],
                    state=hubspot_record['state'],
                    city=hubspot_record.get('city', ''),
                    district=hubspot_record.get('district', ''),
                    election_date=str(hubspot_record['election_date']),
                    election_type=hubspot_record['election_type'],
                    matched_race_id=int(matched_race['original_race_id']),
                    matched_race_name=matched_race['race_name'],
                    match_confidence=confidence,
                    match_reasoning=reasoning,
                    partition_key=self.build_partition_key(
                        hubspot_record['election_date'],
                        hubspot_record['state'],
                        hubspot_record['election_type']
                    ),
                    candidate_races_considered=races_considered
                )
            else:
                return MatchResult(
                    hubspot_company_id=hubspot_record['company_id'],
                    candidate_name=hubspot_record.get('candidate_name', ''),
                    candidate_office=hubspot_record['office_name'],
                    state=hubspot_record['state'],
                    city=hubspot_record.get('city', ''),
                    district=hubspot_record.get('district', ''),
                    election_date=str(hubspot_record['election_date']),
                    election_type=hubspot_record['election_type'],
                    matched_race_id=None,
                    matched_race_name=None,
                    match_confidence=confidence,
                    match_reasoning=f"Low confidence or no match: {reasoning}",
                    partition_key=self.build_partition_key(
                        hubspot_record['election_date'],
                        hubspot_record['state'],
                        hubspot_record['election_type']
                    ),
                    candidate_races_considered=races_considered
                )

        except Exception as e:
            self.logger.error(f"LLM validation failed: {str(e)}")
            return MatchResult(
                hubspot_company_id=hubspot_record['company_id'],
                candidate_name=hubspot_record.get('candidate_name', ''),
                candidate_office=hubspot_record['office_name'],
                state=hubspot_record['state'],
                city=hubspot_record.get('city', ''),
                district=hubspot_record.get('district', ''),
                election_date=str(hubspot_record['election_date']),
                election_type=hubspot_record['election_type'],
                matched_race_id=None,
                matched_race_name=None,
                match_confidence=0.0,
                match_reasoning=f"Error: {str(e)}",
                partition_key=self.build_partition_key(
                    hubspot_record['election_date'],
                    hubspot_record['state'],
                    hubspot_record['election_type']
                ),
                candidate_races_considered=races_considered
            )

    async def match_single_record(self, hubspot_record: pd.Series) -> MatchResult:
        """Match a single HubSpot record"""
        candidate_races = self.search_partition(hubspot_record, top_k=5)
        match_result = await self.validate_match_with_llm(hubspot_record, candidate_races)
        return match_result

    async def match_all_records(self, hubspot_df: pd.DataFrame) -> List[MatchResult]:
        """Match all HubSpot records using high concurrency"""
        self.logger.info(f"🚀 Matching {len(hubspot_df):,} HubSpot records...")

        tasks = []
        for idx, row in hubspot_df.iterrows():
            task = self.match_single_record(row)
            tasks.append(task)

        self.logger.info(f"   - Created {len(tasks):,} matching tasks")
        self.logger.info(f"   - Executing with {self.max_workers} workers...")

        results = await asyncio.gather(*tasks, return_exceptions=True)

        successful_results = [r for r in results if isinstance(r, MatchResult)]
        failed_results = [r for r in results if isinstance(r, Exception)]

        self.logger.info(f"✅ Matching complete:")
        self.logger.info(f"   - Successful: {len(successful_results):,}")
        self.logger.info(f"   - Failed: {len(failed_results):,}")

        return successful_results

    def save_matches(self, matches: List[MatchResult]):
        """Save match results to output directory"""
        self.logger.info("💾 Saving match results...")

        current_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(current_dir, "output")
        os.makedirs(output_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        matches_df = pd.DataFrame([vars(m) for m in matches])

        parquet_file = os.path.join(output_dir, "hubspot_googlesheets_race_matches_latest.parquet")
        tsv_file = os.path.join(output_dir, f"hubspot_googlesheets_race_matches_{timestamp}.tsv")

        matches_df.to_parquet(parquet_file, index=False)
        matches_df.to_csv(tsv_file, sep='\t', index=False)

        self.logger.info(f"✅ Match results saved:")
        self.logger.info(f"   - {parquet_file}")
        self.logger.info(f"   - {tsv_file}")

        matched_count = matches_df['matched_race_id'].notna().sum()
        match_rate = (matched_count / len(matches_df) * 100) if len(matches_df) > 0 else 0

        self.logger.info(f"\n📊 MATCH STATISTICS:")
        self.logger.info(f"   - Total records: {len(matches_df):,}")
        self.logger.info(f"   - Successful matches: {matched_count:,} ({match_rate:.1f}%)")
        self.logger.info(f"   - No match: {len(matches_df) - matched_count:,}")

        if matched_count > 0:
            avg_confidence = matches_df[matches_df['matched_race_id'].notna()]['match_confidence'].mean()
            self.logger.info(f"   - Average confidence: {avg_confidence:.1f}%")

    async def run(self, test_mode: bool = False):
        """Execute complete matching pipeline"""
        self.logger.info("🚀 Starting production matching...")

        current_dir = os.path.dirname(os.path.abspath(__file__))
        offline_data_dir = os.path.join(current_dir, "offline_data")

        hubspot_df = pd.read_parquet(
            os.path.join(offline_data_dir, "hubspot_companies_with_embeddings_latest.parquet")
        )
        google_sheets_df = pd.read_parquet(
            os.path.join(offline_data_dir, "google_sheets_races_with_embeddings_latest.parquet")
        )

        self.logger.info(f"   - Loaded {len(hubspot_df):,} HubSpot records")
        self.logger.info(f"   - Loaded {len(google_sheets_df):,} Google Sheets races")

        if test_mode:
            hubspot_df = hubspot_df.head(50)
            self.logger.info(f"   - TEST MODE: Limited to {len(hubspot_df)} records")

        self.build_faiss_partitions(google_sheets_df)

        matches = await self.match_all_records(hubspot_df)

        self.save_matches(matches)

        self.logger.info("✅ Production matching complete!")


def main():
    """Main execution"""
    print("="*80)
    print("HUBSPOT-GOOGLE SHEETS PRODUCTION MATCHING")
    print("="*80)

    try:
        batch_size = int(os.getenv('BATCH_SIZE', 1000))
        max_workers = int(os.getenv('MAX_WORKERS', 1500))

        environment = os.getenv('ENVIRONMENT', '').lower()
        test_mode = environment == 'test'

        if test_mode:
            print("🧪 Running in DEVELOPMENT mode (50 test records)")

        matcher = ProductionMatcher(batch_size=batch_size, max_workers=max_workers)

        asyncio.run(matcher.run(test_mode=test_mode))

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        raise


if __name__ == "__main__":
    main()
