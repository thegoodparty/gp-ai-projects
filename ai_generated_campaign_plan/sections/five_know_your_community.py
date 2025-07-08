import asyncio
from datetime import date
from ai_generated_campaign_plan.schema.models import CampaignInfo, CleanedCampaignInfo, IncumbentStatus, RaceType, SearchTermsList
from ai_generated_campaign_plan.utils.utils import CampaignUtils
from shared.llm import LLMClient
from shared.logger import get_logger
from shared.tavily_client import SharedTavilyClient

class KnowYourCommunityGenerator:
    """
    A class to generate the 'Know Your Community' section of a campaign plan.
    This section will include:
    - Local media and press outlets
    - Community events the candidate can attend

    example output:
    ```
    ## 5. KNOW YOUR COMMUNITY

    ### Community Events & Civic Presence
    - City Commission & Parks Meetings (June 12 & 16): opportunities to speak or attend.
    - "Save a Life" / Veterans Event (June 24): civic visibility and voter interaction.
    - Library & Bookmobile Events (June 11–17): reach families and educators.
    - Chicopee Chamber (June 9, 11, 27): networking and visibility events.

    ### Earned Media & Press Outreach
    - WWLP-22News: broadcast coverage.
    - NEPM (PBS/NPR): regional trust and reach.
    - The Chicopee Register: hyper-local coverage.
    - WACE 730 AM: faith-based and local listenership.
    - Patch.com – Chicopee: digital visibility hub.

    *Check the dates for accuracy. Regular searching of community calendars and websites is recommended to find new events.*
    ```
    """
    
    _api_semaphore = asyncio.Semaphore(10) # this is set to call tavily in parallel but not overload the system
    
    def __init__(self):
        """Initialize the generator with necessary clients and logger."""
        self.logger = get_logger(__name__)
        self.tavily_client = SharedTavilyClient()
        self.llm_client = LLMClient()
        
        self.logger.info("KnowYourCommunityGenerator initialized")

    async def _generate_media_outreach_section(self, cleaned_campaign_info: CleanedCampaignInfo) -> str:
        """
        Generate the Earned Media & Press Outreach section.
        
        Args:
            cleaned_campaign_info: Cleaned campaign information
            
        Returns:
            str: Generated media outreach section
        """
        self.logger.info(f"Generating media outreach section for candidate: {cleaned_campaign_info.candidate_name}")
        
        try:
            media_info = await self._fetch_media_press_outreach_for_campaign(cleaned_campaign_info)
            
            media_prompt = f"""
You are an expert campaign strategist. Generate a media outreach section for this campaign.

CAMPAIGN CONTEXT:
- Office: {cleaned_campaign_info.office_and_jurisdiction}

MEDIA INFO:
{media_info}

RULES:
- Maximum 7 media outlets
- Pick the most relevant media outlets for the campaign
- Give specific outlet names where available

REQUIRED FORMAT: Each line must start with " - " (space-dash-space) followed by outlet name: brief description.

EXAMPLES OF CORRECT FORMAT:
 - WWLP-22News: broadcast coverage.
 - NEPM (PBS/NPR): regional trust and reach.
 - The Chicopee Register: hyper-local coverage.
 - WACE 730 AM: faith-based and local listenership.
 - Patch.com – Chicopee: digital visibility hub.

OUTPUT REQUIREMENTS:
- Use exactly " - " (space-dash-space) at the start of each line
- Format: " - Outlet Name: brief description."
- Keep descriptions concise (under 10 words)
- Do not use asterisks (*) or other bullet formats
- Do not include any other text besides the bullet points

CRITICAL: Return ONLY the bullet points in the exact format shown above. Do not include any explanations, corrections, or thinking process. Do not show your work. Just return the clean bullet points with consistent " - " formatting.

FINAL REMINDER: Start EVERY line with the exact character sequence: [space][dash][space]
"""
            
            self.logger.debug("Generating media outreach content")
            
            media_response = self.llm_client.create_completion(
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert campaign strategist. CRITICAL: Every line must start with exactly ' - ' (space-dash-space). Return ONLY clean bullet points with no explanations, corrections, or thinking process."
                    },
                    {
                        "role": "user",
                        "content": media_prompt
                    }
                ],
                max_tokens=10000,
                temperature=0.0
            )
            
            media_content = media_response.choices[0].message.content
            
            return f"""
### Earned Media & Press Outreach
{media_content}
"""
            
        except Exception as e:
            self.logger.error(f"Failed to generate media outreach section: {str(e)}")
            return "## Earned Media & Press Outreach\n - Unable to generate media outreach information at this time."

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

    async def _generate_community_events_section(self, cleaned_campaign_info: CleanedCampaignInfo) -> str:
        """
        Generate the Community Events & Civic Presence section.
        
        Args:
            cleaned_campaign_info: Cleaned campaign information
            
        Returns:
            str: Generated community events section
        """
        self.logger.info(f"Generating community events section for candidate: {cleaned_campaign_info.candidate_name}")
        
        try:
            community_events = await self._fetch_community_events_for_campaign(cleaned_campaign_info)
            self.logger.info("Filtering events for best voter reach potential")
            filtered_formatted_list = await self._filter_best_events(community_events, cleaned_campaign_info)
            
            return f"""
### Community Events & Civic Presence
{filtered_formatted_list}
"""
            
        except Exception as e:
            self.logger.error(f"Failed to generate community events section: {str(e)}")
            return "## Community Events & Civic Presence\n - Unable to generate community events at this time."

    async def _generate_search_terms(self, cleaned_campaign_info: CleanedCampaignInfo) -> list[str]:
        """
        Generate 3 targeted search terms based on campaign information.
        
        Args:
            cleaned_campaign_info: Cleaned campaign information
            
        Returns:
            list[str]: 3 targeted search terms
        """
        now = date.today()
        self.logger.info(f"Generating search terms for candidate: {cleaned_campaign_info.candidate_name}")
        
        location = cleaned_campaign_info.city + ", " + cleaned_campaign_info.state_full
        
        try:
            search_terms_prompt = f"""
Generate 3 web search terms to find community events where a political candidate can connect with voters.

today's date: {now}

CAMPAIGN CONTEXT:
- Office: {cleaned_campaign_info.office_and_jurisdiction}
- Location: {location}
- Election Date: {cleaned_campaign_info.election_date}

REQUIREMENTS:
- Target events where candidates can speak or engage voters
- this can be a mix of formal meetings and community events
- Make searches specific enough to find real events with dates

EXAMPLES:
-"Chicopee MA community events {now.month} 2025"
-"Chicopee MA local events school meetings Fall 2025"
-"Chicopee MA school district town hall meetings Fall 2025"

Return exactly 3 search terms, one per line, with no bullets or formatting.
"""
            
            self.logger.debug("Generating targeted search terms")
            
            search_terms_response = self.llm_client.create_structured_completion(
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert campaign strategist. Generate exactly 3 search terms for finding community events where political candidates can connect with voters."
                    },
                    {
                        "role": "user",
                        "content": search_terms_prompt
                    }
                ],
                response_schema=SearchTermsList,
                max_tokens=10000,
            )
            
            search_terms = search_terms_response.search_terms
                        
            if len(search_terms) < 3:
                self.logger.warning(f"Generated only {len(search_terms)} search terms, filling with defaults")
                search_terms.extend([
                    f"community events {location} with dates from {now.month} {now.year}",
                    f"town meetings {location} with dates from {now.month} {now.year}",
                    f"civic events {location} with dates from {now.month} {now.year}"
                ])
            
            search_terms = search_terms[:3] 
            
            self.logger.info(f"Generated search terms: {search_terms}")
            return search_terms
            
        except Exception as e:
            self.logger.error(f"Failed to generate search terms: {str(e)}")
            return [
                f"community events {location} with dates from {now.month} {now.year}",
                f"town meetings {location} with dates from {now.month} {now.year}",
                f"civic events {location} with dates from {now.month} {now.year}"
            ]

    async def _fetch_community_events_for_campaign(self, cleaned_campaign_info: CleanedCampaignInfo) -> str:
        """
        Fetch community events for the campaign using AI-generated search terms.
        """
        self.logger.info(f"Fetching community events for candidate: {cleaned_campaign_info.candidate_name}")
        self.logger.debug(f"Campaign info details: {cleaned_campaign_info}")
        
        try:
            today = date.today()
            election_date = cleaned_campaign_info.election_date
            
            if election_date < today:
                self.logger.warning(f"Election date {election_date} is in the past.")
                return "Election date is in the past. Please provide a future election date."

            search_terms = await self._generate_search_terms(cleaned_campaign_info)
            search_terms.append(
                f"Community Events {cleaned_campaign_info.city} {cleaned_campaign_info.state_full} with dates {today.month} {today.year}"
            )

            search_tasks = []
            for search_term in search_terms:
                async def search_task(query=search_term):
                    async with self._api_semaphore:
                        return await self.tavily_client.get_search_context(
                            query=query,
                            search_depth="basic",
                            max_results=5
                        )
                search_tasks.append(search_task())
            
            self.logger.info(f"Executing {len(search_tasks)} targeted searches...")
            search_results = await asyncio.gather(*search_tasks, return_exceptions=True)
            
            self.logger.debug(f"Search results: {search_results}")

            all_events = []
            for i, search_term in enumerate(search_terms):
                result = search_results[i]
                
                if isinstance(result, Exception):
                    self.logger.error(f"Search failed for '{search_term}': {result}")
                    all_events.append(f"\n=== {search_term} ===\nError: {result}")
                else:
                    all_events.append(f"\n=== {search_term} ===\n{result}")
            
            combined_events = "\n".join(all_events)
            
            self.logger.info("Successfully fetched targeted community events")
            self.logger.debug(f"Combined events: {combined_events}")
            return combined_events
            
        except Exception as e:
            self.logger.error(f"Failed to fetch community events: {str(e)}")
            return f"Error fetching community events: {str(e)}"

    async def _filter_best_events(self, community_events: str, cleaned_campaign_info: CleanedCampaignInfo) -> str:
        """
        Filter community events to select the best ones based on voter reach potential.
        
        Args:
            community_events: Raw community events data
            cleaned_campaign_info: Cleaned campaign information
            
        Returns:
            str: Filtered events information
        """
        self.logger.info(f"Filtering events for candidate: {cleaned_campaign_info.candidate_name}")
        self.logger.debug(f"Raw events length: {len(community_events)} characters")
        
        try:
            filter_prompt = f"""
You are an expert campaign strategist. Filter and select the up to 8 community events for this campaign.
pick a minimum of 5 events.

CAMPAIGN CONTEXT:
- Office: {cleaned_campaign_info.office_and_jurisdiction}
- Election Date: {cleaned_campaign_info.election_date}
- Today's Date: {date.today()}

COMMUNITY EVENTS DATA:
{community_events}

SELECTION CRITERIA:
- Only include events that have not already occurred (after today's date)
- Only include events before the election date
- Prioritize events where candidates can speak or meet potential voters
- Include a mix of formal meetings and community events

DATE FORMATTING REQUIREMENTS:
- Convert any dates to readable format (July 9, 2025)

CHRONOLOGICAL ORDER REQUIREMENT:
- Events MUST be listed in strict chronological order (earliest date first)

FORMAT: Each line starts with " - " followed by event name (clean date): brief description.

EXAMPLES:
 - City Council Meeting (July 8, 2025): opportunity to address community concerns.
 - School Board Meeting (July 15, 2025): engage with education stakeholders.
 - Community Forum (September 10, 2025): voter outreach and visibility.

CRITICAL: Return ONLY the bullet points sorted by date (earliest first). Clean up messy date formats. No explanations or additional text.
"""
            
            self.logger.debug("Sending events for AI filtering")
            
            filter_response = self.llm_client.create_completion(
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert campaign strategist. CRITICAL: Every line must start with exactly ' - ' (space-dash-space). Return ONLY clean bullet points with no explanations, corrections, or thinking process."
                    },
                    {
                        "role": "user",
                        "content": filter_prompt
                    }
                ],
                max_tokens=20000,
                temperature=0.0
            )
            
            filtered_events_text = filter_response.choices[0].message.content
            self.logger.info(f"AI filtering completed.")
            self.logger.debug(f"Filtered events text length: {len(filtered_events_text)} characters")
            
            return filtered_events_text
            
        except Exception as e:
            self.logger.error(f"Failed to filter events: {str(e)}")
            self.logger.warning("Falling back to original events data")
            return community_events

    async def generate_section(self, cleaned_campaign_info: CleanedCampaignInfo) -> str:
        """
        Generate the complete 'Know Your Community' section by assembling the sub-sections.
        
        Args:
            cleaned_campaign_info: Cleaned campaign information
            
        Returns:
            str: Generated complete community section
            
        Raises:
            Exception: If community section generation fails
        """
        self.logger.info(f"Starting community section generation for candidate: {cleaned_campaign_info.candidate_name}")
        self.logger.debug(f"Campaign info details: {cleaned_campaign_info}")
        
        try:
            self.logger.debug("Generating community events and media outreach sections in parallel")
            
            community_events_task = self._generate_community_events_section(cleaned_campaign_info)
            media_outreach_task = self._generate_media_outreach_section(cleaned_campaign_info)
            
            community_events_section, media_outreach_section = await asyncio.gather(
                community_events_task, 
                media_outreach_task
            )
            
            complete_section ="\n".join([
                "## 5. KNOW YOUR COMMUNITY",
                community_events_section,
                media_outreach_section,
                "*Check the dates for accuracy. Regular searching of community calendars and websites is recommended.*"
            ])

            self.logger.info("Successfully generated complete community section")
            self.logger.debug(f"Generated section length: {len(complete_section)} characters")
            
            return complete_section
            
        except Exception as e:
            self.logger.error(f"Failed to generate community section: {str(e)}")
            self.logger.debug(f"Exception details: {type(e).__name__}: {e}", exc_info=True)
            raise

