from datetime import date

from ai_generated_campaign_plan.schema.models import (
    CampaignInfo,
    IncumbentStatus,
)
from shared.llm import LLMClient
from shared.logger import get_logger


class StrategicLandscapeElectoralGoalsGenerator:
    """
    A class to generate the 'Strategic Landscape & Electoral Goals' section of a campaign plan.
    This section includes:
    - Strategic landscape analysis with opportunities and challenges
    - Electoral goals with win numbers, voter contact goals, and contactable universe
    """

    def __init__(self):
        """Initialize the generator with necessary clients and logger."""
        self.logger = get_logger(__name__)
        self.llm_client = LLMClient()

        self.logger.info("StrategicLandscapeElectoralGoalsGenerator initialized")

    def _generate_electoral_goals(self, campaign_info: CampaignInfo) -> str:
        """
        Generate electoral goals section directly from campaign data.
        
        Args:
            campaign_info (CampaignInfo): Campaign information containing electoral data
        
        Returns:
            str: Formatted electoral goals section text
        """
        self.logger.debug(f"Generating electoral goals for {campaign_info.candidate_name}")

        win_number = campaign_info.win_number
        total_likely_voters = campaign_info.total_likely_voters
        voter_contact_goal = int(win_number * 3)
        cell_phones = campaign_info.available_cell_phones
        landlines = campaign_info.available_landlines

        self.logger.debug(f"Electoral calculations - Win: {win_number}, Total Voters: {total_likely_voters}, Contact Goal: {voter_contact_goal}")

        electoral_goals_text = f"""
Win Number: {win_number:,}
 - Likely Voters: {total_likely_voters:,}
 - Voter Contact Goal: {voter_contact_goal:,}
 
Contactable Universe:
 - Cell Phones: {cell_phones:,}
 - Landlines: {landlines:,}
"""

        self.logger.info(f"Generated electoral goals for {campaign_info.candidate_name}")
        return electoral_goals_text

    def _generate_strategic_landscape(self, campaign_info: CampaignInfo) -> str:
        """
        Generate strategic landscape section using LLM analysis.
        
        Args:
            campaign_info (CampaignInfo): Campaign information including incumbent status and race context
        
        Returns:
            str: Formatted strategic landscape section text
        """
        self.logger.debug(f"Generating strategic landscape for {campaign_info.candidate_name}")

        incumbent_context = ""
        if campaign_info.incumbent_status == IncumbentStatus.ELECTED:
            incumbent_context = "The candidate is an elected incumbent who won their previous election."
        elif campaign_info.incumbent_status == IncumbentStatus.APPOINTED:
            incumbent_context = "The candidate is an appointed incumbent who was selected to fill the seat but has not run for election before."
        elif campaign_info.incumbent_status == IncumbentStatus.NOT_APPLICABLE:
            incumbent_context = "The candidate is not an incumbent and this is a new race for them."

        prompt = f"""
You are an expert campaign strategist. Generate a Strategic Landscape section that identifies opportunities and challenges for this candidate.

CANDIDATE INFORMATION:
- Name: {campaign_info.candidate_name}
- Office: {campaign_info.office_and_jurisdiction}
- Race Type: {campaign_info.race_type}
- Seats Available: {campaign_info.seats_available}
- Number of Opponents: {campaign_info.number_of_opponents}
- Incumbent Status: {incumbent_context}
- Additional Race Context: {campaign_info.additional_race_context or "No additional context provided"}

CRITICAL RULES:
1. If the candidate is an APPOINTED incumbent, they hold the office but this is their first time running for ELECTION (not contradictory)
2. If the candidate is an ELECTED incumbent, they have both held office and run for election before
3. Opportunities and challenges must be logically consistent with each other
4. Focus on realistic campaign dynamics based on the specific incumbent status
5. Be specific to the race type and office level

Generate ONLY the section in this exact format:

Opportunities:

[List 3-4 specific opportunities, each as a bullet point]

Challenges:

[List 3-4 specific challenges, each as a bullet point]
"""

        self.logger.debug("Sending strategic landscape generation request to LLM")

        try:
            response = self.llm_client.create_completion(
                max_tokens=10000,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert campaign strategist. Generate strategic analysis that is logically consistent and specific to the candidate's situation.",
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                temperature=0.1,
            )

            result = response.choices[0].message.content.strip()

            self.logger.info(f"Successfully generated strategic landscape for {campaign_info.candidate_name}")
            self.logger.debug(f"Strategic landscape length: {len(result)} characters")

            return result

        except Exception as e:
            self.logger.error(f"Failed to generate strategic landscape: {e!s}")
            raise

    def generate_section(self, campaign_info: CampaignInfo) -> str:
        """
        Generate the complete Strategic Landscape & Electoral Goals section.
        
        Args:
            campaign_info (CampaignInfo): Campaign information
        
        Returns:
            str: Complete formatted section combining strategic landscape and electoral goals
            
        Raises:
            Exception: If section generation fails
        """
        self.logger.info(f"Starting Strategic Landscape & Electoral Goals generation for {campaign_info.candidate_name}")
        self.logger.debug(f"Campaign info details: {campaign_info}")

        try:
            self.logger.debug("Beginning strategic landscape and electoral goals analysis process")

            strategic_landscape = self._generate_strategic_landscape(campaign_info)
            electoral_goals = self._generate_electoral_goals(campaign_info)

            complete_section = f"""
## 2. STRATEGIC LANDSCAPE & ELECTORAL GOALS

### Strategic Landscape

{strategic_landscape}

### Electoral Goals

{electoral_goals}"""

            self.logger.info("Successfully generated complete Strategic Landscape & Electoral Goals section")
            self.logger.debug(f"Complete section length: {len(complete_section)} characters")

            return complete_section

        except Exception as e:
            self.logger.error(f"Failed to generate Strategic Landscape & Electoral Goals section: {e!s}")
            self.logger.debug(f"Exception details: {type(e).__name__}: {e}", exc_info=True)
            raise


if __name__ == "__main__":
    logger = get_logger(__name__)
    logger.info("Starting two_strategic_landscape_electoral_goals module in standalone mode")

    try:
        logger.debug("Creating sample campaign info")
        example_campaign = CampaignInfo(
            candidate_name="Mark Johnson",
            election_date=date(2024, 11, 5),
            office_and_jurisdiction="School Board, At-Large, Chicopee, MA",
            incumbent_status=IncumbentStatus.APPOINTED,
            race_type="Nonpartisan",
            seats_available=3,
            number_of_opponents=5,
            win_number=4213,
            total_likely_voters=8426,
            available_cell_phones=4505,
            available_landlines=3780,
            additional_race_context="Well-networked across Chicopee with strong community ties. Message focuses on earning the right to take a full swing at the pitch."
        )

        logger.debug(f"Campaign info created: {example_campaign.candidate_name}")
        logger.info("Generating Strategic Landscape & Electoral Goals section")

        generator = StrategicLandscapeElectoralGoalsGenerator()
        result = generator.generate_section(example_campaign)

        logger.info("Module execution completed successfully")
        print(result)

    except Exception as e:
        logger.error(f"Module execution failed: {e!s}")
        logger.debug(f"Exception in main: {type(e).__name__}: {e}", exc_info=True)
        raise
