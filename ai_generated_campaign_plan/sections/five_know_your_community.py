import asyncio
from datetime import date
from ai_generated_campaign_plan.schema.models import CampaignInfo, CleanedCampaignInfo, IncumbentStatus, RaceType
from ai_generated_campaign_plan.utils.utils import clean_campaign_info
from shared.llm import LLMClient
from shared.logger import get_logger
from shared.tavily_client import SharedTavilyClient


class KnowYourCommunityGenerator:
    """
    A class to generate the 'Know Your Community' section of a campaign plan.
    This section will include:
    - Local media and press outlets
    - Community events the candidate can attend
    """
    
    _api_semaphore = asyncio.Semaphore(10) 
    
    def __init__(self):
        """Initialize the generator with necessary clients and logger."""
        self.logger = get_logger(__name__)
        self.tavily_client = SharedTavilyClient()
        self.llm_client = LLMClient()
        
        self.logger.info("KnowYourCommunityGenerator initialized")
    
    async def _fetch_media_press_outreach_for_campaign(self, cleaned_campaign_info: CleanedCampaignInfo) -> str:
        """
        Fetch media and press outreach for the campaign.
        
        Args:
            cleaned_campaign_info: Cleaned campaign information
            
        Returns:
            str: Media and press outreach information
        """
        self.logger.info(f"Fetching media and press outreach for candidate: {cleaned_campaign_info.candidate_name}")
        self.logger.debug(f"Campaign info details: {cleaned_campaign_info}")
        
        location = cleaned_campaign_info.city + ", " + cleaned_campaign_info.state_full

        try:
            async with self._api_semaphore:
                media_context = await self.tavily_client.get_search_context(
                    query=f"local media outlets newspapers radio TV stations {location}",
                    max_results=10
                )
            
            self.logger.debug(f"Media context: {media_context}")
            return media_context
            
        except Exception as e:
            self.logger.error(f"Failed to fetch media outreach information: {str(e)}")
            return f"Error fetching media information: {str(e)}"
    
    async def _fetch_community_events_for_campaign(self, cleaned_campaign_info: CleanedCampaignInfo) -> str:
        """
        Fetch community events for the campaign between today and election date.
        """
        self.logger.info(f"Fetching community events for candidate: {cleaned_campaign_info.candidate_name}")
        self.logger.debug(f"Campaign info details: {cleaned_campaign_info}")
        
        location = cleaned_campaign_info.city + ", " + cleaned_campaign_info.state_full
        
        try:
            today = date.today()
            election_date = cleaned_campaign_info.election_date
            
            if election_date < today:
                self.logger.warning(f"Election date {election_date} is in the past.")
                return "Election date is in the past. Please provide a future election date."
            
            months_to_search = []
            current_date = today.replace(day=1)  
            
            while current_date <= election_date:
                months_to_search.append(current_date)
                if current_date.month == 12:
                    current_date = current_date.replace(year=current_date.year + 1, month=1)
                else:
                    current_date = current_date.replace(month=current_date.month + 1)
            
            self.logger.info(f"Searching for events across {len(months_to_search)} months")
            
            search_tasks = []
            month_info = []
            
            for month_date in months_to_search:
                month_name = month_date.strftime("%B")
                year = month_date.year
                month_info.append((month_name, year))
                
                self.logger.debug(f"Preparing search for events in {month_name} {year}")
                
                async def search_task(m_name=month_name, m_year=year):
                    return await self.tavily_client.get_search_context(
                        query=f"{location} community events {m_name} {m_year} calendar with dates",
                        search_depth="basic",
                        max_results=3
                    )
                
                task = search_task()
                search_tasks.append(task)
            
            self.logger.info(f"Executing {len(search_tasks)} searches in parallel...")
            search_results = await asyncio.gather(*search_tasks, return_exceptions=True)
            
            all_events = []
            for i, (month_name, year) in enumerate(month_info):
                result = search_results[i]
                
                if isinstance(result, Exception):
                    self.logger.error(f"Search failed for {month_name} {year}: {result}")
                    all_events.append(f"\n=== {month_name} {year} Events ===\nError: {result}")
                else:
                    all_events.append(f"\n=== {month_name} {year} Events ===\n{result}")
            
            combined_events = "\n".join(all_events)
            
            self.logger.info(f"Successfully fetched events for {len(months_to_search)} months")
            
            self.logger.debug(f"Combined events: {combined_events}")
            return combined_events
            
        except Exception as e:
            self.logger.error(f"Failed to fetch community events: {str(e)}")
            return f"Error fetching community events: {str(e)}"
    
    async def generate_know_your_community_section(self, cleaned_campaign_info: CleanedCampaignInfo) -> str:
        """
        Generate a section of the campaign plan that includes information about the community events and media info.
        
        Args:
            cleaned_campaign_info: Cleaned campaign information
            
        Returns:
            str: Generated community section
            
        Raises:
            Exception: If community section generation fails
        """
        self.logger.info(f"Starting community section generation for candidate: {cleaned_campaign_info.candidate_name}")
        self.logger.debug(f"Campaign info details: {cleaned_campaign_info}")
        
        try:
            self.logger.debug("Beginning community analysis process")
                        
            media_info = await self._fetch_media_press_outreach_for_campaign(cleaned_campaign_info)
            community_events = await self._fetch_community_events_for_campaign(cleaned_campaign_info)
            
            prompt = f"""
You are an expert campaign strategist. You are given community events and media info and you are to generate a know your community section for the campaign.

election campaign jurisdiction and office is for:
{cleaned_campaign_info.office_and_jurisdiction}

community events are here:
{community_events}

media info is here:
{media_info}

RULES:
- events should be listed in the order of the date they are happening
- no repeats of events
- do not include any events that happened already. today is {date.today()}
- community event list can be up to 10 events.
- media info can be up to 7 media outlets.
- pick the most relevant media outlets and events for the campaign.


this section should look like this:
```
# KNOW YOUR COMMUNITY
## Community Events & Civic Presence
 - City Commission & Parks Meetings (June 12 & 16): opportunities to speak or attend.
 - June 24 "Save a Life" / Veterans Event: civic visibility and voter interaction.
 - Library & Bookmobile Events (June 11–17): reach families and educators.
 - Chicopee Chamber: June 9, 11, 27 networking and visibility events.
## Earned Media & Press Outreach
 - WWLP-22News: broadcast coverage.
 - NEPM (PBS/NPR): regional trust and reach.
 - The Chicopee Register: hyper-local coverage.
 - WACE 730 AM: faith-based and local listenership.
 - Patch.com – Chicopee: digital visibility hub.

 *Check the dates for accuracy. Regular searching of community calendars and websites is recommended to find new events.*
```
"""
            self.logger.debug(f"Prompt: {prompt}")
            self.logger.debug(f"running llm with prompt")
            llm_response = self.llm_client.create_completion(
                max_tokens=10000,
                model="deepseek-ai/DeepSeek-R1",
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert campaign strategist. You are given a campaign info and you are to generate a know your community section for the campaign.",
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                temperature=0.0
            )
            
            result = llm_response.choices[0].message.content

            self.logger.info("Successfully generated community section")
            self.logger.debug(f"Generated section length: {len(result)} characters")
            
            return result
            
        except Exception as e:
            self.logger.error(f"Failed to generate community section: {str(e)}")
            self.logger.debug(f"Exception details: {type(e).__name__}: {e}", exc_info=True)
            raise


if __name__ == "__main__":
    logger = get_logger(__name__)
    logger.info("Starting five_know_your_community module in standalone mode")
    
    try:
        logger.debug("Creating sample campaign info")
        campaign_info = CampaignInfo(
            candidate_name="John Doe",
            primary_date=None,
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
        )
        logger.debug(f"Campaign info created: {campaign_info.candidate_name}")
        logger.info("Cleaning campaign information")
        
        cleaned_campaign_info = clean_campaign_info(campaign_info)
        
        logger.debug("Campaign info cleaned successfully")
        logger.info("Generating community section")
        
        generator = KnowYourCommunityGenerator()
        result = asyncio.run(generator.generate_know_your_community_section(cleaned_campaign_info))
        
        logger.info("Module execution completed successfully")

        print(result)
    except Exception as e:
        logger.error(f"Module execution failed: {str(e)}")
        logger.debug(f"Exception in main: {type(e).__name__}: {e}", exc_info=True)
        raise