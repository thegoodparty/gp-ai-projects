from datetime import date
import os
import json
import time
import logging
from together import Together
from dotenv import load_dotenv

from ai_generated_campaign_plan.schema.models import CampaignInfo, CleanedCampaignInfo, IncumbentStatus, RaceType, AdditionalCampaignInfo
from shared.logger import get_logger


load_dotenv()


def clean_campaign_info(campaign_info: CampaignInfo) -> CleanedCampaignInfo:
    """
    Convert CampaignInfo to CleanedCampaignInfo using Together AI to extract and format additional fields.
    
    Args:
        campaign_info: The original campaign information
        
    Returns:
        CleanedCampaignInfo: Enhanced campaign info with parsed location and formatted dates
    """
    logger = get_logger(__name__)
    together_client = Together(api_key=os.getenv("TOGETHER_API_KEY"))
    
    extraction_prompt = f"""
Given the following campaign information

CAMPAIGN INFO:
- Office and Jurisdiction: {campaign_info.office_and_jurisdiction}
- Election Date: {campaign_info.election_date}
- Primary Date: {campaign_info.primary_date}

extract the following fields as a JSON object:

EXTRACT:
    city: str = Field(..., description="The city name extracted from the jurisdiction")
    state: str = Field(..., description="The state name or abbreviation extracted from the jurisdiction")
    state_full: str = Field(..., description="The full state name (e.g., Massachusetts)")
    election_date_formatted: str = Field(..., description="The election date formatted as YYYY-MM-DD")
    has_primary: bool = Field(..., description="Whether the election has a primary")
    primary_date_formatted: Optional[str] = Field(None, description="The primary date formatted as YYYY-MM-DD if exists")
"""

    max_retries = 5
    base_delay = 1  
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Attempting to extract campaign info using Together AI (attempt {attempt + 1}/{max_retries})")
            
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
                model="Qwen/Qwen3-235B-A22B-fp8-tput",
                response_format={
                    "type": "json_schema",
                    "schema": AdditionalCampaignInfo.model_json_schema(),
                },
                max_tokens=300,
            )
            
            output = json.loads(result.choices[0].message.content)
            extracted_fields = AdditionalCampaignInfo(**output)
            print(extracted_fields)

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
            
            logger.info("Successfully extracted and cleaned campaign info")
            return cleaned_info
            
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed with error: {str(e)}")
            
            if attempt < max_retries - 1: 
                delay = base_delay * (2 ** attempt)  
                logger.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                logger.error(f"All {max_retries} attempts failed. Unable to extract campaign info using Together AI.")
                raise RuntimeError(f"Failed to clean campaign info using Together AI after {max_retries} attempts. Last error: {str(e)}")


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