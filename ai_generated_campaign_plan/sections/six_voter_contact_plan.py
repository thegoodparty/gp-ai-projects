import asyncio
from datetime import date, timedelta
from ai_generated_campaign_plan.schema.models import CleanedCampaignInfo, ContactOptimization, IncumbentStatus
from shared.logger import get_logger
from shared.llm import LLMClient



class VoterContactPlanGenerator:
    
    def __init__(self):
        self.llm_client = LLMClient()
        self.logger = get_logger(__name__)
    

    
    async def generate_section(self, campaign_info: CleanedCampaignInfo, primary_contact_strategy: ContactOptimization = None, general_contact_strategy: ContactOptimization = None) -> str:
        self.logger.info(f"Generating voter contact plan for {campaign_info.candidate_name}")
        
        has_primary = campaign_info.has_primary
        
        if has_primary:            
            prompt = f"""
today's date: {date.today()}
primary date: {campaign_info.primary_date}
general election date: {campaign_info.election_date}

CAMPAIGN CONTEXT:
- Candidate: {campaign_info.candidate_name}
- Office: {campaign_info.office_and_jurisdiction}
- Has Primary: Yes

RULES:
- the number of texts before the primary should not exceed {primary_contact_strategy.p2p_texts}
- the number of robocalls before the primary should not exceed {primary_contact_strategy.robocalls}
- the number of texts after the primary should not exceed {general_contact_strategy.p2p_texts}
- the number of robocalls after the primary should not exceed {general_contact_strategy.robocalls}
- one set of text messaging and robocalls should be schedules close to the election to remind of the polling location and to vote
- there are two parts, one before the primary and one after the primary, 
- These are some examples of the messages that can be used before the primary:
    - First Text before primary: Candidate intro and vote-by-mail awareness if applicable
    - First Robocall before primary: Ballot arrival and early voting prompt
    - Second Text before primary: Experience and contrast message
    - Second Robocall before primary: Vote return and community message
    - Third Text before primary: Persuasion and vote planning
    - Last Robocall before primary: Final GOTV push and polling info
    - Last Text before primary: Final GOTV reminder
 - These are some examples of the messages that can be used after the primary:
    - First Text after primary: Reintroduction and contrast message
    - First Robocall after primary: Early voting alert
    - Second Text after primary: Key issues and voter education
    - Second Robocall after primary: Final persuasion
    - Third Text after primary: Vote-by-mail deadline and GOTV push
    - Last Text after primary: Final reminder and polling location link
    - Last Robocall after primary: Election Day GOTV
 - If there are two texts or robocalls, take example from first and last text or robocall

Generate a voter contact plan with both primary and general election phases in this format:
[List contacts chronologically from today through primary, then general election]

Example format:
- [FULL MONTH DD] – P2P Text #1: [Message theme]
- [FULL MONTH DD] – Robocall #1: [Message theme]
- [Primary Date] – Primary Election Day
- [FULL MONTH DD] – P2P Text #[N]: [Message theme for general]
- [Election Date] – General Election Day
"""
        else:            
            prompt = f"""
today's date: {date.today()}
general election date: {campaign_info.election_date}

CAMPAIGN CONTEXT:
- Candidate: {campaign_info.candidate_name}
- Office: {campaign_info.office_and_jurisdiction}
- Has Primary: No

RULES:
- Only include {general_contact_strategy.p2p_texts} texts and {general_contact_strategy.robocalls} robocalls
- one set of text messaging and robocalls should be schedules close to the election to remind of the polling location and to vote
- List contacts chronologically from today through general election
- These are some examples of the messages that can be used:
        - Early Text – Voter intro + early voting alert
        - Early Robocall – Candidate intro + race message
        - Mid campaign Text – Contrast/persuasion message
        - Mid campaign Robocall – Polling info + persuasion
        - Late campaign Text – Final early vote push
        - Final Text – Election Day reminder + poll finder
        - Final Robocall – Final GOTV call (morning)

Generate a voter contact plan for general election only in this format:

Example format:
- [FULL MONTH DD] – P2P Text #1: [Message theme]
- [FULL MONTH DD] – Robocall #1: [Message theme]
- [Election Date] – General Election Day
"""
        try:
            response = self.llm_client.create_completion(
                messages=[
                    {"role": "system", "content": "You are a campaign strategist. Generate the voter contact plan in the exact format shown. Do not add thinking or reasoning."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=5000
            )
            
            voter_contact_plan = response.choices[0].message.content

            self.logger.info(f"Generated voter contact plan: {voter_contact_plan}")
            return "\n".join([
                "## 6. VOTER CONTACT PLAN",
                "### Core Tactics (Chronological)",
                voter_contact_plan,

            ])
        except Exception as e:
            self.logger.error(f"Error generating voter contact plan: {str(e)}")
            return "## 6. VOTER CONTACT PLAN\n\n[Error generating voter contact plan]"
            


if __name__ == "__main__":
    from ai_generated_campaign_plan.schema.models import CampaignInfo
    from ai_generated_campaign_plan.utils.utils import CampaignUtils
    campaign_utils = CampaignUtils()
    generator = VoterContactPlanGenerator()

    print("=== TEST CASE 1: CAMPAIGN WITH PRIMARY ===")
    campaign_info_with_primary = CampaignInfo(
        candidate_name="John Doe",
        office_and_jurisdiction="Mayor, Springfield, MA",
        election_date=date(2025, 11, 5),
        primary_date=date(2025, 8, 1),
        race_type="Nonpartisan",
        seats_available=1,
        number_of_opponents=2,
        win_number=15000,
        total_likely_voters=100000,
        available_cell_phones=10000,
        available_landlines=1000,
        incumbent_status=IncumbentStatus.NOT_APPLICABLE,
        additional_race_context="Focus on education funding and infrastructure improvements"
    )

    cleaned_campaign_info_with_primary = campaign_utils.clean_campaign_info(campaign_info_with_primary)
    primary_campaign_plan = campaign_utils.optimize_contact_strategy(date.today(), cleaned_campaign_info_with_primary.primary_date)
    general_campaign_plan = campaign_utils.optimize_contact_strategy(cleaned_campaign_info_with_primary.primary_date, cleaned_campaign_info_with_primary.election_date)
    result_with_primary = asyncio.run(generator.generate_section(cleaned_campaign_info_with_primary, primary_campaign_plan, general_campaign_plan))
    print(result_with_primary)
    
    print("\n" + "="*60 + "\n")
    
    print("=== TEST CASE 2: CAMPAIGN WITHOUT PRIMARY ===")
    campaign_info_no_primary = CampaignInfo(
        candidate_name="Jane Smith",
        office_and_jurisdiction="City Council, District 3, Boston, MA",
        election_date=date(2025, 7, 22),
        primary_date=None,
        race_type="Nonpartisan",
        seats_available=1,
        number_of_opponents=3,
        win_number=8000,
        total_likely_voters=50000,
        available_cell_phones=5000,
        available_landlines=500,
        incumbent_status=IncumbentStatus.NOT_APPLICABLE,
        additional_race_context="Focus on neighborhood safety and small business support"
    )

    cleaned_campaign_info_no_primary = campaign_utils.clean_campaign_info(campaign_info_no_primary)
    general_campaign_plan = campaign_utils.optimize_contact_strategy(date.today(), cleaned_campaign_info_no_primary.election_date)
    result_no_primary = asyncio.run(generator.generate_section(cleaned_campaign_info_no_primary, general_contact_strategy=general_campaign_plan))
    print(result_no_primary)