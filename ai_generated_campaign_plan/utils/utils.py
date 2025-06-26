from datetime import date
import os
import json
from typing import Optional
from together import Together
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from ai_generated_campaign_plan.schema.models import CampaignInfo, CleanedCampaignInfo, IncumbentStatus, RaceType, AdditionalCampaignInfo


load_dotenv()


def clean_campaign_info(campaign_info: CampaignInfo) -> CleanedCampaignInfo:
    """
    Convert CampaignInfo to CleanedCampaignInfo using Together AI to extract and format additional fields.
    
    Args:
        campaign_info: The original campaign information
        
    Returns:
        CleanedCampaignInfo: Enhanced campaign info with parsed location and formatted dates
    """
    together_client = Together(api_key=os.getenv("TOGETHER_API_KEY"))
    
    # Prepare the prompt for AI to extract and format the additional fields
    extraction_prompt = f"""
Given the following campaign information, extract and format the additional required fields:

CAMPAIGN INFO:
- Office and Jurisdiction: {campaign_info.office_and_jurisdiction}
- Election Date: {campaign_info.election_date}
- Primary Date: {campaign_info.primary_date}

EXTRACT:
1. City name from the jurisdiction
2. State abbreviation from the jurisdiction  
3. Full state name (e.g., MA -> Massachusetts)
4. Election date in YYYY-MM-DD format
5. Primary date in YYYY-MM-DD format (if exists, otherwise null)
6. Whether the election has a primary
"""

    try:
        result = together_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert at parsing location and date information from campaign data. Extract the required fields accurately and format dates consistently. Only respond in JSON format.",
                },
                {
                    "role": "user",
                    "content": extraction_prompt,
                },
            ],
            model="meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
            response_format={
                "type": "json_schema",
                "schema": AdditionalCampaignInfo.model_json_schema(),
            },
            max_tokens=300,
        )
        
        output = json.loads(result.choices[0].message.content)
        extracted_fields = AdditionalCampaignInfo(**output)

        cleaned_info = CleanedCampaignInfo(
            candidate_name=campaign_info.candidate_name,
            primary_date=campaign_info.primary_date,
            election_date=campaign_info.election_date,
            office_and_jurisdiction=campaign_info.office_and_jurisdiction,
            incumbent_status=campaign_info.incumbent_status,
            race_type=campaign_info.race_type,
            seats_available=campaign_info.seats_available,
            number_of_opponents=campaign_info.number_of_opponents,
            win_number=campaign_info.win_number,
            total_likely_voters=campaign_info.total_likely_voters,
            available_cell_phones=campaign_info.available_cell_phones,
            available_landlines=campaign_info.available_landlines,
            additional_race_context=campaign_info.additional_race_context,
            city=extracted_fields.city,
            state=extracted_fields.state,
            state_full=extracted_fields.state_full,
            election_date_formatted=extracted_fields.election_date_formatted,
            has_primary=extracted_fields.has_primary,
            primary_date_formatted=extracted_fields.primary_date_formatted
        )
        
        return cleaned_info
        
    except Exception as e:
        raise RuntimeError(f"Failed to clean campaign info using Together AI: {str(e)}")


if __name__ == "__main__":
    clean_campaign_info(CampaignInfo(
        candidate_name="John Doe",
        primary_date=None,
        election_date=date(2024, 11, 5),
        office_and_jurisdiction="School Board, At-Large, Chicopee, MA",
        incumbent_status=IncumbentStatus.NOT_APPLICABLE,
        race_type=RaceType.NONPARTISAN,
        seats_available=1,
        number_of_opponents=1,
        win_number=4213,
        total_likely_voters=8429,
        available_cell_phones=4505,
        available_landlines=3780,
        additional_race_context="Focus on education funding and infrastructure improvements"
    ))