if __name__ == "__main__":
    logger = get_logger(__name__)
    logger.info("Starting five_know_your_community module in standalone mode")
    
    logger.debug("Creating sample campaign info")
    campaign_info = CampaignInfo(
        candidate_name="John Doe",
        primary_date=date(2025, 7, 10),
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
    
    utils = CampaignUtils()
    cleaned_campaign_info = utils.clean_campaign_info(campaign_info)
    
    logger.debug("Campaign info cleaned successfully")
    logger.info("Generating community section")
    
    generator = KnowYourCommunityGenerator()
    result = asyncio.run(generator.generate_section(cleaned_campaign_info))

    example_search_output = """
=== Chicopee MA community events school related Fall 2025 ===

Search Results:

1. Holiday: Fall events Events & Tickets in Chicopee, MA | Eventbrite
   URL: https://www.eventbrite.com/b/ma--chicopee/holiday/fall-events/
   Content: Classes ; Autumn Candle-Making Workshop at Mighty Squirrel Brewing Co. – Waltham. Mon, Sep 22 • 6:00 PM. Mighty Squirrel Brewing Co. From $47.73 ; Autumn Wreath...

2. Events around Chicopee, MA - Community Calendar - AARP Local
   URL: https://local.aarp.org/chicopee-ma/events/
   Content: Image 4: spinner imageImage 5: the help icon Image 6: spinner imageImage 7: the help icon Image 8: spinner imageImage 9: AARP's Eye Center Image 58: spinner imageImage 59: illustration of a map with an icon of a person helping another person with a cane navigate towards caregiving Image 64: spinner imageImage 65: AARP Right Again Trivia and AARP Rewards Image 66: spinner imageImage 67: AARP Right Again Trivia Sports and AARP Rewards Image 70: spinner imageImage 71: Throwback Thursday Crossword a...

3. Calendar • Chicopee, MA • CivicEngage
   URL: https://www.chicopeema.gov/Calendar.aspx
   Content: 1.   - [x]  All Calendars   6.   - [x]  City Events Calendar   1.   - [x]  Meetings Calendar   July 8,2025,4:30 PM @ Fire Department Headquarters   Ambulance Commission Meeting Notice 2025-07-08T16:30:00 Fire Department Headquarters 80 Church Street Chicopee MA 01020 More Details July 9,2025,6:30 PM @ Chicopee City Hall Auditorium   Chicopee School Committee Meeting Notice 2025-07-09T18:30:00 Chicopee City Hall Auditorium 274 Front St.Chicopee MA 01013 More Details July 10,2025,11:00 AM @ Board ...

4. Chicopee Public Schools: Home
   URL: https://www.chicopeeps.org/en-US
   Content: | |  |  |  |  | | --- | --- | --- | | |  | | --- | | Superintendent Ware Congratulates Freshmen in Carpentry    During a recent visit to Chicopee Comprehensive High School, Superintendent Ware stopped by the Carpentry shop and recognized freshmen students who were proudly receiving acceptance into the program—displayed in certificate frames they built themselves. We look forward to sharing more updates soon as we continue building a strong, shared vision for the future of Chicopee Public Schools...

5. Athletics - Chicopee Public Schools
   URL: https://www.chicopeeps.org/en-US/athletics-ca110258
   Content: All student athletes participating in athletics for the Chicopee Public Schools must register online. Below are detailed instructions for registration....

=== Chicopee MA public forums school district 2025 ===

Search Results:

1. 2024-2025 School Calendar - Chicopee Public Schools
   URL: https://www.chicopeeps.org/en-US/printable-calendar-9f32c23b/2024-2025-school-calendar-e05bda76
   Content: 2024-2025 School Calendar - Chicopee Public Schools Language Select your language Set your preferred language. Language English Powered by Image 1: Google TranslateTranslate skip to main contentskip to navigation *    Tech Support *    Menus *    School Brains Portal *    Athletics *   Skip to Navigation Image 2: Chicopee Public Schools Chicopee Public Schools ======================= Login *   Links & Resources *   Departments *   Calendars *   Schools *   Staff Directory *   Contact Printable C...

2. Chicopee School Committee approves School Choice slots
   URL: https://thereminder.com/local-news/hampden-county/chicopee/chicopee-school-committee-approves-school-choice-slots/
   Content: The School Committee unanimously approved offering 31 School Choice slots for grades 1 through 5 for the 2025-26 school year....

3. Chicopee Public Schools - Facebook
   URL: https://m.facebook.com/story.php/?story_fbid=1023178026482973&id=100063723542962
   Content: Summer is winding down and we are gearing up for the start of the 2024-2025 school year! As a friendly reminder, the First Day of School for Grades 1-12...

4. Chicopee Area Community Forum | Facebook
   URL: https://www.facebook.com/groups/1337735189714040/
   Content: Log into Facebook Image 1: Facebook _Notice_ You must log in to continue. Log Into Facebook You must log in to continue. Log In Forgot account? Create new account *   English (US) *   Español *   Français (France)") *   中文(简体)") *   العربية *   Português (Brasil)") *   Italiano *   한국어 *   Deutsch *   हिन्दी *   日本語 *   Sign Up *   Log In *   Messenger *   Facebook Lite *   Video *   Meta Pay *   Meta Store *   Meta Quest *   Ray-Ban Meta *   Meta AI *   Instagram *   Threads *   Voting Informat...

5. Chicopee Public Schools: Home
   URL: https://www.chicopeeps.org/en-US
   Content: | |  |  |  |  | | --- | --- | --- | | |  | | --- | | Superintendent Ware Congratulates Freshmen in Carpentry    During a recent visit to Chicopee Comprehensive High School, Superintendent Ware stopped by the Carpentry shop and recognized freshmen students who were proudly receiving acceptance into the program—displayed in certificate frames they built themselves. We look forward to sharing more updates soon as we continue building a strong, shared vision for the future of Chicopee Public Schools...

=== Chicopee MA local civic meetings education topics 2025 ===

Search Results:

1. Meetings Calendar - Chicopee, MA
   URL: https://www.chicopeema.gov/calendar.aspx?CID=39
   Content: July 9, 2025, 6:30 PM. @. Chicopee City Hall Auditorium. Chicopee School Committee Meeting Notice2025-07-09T18:30:00. Chicopee City Hall Auditorium274 Front St....

2. Agenda Center - Chicopee, MA
   URL: https://www.chicopeema.gov/AgendaCenter
   Content: | ### **Apr 1, 2025** — Posted Mar 28, 2025 1:22 PM City Council Meeting (PDF) | Image 12: Minutes Opens in new windowAgenda center file City Council Meeting (PDF) | Download▼ City Council Meeting (PDF) 1. | ### **Mar 4, 2025** — Posted Feb 28, 2025 12:21 PM City Council Meeting Agenda (PDF) | Image 15: Minutes Opens in new windowAgenda center file City Council Meeting Agenda (PDF) | Download▼ City Council Meeting Agenda (PDF) 1. | ### **Feb 4, 2025** — Posted Jan 31, 2025 1:31 PM City Council M...

3. 6-3-25 City Council - YouTube
   URL: https://www.youtube.com/watch?v=XwgLiWjNhcw
   Content: Chicopee TV New 8 views · 2:30. Go to channel · Councilwoman Michele Fiore walks out of City Council meeting as public comment gets heated. 8...

4. Chicopee Public Schools: Home
   URL: https://www.chicopeeps.org/en-US
   Content: | |  |  |  |  | | --- | --- | --- | | |  | | --- | | Superintendent Ware Congratulates Freshmen in Carpentry    During a recent visit to Chicopee Comprehensive High School, Superintendent Ware stopped by the Carpentry shop and recognized freshmen students who were proudly receiving acceptance into the program—displayed in certificate frames they built themselves. We look forward to sharing more updates soon as we continue building a strong, shared vision for the future of Chicopee Public Schools...

=== Community Events Chicopee Massachusetts with dates from 7 2025 ===

Search Results:

1. Calendar • Chicopee, MA • CivicEngage
   URL: https://www.chicopeema.gov/Calendar.aspx
   Content: 1.   - [x]  All Calendars   6.   - [x]  City Events Calendar   1.   - [x]  Meetings Calendar   July 8,2025,4:30 PM @ Fire Department Headquarters   Ambulance Commission Meeting Notice 2025-07-08T16:30:00 Fire Department Headquarters 80 Church Street Chicopee MA 01020 More Details July 9,2025,6:30 PM @ Chicopee City Hall Auditorium   Chicopee School Committee Meeting Notice 2025-07-09T18:30:00 Chicopee City Hall Auditorium 274 Front St.Chicopee MA 01013 More Details July 10,2025,11:00 AM @ Board ...

2. Event List | Chicopee Chamber of Commerce
   URL: https://business.chicopeechamber.org/events?from=10/24/2024&to=10/24/2027&o=alpha
   Content: Event List | Chicopee Chamber of Commerce *   Business Resources *   2023 Business Resource Guide & Member Directory 2024 Business After Hours at Goodwork... ##### 2024 Business After Hours at Goodworks Coffee House *   Wednesday Dec 4, 2024 Categories:Chamber Of Commerce Networking Categories:Festivals & Celebrations Arts & Culture Community Chamber Of Commerce Networking Categories:Festivals & Celebrations Arts & Culture Community Chamber Of Commerce Networking Categories:Festivals & Celebrati...

3. Chicopee, MA Events, Calendar & Tickets | Eventbrite
   URL: https://www.eventbrite.com/d/ma--chicopee/events/
   Content: Chicopee, MA Events, Calendar & Tickets | Eventbrite  *       *   Find Events *   Event Ticketing *       *   Find Events *   Event Ticketing 4.   Events in Chicopee Image 19: Career Fair: Exclusive Tech Hiring Event-New Tickets Available primary image Image 27: Career Fair: Exclusive Tech Hiring Event-New Tickets Available primary image _Save this event: Project Management Techniques Training in Chicopee, MA_ _Share this event: Project Management Techniques Training in Chicopee, MA_ _Save this ...

4. Community Events in Chicopee, MA - Local Gatherings & Activities
   URL: https://www.eventbrite.com/b/ma--chicopee/community/
   Content: Community Events in Chicopee, MA - Local Gatherings & Activities | Eventbrite  *       *   Find Events *   Event Ticketing *   Event Types Event Types *   Online Events *   Contact your event organizer *       *   Find Events *   Event Ticketing *   Event Types *   Online Events *   Contact your event organizer 4.   Events in Chicopee/ Community events *   Image 3: LGBTQIA+ events LGBTQIA+ *   Image 4: State events State *   Image 5: Historic events Historic *   Image 6: Heritage events Heritage...

5. Calendar • Golf Commission Meeting Notice - Chicopee, MA
   URL: https://www.chicopeema.gov/Calendar.aspx?EID=8298
   Content: Image 1: Loading Image 4 - [x] Show Past Events Select a Calendar 1.   - [x]  All Calendars   2.   - [x]  Building Department   3.   - [x]  Chicopee Public Library   4.   - [x]  City Clerks   5.   - [x]  City Council   6.   - [x]  City Events Calendar   7.   - [x]  Community Development   2.   - [x]  Engineering   3.   - [x]  Fire Department   4.   - [x]  Health Department   7.   - [x]  License Commission   1.   - [x]  Meetings Calendar   3.   - [x]  Planning Department   4.   - [x]  Police Depa...
   """


    result = asyncio.run(generator._filter_best_events(example_search_output, cleaned_campaign_info))
    print(result)