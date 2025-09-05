#!/usr/bin/env python3

import os
import sys
import pandas as pd
from pathlib import Path
from datetime import datetime

# Add parent directory to path to import shared modules
sys.path.append(str(Path(__file__).parent.parent.parent))

from shared.databricks_client import DatabricksClient
from shared.logger import get_logger

def create_motivated_users_parquet():
    """
    Create a parquet file containing the motivated users who provided
    state + office information but still couldn't get matched by BallotReady.
    
    These are the best candidates for enhanced matching algorithms.
    """
    logger = get_logger(__name__)
    
    try:
        with DatabricksClient() as client:
            logger.info("Creating parquet for motivated unmatched users..")
            
            # Query for motivated users - those with state AND office but no BR match
            motivated_users_query = """
            SELECT 
                -- Campaign identifiers
                c.id as campaign_id,
                c.data:name::string as candidate_name,
                c.data:slug::string as candidate_slug,
                c.data:hubspotId::string as hubspot_id,
                
                -- Account details
                CASE WHEN YEAR(c.created_at) > 2030 OR YEAR(c.created_at) < 2020 
                     THEN NULL ELSE try_cast(c.created_at as TIMESTAMP) END as account_creation_date,
                CASE WHEN YEAR(c.updated_at) > 2030 OR YEAR(c.updated_at) < 2020 
                     THEN NULL ELSE try_cast(c.updated_at as TIMESTAMP) END as account_update_date,
                c.is_active,
                c.is_pro,
                c.is_demo,
                c.data:currentStep::string as current_step,
                c.data:launchStatus::string as launch_status,
                c.data:party::string as data_party,
                c.data:createdBy::string as created_by,
                c.data:adminUserEmail::string as admin_user_email,
                CASE WHEN try_cast(c.data:lastVisited::string as bigint) > 0 
                     THEN from_unixtime(c.data:lastVisited::bigint / 1000) 
                     ELSE NULL END as last_visited_date,
                
                -- User-provided campaign details (what they manually entered)
                c.details:state::string as state,
                c.details:office::string as office,
                c.details:otherOffice::string as other_office,
                c.details:level::string as level,
                c.details:ballotLevel::string as ballot_level,
                c.details:party::string as party,
                c.details:partisanType::string as partisan_type,
                c.details:district::string as district,
                c.details:zip::string as zip_code,
                c.details:city::string as city,
                c.details:county::string as county,
                CASE WHEN try_cast(c.details:electionDate::string as date) > '2030-12-31' 
                          OR try_cast(c.details:electionDate::string as date) < '2020-01-01'
                     THEN NULL ELSE try_cast(c.details:electionDate::string as date) END as election_date,
                c.details:tier::string as tier,
                
                -- Additional campaign information from investigation
                c.details:knowRun::string as knows_running,
                c.details:pledged::string as has_pledged,
                c.details:hasPrimary::string as has_primary,
                c.details:officeTermLength::string as office_term_length,
                CASE WHEN try_cast(c.details:filingPeriodsStart::string as date) > '2030-12-31' 
                          OR try_cast(c.details:filingPeriodsStart::string as date) < '2020-01-01'
                     THEN NULL ELSE try_cast(c.details:filingPeriodsStart::string as date) END as filing_period_start,
                CASE WHEN try_cast(c.details:filingPeriodsEnd::string as date) > '2030-12-31' 
                          OR try_cast(c.details:filingPeriodsEnd::string as date) < '2020-01-01'
                     THEN NULL ELSE try_cast(c.details:filingPeriodsEnd::string as date) END as filing_period_end,
                c.details:occupation::string as occupation,
                c.details:funFact::string as fun_fact,
                c.details:website::string as campaign_website,
                c.details:pastExperience::string as past_experience,
                
                -- BR election information (when available)
                c.details:electionId::string as br_election_id,
                CASE WHEN try_cast(c.details:primaryElectionDate::string as date) > '2030-12-31' 
                          OR try_cast(c.details:primaryElectionDate::string as date) < '2020-01-01'
                     THEN NULL ELSE try_cast(c.details:primaryElectionDate::string as date) END as primary_election_date,
                c.details:primaryElectionId::string as br_primary_election_id,
                
                -- BR data (should be null/empty for these users)
                c.details:positionId::string as br_position_id,
                c.details:raceId::string as br_race_id,
                
                -- P2V data
                ptv.id as ptv_id,
                ptv.data:p2vStatus::string as p2v_status,
                CASE WHEN try_cast(ptv.data:p2vCompleteDate::string as date) > '2030-12-31'
                          OR try_cast(ptv.data:p2vCompleteDate::string as date) < '2020-01-01'
                     THEN NULL ELSE try_cast(ptv.data:p2vCompleteDate::string as date) END as p2v_complete_date,
                try_cast(try_cast(ptv.data:winNumber::string as double) as int) as p2v_win_number,
                ptv.data:electionType::string as l2_election_type,
                ptv.data:electionLocation::string as l2_election_location,
                try_cast(try_cast(ptv.data:averageTurnout::string as double) as int) as l2_average_turnout,
                try_cast(try_cast(ptv.data:projectedTurnout::string as double) as int) as l2_projected_turnout,
                
                -- Analysis flags
                CASE WHEN ptv.data:p2vStatus::string IS NOT NULL THEN 1 ELSE 0 END as has_p2v_data,
                CASE WHEN ptv.data:p2vStatus::string = 'Complete' THEN 1 ELSE 0 END as p2v_complete,
                1 as has_state_info,
                1 as has_office_info,
                CASE WHEN c.details:zip::string IS NOT NULL AND c.details:zip::string != '' THEN 1 ELSE 0 END as has_zip_code,
                CASE WHEN c.details:city::string IS NOT NULL AND c.details:city::string != '' THEN 1 ELSE 0 END as has_city,
                CASE WHEN c.details:county::string IS NOT NULL AND c.details:county::string != '' THEN 1 ELSE 0 END as has_county,
                CASE WHEN c.details:district::string IS NOT NULL AND c.details:district::string != '' THEN 1 ELSE 0 END as has_district,
                CASE WHEN c.details:electionDate::string IS NOT NULL AND c.details:electionDate::string != '' THEN 1 ELSE 0 END as has_election_date,
                CASE WHEN c.details:occupation::string IS NOT NULL AND c.details:occupation::string != '' THEN 1 ELSE 0 END as has_occupation,
                CASE WHEN c.details:website::string IS NOT NULL AND c.details:website::string != '' AND c.details:website::string != 'skipped' THEN 1 ELSE 0 END as has_website,
                CASE WHEN c.details:pastExperience::string IS NOT NULL AND c.details:pastExperience::string != '' THEN 1 ELSE 0 END as has_past_experience,
                CASE WHEN c.data:launchStatus::string = 'launched' THEN 1 ELSE 0 END as is_launched,
                CASE WHEN c.data:createdBy::string = 'admin' THEN 1 ELSE 0 END as admin_created,
                
                -- Time dimensions
                CASE WHEN YEAR(c.created_at) > 2030 OR YEAR(c.created_at) < 2020 
                     THEN NULL ELSE DATE_TRUNC('month', c.created_at) END as creation_month,
                CASE WHEN YEAR(c.created_at) > 2030 OR YEAR(c.created_at) < 2020 
                     THEN NULL ELSE YEAR(c.created_at) END as creation_year,
                CASE WHEN YEAR(c.created_at) > 2030 OR YEAR(c.created_at) < 2020 
                     THEN NULL ELSE MONTH(c.created_at) END as creation_month_num
                
            FROM goodparty_data_catalog.dbt.stg_airbyte_source__gp_api_db_campaign c
            LEFT JOIN goodparty_data_catalog.dbt.stg_airbyte_source__gp_api_db_path_to_victory ptv 
                ON c.id = ptv.campaign_id
            WHERE c.created_at >= '2024-01-01'
                AND YEAR(c.created_at) <= 2030 
                AND YEAR(c.created_at) >= 2020
                -- No BR matches
                AND (c.details:positionId::string IS NULL OR c.details:positionId::string = '')
                AND (c.details:raceId::string IS NULL OR c.details:raceId::string = '')
                -- BUT has state AND office info (motivated users)
                AND c.details:state::string IS NOT NULL 
                AND c.details:state::string != ''
                AND c.details:office::string IS NOT NULL 
                AND c.details:office::string != ''
                AND c.details:office::string != 'Other'
            ORDER BY c.created_at DESC
            """
            
            logger.info("Executing query for motivated unmatched users...")
            motivated_df = client.execute_query(motivated_users_query)
            
            # Generate timestamp for file naming
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # Create offline_data directory if it doesn't exist
            script_dir = Path(__file__).parent
            offline_data_dir = script_dir / "offline_data"
            offline_data_dir.mkdir(parents=True, exist_ok=True)
            
            # Export to Parquet
            output_file = offline_data_dir / f'motivated_unmatched_users_{timestamp}.parquet'
            logger.info(f"Saving parquet to: {output_file}")
            try:
                motivated_df.to_parquet(output_file, index=False)
                logger.info(f"Successfully saved parquet file: {output_file}")
            except Exception as e:
                logger.error(f"Failed to save parquet file: {e}")
                raise
            
            logger.info(f"\n{'='*80}")
            logger.info(f"MOTIVATED UNMATCHED USERS EXPORT COMPLETE")
            logger.info(f"{'='*80}")
            logger.info(f"File: {output_file}")
            logger.info(f"Total campaigns: {len(motivated_df):,}")
            logger.info(f"Columns: {len(motivated_df.columns)}")
            logger.info(f"File size: {output_file.stat().st_size / 1024 / 1024:.1f} MB")
            
            # Quick analysis
            logger.info(f"\n📊 QUICK ANALYSIS:")
            logger.info(f"Date range: {motivated_df['account_creation_date'].min()} to {motivated_df['account_creation_date'].max()}")
            
            # Geographic spread
            state_count = motivated_df['state'].nunique()
            top_states = motivated_df['state'].value_counts().head(5)
            logger.info(f"\nGeographic spread: {state_count} states/territories")
            logger.info(f"Top 5 states:")
            for state, count in top_states.items():
                pct = count / len(motivated_df) * 100
                logger.info(f"  {state}: {count:,} ({pct:.1f}%)")
            
            # Zip code analysis
            zip_provided = motivated_df['zip_code'].notna() & (motivated_df['zip_code'] != '')
            zip_coverage = zip_provided.mean() * 100
            unique_zips = motivated_df[zip_provided]['zip_code'].nunique()
            logger.info(f"\nZip code coverage:")
            logger.info(f"  Users with zip codes: {zip_provided.sum():,} ({zip_coverage:.1f}%)")
            logger.info(f"  Unique zip codes: {unique_zips:,}")
            
            # City analysis  
            city_provided = motivated_df['city'].notna() & (motivated_df['city'] != '')
            city_coverage = city_provided.mean() * 100
            unique_cities = motivated_df[city_provided]['city'].nunique()
            logger.info(f"\nCity coverage:")
            logger.info(f"  Users with cities: {city_provided.sum():,} ({city_coverage:.1f}%)")
            logger.info(f"  Unique cities: {unique_cities:,}")
            
            # County analysis
            county_provided = motivated_df['county'].notna() & (motivated_df['county'] != '')
            county_coverage = county_provided.mean() * 100
            unique_counties = motivated_df[county_provided]['county'].nunique()
            logger.info(f"\nCounty coverage:")
            logger.info(f"  Users with counties: {county_provided.sum():,} ({county_coverage:.1f}%)")
            logger.info(f"  Unique counties: {unique_counties:,}")
            
            if zip_provided.sum() > 0:
                top_zips = motivated_df[zip_provided]['zip_code'].value_counts().head(10)
                logger.info(f"\nTop 10 zip codes:")
                for zip_code, count in top_zips.items():
                    logger.info(f"  {zip_code}: {count:,}")
            
            if city_provided.sum() > 0:
                top_cities = motivated_df[city_provided]['city'].value_counts().head(10)
                logger.info(f"\nTop 10 cities:")
                for city, count in top_cities.items():
                    logger.info(f"  {city}: {count:,}")
            
            # Office types they tried
            top_offices = motivated_df['office'].value_counts().head(10)
            logger.info(f"\nTop 10 office types they tried:")
            for office, count in top_offices.items():
                logger.info(f"  {office}: {count:,}")
            
            # Campaign readiness analysis
            logger.info(f"\nCAMPAIGN READINESS INDICATORS:")
            
            if 'has_election_date' in motivated_df.columns:
                election_date_rate = motivated_df['has_election_date'].mean() * 100
                logger.info(f"  Have election dates: {motivated_df['has_election_date'].sum():,} ({election_date_rate:.1f}%)")
            
            if 'has_district' in motivated_df.columns:
                district_rate = motivated_df['has_district'].mean() * 100
                logger.info(f"  Have district info: {motivated_df['has_district'].sum():,} ({district_rate:.1f}%)")
                
            if 'has_occupation' in motivated_df.columns:
                occupation_rate = motivated_df['has_occupation'].mean() * 100
                logger.info(f"  Have occupation: {motivated_df['has_occupation'].sum():,} ({occupation_rate:.1f}%)")
                
            if 'has_website' in motivated_df.columns:
                website_rate = motivated_df['has_website'].mean() * 100
                logger.info(f"  Have campaign website: {motivated_df['has_website'].sum():,} ({website_rate:.1f}%)")
                
            if 'has_past_experience' in motivated_df.columns:
                experience_rate = motivated_df['has_past_experience'].mean() * 100
                logger.info(f"  Have past experience: {motivated_df['has_past_experience'].sum():,} ({experience_rate:.1f}%)")
                
            if 'is_launched' in motivated_df.columns:
                launched_rate = motivated_df['is_launched'].mean() * 100
                logger.info(f"  Have launched campaigns: {motivated_df['is_launched'].sum():,} ({launched_rate:.1f}%)")
                
            if 'admin_created' in motivated_df.columns:
                admin_rate = motivated_df['admin_created'].mean() * 100
                logger.info(f"  Admin-created accounts: {motivated_df['admin_created'].sum():,} ({admin_rate:.1f}%)")
            
            # P2V completion
            p2v_rate = motivated_df['has_p2v_data'].mean() * 100
            p2v_complete_rate = motivated_df['p2v_complete'].mean() * 100
            logger.info(f"\nP2V completion:")
            logger.info(f"  Has P2V data: {p2v_rate:.1f}%")
            logger.info(f"  P2V complete: {p2v_complete_rate:.1f}%")
            
            # Monthly trend
            monthly_counts = motivated_df.groupby('creation_month').size().tail(12)
            logger.info(f"\nRecent monthly counts:")
            for month, count in monthly_counts.items():
                logger.info(f"  {str(month)[:7]}: {count:,}")
            
            logger.info(f"\n{'='*80}")
            logger.info(f"KEY INSIGHTS")
            logger.info(f"{'='*80}")
            logger.info(f"🎯 TARGET: {len(motivated_df):,} motivated users who provided data but system failed")
            logger.info(f"💡 OPPORTUNITY: These users are prime candidates for:")
            logger.info(f"   • Enhanced BR matching algorithms")
            logger.info(f"   • Manual review process") 
            logger.info(f"   • Improved search functionality")
            logger.info(f"   • Alternative data sources integration")
            logger.info(f"📈 P2V UPSIDE: Currently {p2v_complete_rate:.1f}% complete, significant room for improvement")
            
            if 'zip_coverage' in locals():
                logger.info(f"🌍 GEOGRAPHIC MATCHING OPPORTUNITIES:")
                logger.info(f"   • {zip_coverage:.1f}% have zip codes for hyper-local matching")
                logger.info(f"   • {city_coverage:.1f}% have cities for municipal race matching")  
                logger.info(f"   • {county_coverage:.1f}% have counties for county-level race matching")
                logger.info(f"   • Geographic data can enhance BR search precision")
            
            return str(output_file), len(motivated_df)
            
    except Exception as e:
        logger.error(f"Failed to create motivated users parquet: {str(e)}")
        raise

if __name__ == "__main__":
    create_motivated_users_parquet()