from datetime import date
from dotenv import load_dotenv

from ai_generated_campaign_plan.schema.models import CampaignInfo, CleanedCampaignInfo, IncumbentStatus, RaceType, AdditionalCampaignInfo, ContactOptimization
from shared.llm import LLMClient
from shared.logger import get_logger

load_dotenv()

class CampaignUtils:
    """
    A unified utility class for campaign information processing and optimization.
    """
    
    def __init__(self, llm_client: LLMClient = None):
        """
        Initialize the campaign utilities.
        
        Args:
            llm_client: LLMClient instance. If None, creates a default one.
        """
        self.llm_client = llm_client or LLMClient()
        self.logger = get_logger(__name__)
    
    def clean_campaign_info(self, campaign_info: CampaignInfo) -> CleanedCampaignInfo:
        """
        Convert CampaignInfo to CleanedCampaignInfo using LLM to extract and format additional fields.
        
        Args:
            campaign_info: The original campaign information
            
        Returns:
            CleanedCampaignInfo: Enhanced campaign info with parsed location and formatted dates
        """
        extraction_prompt = f"""
Given the following campaign information:

- Office and Jurisdiction: {campaign_info.office_and_jurisdiction}
- Election Date: {campaign_info.election_date}
- Primary Date: {campaign_info.primary_date}

extract the following fields as a JSON object:
    city: str = Field(..., description="The city name extracted from the jurisdiction")
    state: str = Field(..., description="The state name or abbreviation extracted from the jurisdiction")
    state_full: str = Field(..., description="The full state name (e.g., Massachusetts)")
    election_date_formatted: str = Field(..., description="The election date formatted as YYYY-MM-DD")
    has_primary: bool = Field(..., description="Whether the election has a primary")
    primary_date_formatted: Optional[str] = Field(None, description="The primary date formatted as YYYY-MM-DD if exists")
"""

        messages = [
            {
                "role": "system",
                "content": "You are an expert at parsing location and date information from campaign data. Extract the required fields accurately and format dates consistently. Only respond in JSON format.",
            },
            {
                "role": "user",
                "content": extraction_prompt,
            },
        ]
        
        try:
            self.logger.info("Extracting campaign info using LLM")
            extracted_fields = self.llm_client.create_structured_completion(
                messages=messages,
                response_schema=AdditionalCampaignInfo,
                max_tokens=300
            )
            
            self.logger.debug(f"Extracted fields: {extracted_fields}")
            
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
            
            self.logger.info("Successfully extracted and cleaned campaign info")
            return cleaned_info
            
        except Exception as e:
            self.logger.error(f"Failed to clean campaign info: {str(e)}")
            raise RuntimeError(f"Failed to clean campaign info: {str(e)}")

    def optimize_contact_strategy(self, start_date: date, end_date: date) -> ContactOptimization:
        """
        Determine optimal number of contacts based on campaign parameters.
        
        Args:
            start_date: Campaign start date
            end_date: Campaign end date
            
        Returns:
            ContactOptimization: Optimized contact strategy with reasoning
        """
        days_available = (end_date - start_date).days
        
        self.logger.info(f"Optimizing contact strategy")
        self.logger.debug(f"Days available: {days_available}")
        
        if days_available <= 21:
            optimization = ContactOptimization(
                p2p_texts=2,
                robocalls=2,
            )
        elif days_available <= 45:
            optimization = ContactOptimization(
                p2p_texts=3,
                robocalls=2,
            )
        else:
            optimization = ContactOptimization(
                p2p_texts=4,
                robocalls=3,
            )
        
        self.logger.info(f"Contact optimization complete: {optimization.p2p_texts} texts, {optimization.robocalls} robocalls")
        
        return optimization
    



if __name__ == "__main__":
    processor = CampaignUtils()
    cleaned_campaign_info = processor.clean_campaign_info(CampaignInfo(
        candidate_name="John Doe",
        primary_date=date(2025, 9, 15),
        election_date=date(2025, 11, 5),
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
    print(cleaned_campaign_info)
    print("--------------------------------")
    print(processor.optimize_contact_strategy(date.today(), cleaned_campaign_info.primary_date))
    print("--------------------------------")
    print(processor.optimize_contact_strategy(cleaned_campaign_info.primary_date, cleaned_campaign_info.election_date))
