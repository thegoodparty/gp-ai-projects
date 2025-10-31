import asyncio
from datetime import date, timedelta
from ai_generated_campaign_plan.schema.models import CleanedCampaignInfo, ContactOptimization, IncumbentStatus
from shared.logger import get_logger
from shared.llm_gemini import GeminiClient

class VoterContactPlanGenerator:
    
    def __init__(self):
        self.llm_client = GeminiClient()
        self.logger = get_logger(__name__)
    

    
    async def generate_section(self, campaign_info: CleanedCampaignInfo, primary_contact_strategy: ContactOptimization = None, general_contact_strategy: ContactOptimization = None) -> str:
        self.logger.info(f"Generating voter contact plan for {campaign_info.candidate_name}")
        
        has_primary = campaign_info.has_primary
        
        if has_primary:    
            # Calculate Phase 1 start date:
            phase_1_7w_date = campaign_info.primary_date - timedelta(weeks=7)
            phase_1_start_date_3d_buffer = date.today() + timedelta(days=3)
            phase_1_start_date = phase_1_start_date_3d_buffer if phase_1_7w_date < phase_1_start_date_3d_buffer else phase_1_7w_date
            phase_1_final_task_1d_buffer = campaign_info.primary_date - timedelta(days=1)
            # Calculate Phase 2 start date:
            phase_2_7w_date = campaign_info.election_date - timedelta(weeks=7)
            phase_2_start_date_1d_buffer = campaign_info.primary_date + timedelta(days=1)
            phase_2_start_date = phase_2_start_date_1d_buffer if phase_2_7w_date < phase_2_start_date_1d_buffer else phase_2_7w_date
            phase_2_final_task_1d_buffer = campaign_info.election_date - timedelta(days=1)

            prompt = f"""
# TIMELINE CONTEXT:
Signup Date: {date.today()}
Voter Outreach Phase 1 Start Date = {phase_1_start_date}
Primary Date: {campaign_info.primary_date}
Voter Outreach Phase 2 Start Date = {phase_2_start_date}
General Election Date: {campaign_info.election_date}

# CAMPAIGN CONTEXT:
- Candidate: {campaign_info.candidate_name}
- Office: {campaign_info.office_and_jurisdiction}
- Has Primary: Yes

# GOAL:
- Generate a Campaign Plan containing Voter Outreach Tasks for both Phase 1 (Primary) and Phase 2 (General Election).

# GOAL COMPLETION GUIDELINES:
- NO Voter Outreach Task date can be before {phase_1_start_date}.
## PHASE 1 GUIDELINES:
- ALL 7 Phase 1 Voter Outreach Tasks MUST be between {phase_1_start_date} and before {campaign_info.primary_date}.
- There must be AT LEAST 1 Voter Outreach Task every 7 days (MAXIMUM 7 days between Voter Outreach Tasks).
- The FINAL Voter Outreach Task in Phase 1 MUST be between {phase_1_final_task_1d_buffer} and before {campaign_info.primary_date}.
- List Voter Outreach Tasks chronologically from {phase_1_start_date} through {campaign_info.primary_date}.

## PHASE 2 GUIDELINES:
- ALL 7 Phase 2 Voter Outreach Tasks MUST be between {phase_2_start_date} and before {campaign_info.election_date}.
- There must be AT LEAST 1 Voter Outreach Task every 7 days (MAXIMUM 7 days between Voter Outreach Tasks).
- The FINAL Voter Outreach Task in Phase 2 MUST be between {phase_2_final_task_1d_buffer} and before {campaign_info.election_date}.
- List Voter Outreach Tasks chronologically from {phase_2_start_date} through {campaign_info.election_date}.

# VOTER OUTREACH TASKS: (Format: outreachType: outreachDescription)
## PHASE 1 TASKS:
- P2P Text: Early Text – Candidate introduction + race message
- Robocall: Early Robocall – Candidate introduction + race message
- P2P Text: Mid campaign Text – Persuasion message
- Robocall: Mid campaign Robocall – Persuasion message + polling info
- P2P Text: Late campaign Text – Early vote push
- Robocall: Final Robocall – GOTV call (morning)
- P2P Text: Final Text – Primary Day reminder + poll finder
### IMPORTANT: THERE MUST BE ALL 7 PHASE 1 VOTER OUTREACH TASKS BETWEEN {phase_1_start_date} AND BEFORE {campaign_info.primary_date}.

## PHASE 2 TASKS:
- P2P Text: Early Text – Candidate reintroduction + race message
- Robocall: Early Robocall – Candidate reintroduction + race message
- P2P Text: Mid campaign Text – Persuasion message
- Robocall: Mid campaign Robocall – Persuasion message + polling info
- P2P Text: Late campaign Text – Early vote push
- Robocall: Final Robocall – GOTV call (morning)
- P2P Text: Final Text – Election Day reminder + poll finder
### IMPORTANT: THERE MUST BE ALL 7 PHASE 2 VOTER OUTREACH TASKS BETWEEN {phase_2_start_date} AND BEFORE {campaign_info.election_date}.
---
# OUTPUT EXAMPLE FORMAT:
- yyyy-mm-dd – outreachType: outreachDescription
- yyyy-mm-dd – outreachType: outreachDescription
...
- {campaign_info.primary_date} – Primary Election Day
- yyyy-mm-dd – outreachType: outreachDescription
- yyyy-mm-dd – outreachType: outreachDescription
...
- {campaign_info.election_date} – General Election Day
"""
        else:            
            # Calculate start date:
            start_7w_date = campaign_info.election_date - timedelta(weeks=7)
            start_date_3d_buffer = date.today() + timedelta(days=3)
            start_date = start_date_3d_buffer if start_7w_date < start_date_3d_buffer else start_7w_date
            final_task_1d_buffer = campaign_info.election_date - timedelta(days=1)
            
            prompt = f"""
# TIMELINE CONTEXT:
Signup Date: {date.today()}
Voter Outreach Start Date = {start_date}
General Election Date: {campaign_info.election_date}

# CAMPAIGN CONTEXT:
- Candidate: {campaign_info.candidate_name}
- Office: {campaign_info.office_and_jurisdiction}
- Has Primary: No

# GOAL:
- Generate a Campaign Plan containing Voter Outreach Tasks.

# GOAL COMPLETION GUIDELINES:
- NO Voter Outreach Task date can be before {start_date}.
- ALL 7 Voter Outreach Tasks MUST be between {start_date} and before {campaign_info.election_date}.
- There must be AT LEAST 1 Voter Outreach Task every 7 days (MAXIMUM 7 days between Voter Outreach Tasks).
- The FINAL Voter Outreach Task MUST be between {final_task_1d_buffer} and before {campaign_info.election_date}.
- List Voter Outreach Tasks chronologically from {campaign_info.election_date - start_date} through {campaign_info.election_date}.

# VOTER OUTREACH TASKS: (Format: outreachType: outreachDescription)
- P2P Text: Early Text – Candidate intro
- Robocall: Early Robocall – Candidate intro + race message
- P2P Text: Mid campaign Text – Persuasion message
- Robocall: Mid campaign Robocall – Persuasion message + polling info
- P2P Text: Late campaign Text – Early vote push
- Robocall: Final Robocall – GOTV call (morning)
- P2P Text: Final Text – Election Day reminder + poll finder
## IMPORTANT: THERE MUST BE ALL 7 VOTER OUTREACH TASKS BETWEEN {start_date} AND BEFORE {campaign_info.election_date}.
---
# OUTPUT EXAMPLE FORMAT:
- yyyy-mm-dd – outreachType: outreachDescription
- yyyy-mm-dd – outreachType: outreachDescription
...
- {campaign_info.election_date} – General Election Day
"""
        try:
            response = self.llm_client.generate_content(
                prompt=prompt,
                system_instruction="You are a campaign strategist. Generate the voter contact plan in the exact format shown. Do not add thinking or reasoning.",
                temperature=0.1
            )
            
            voter_contact_plan = response

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

    print("\n" + "="*60 + "\n")

    print("=== CAMPAIGN WITH PRIMARY TEST CASES ===")

    print("\n" + "="*60 + "\n")

    print("=== TEST CASE 1: PRIMARY 16 + GENERAL ELECTION 24 WEEKS FROM SIGNUP DATE ===")
    primary_date_weeks = date.today() + timedelta(weeks=16)
    election_date_weeks = date.today() + timedelta(weeks=24)
    campaign_info_with_primary = CampaignInfo(
        candidate_name="John Doe",
        office_and_jurisdiction="Mayor, Springfield, MA",
        election_date=election_date_weeks,
        primary_date=primary_date_weeks,
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
    print(f"Signup Date: {date.today()}")
    print(f"Primary Date: {cleaned_campaign_info_with_primary.primary_date}")
    print(f"Election Date: {cleaned_campaign_info_with_primary.election_date}")
    print(result_with_primary)

    print("\n" + "="*60 + "\n")

    print("=== TEST CASE 2: PRIMARY 12 WEEKS + GENERAL ELECTION 16 FROM SIGNUP DATE ===")
    primary_date_weeks = date.today() + timedelta(weeks=12)
    election_date_weeks = date.today() + timedelta(weeks=16)
    campaign_info_with_primary = CampaignInfo(
        candidate_name="John Doe",
        office_and_jurisdiction="Mayor, Springfield, MA",
        election_date=election_date_weeks,
        primary_date=primary_date_weeks,
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
    print(f"Signup Date: {date.today()}")
    print(f"Primary Date: {cleaned_campaign_info_with_primary.primary_date}")
    print(f"Election Date: {cleaned_campaign_info_with_primary.election_date}")
    print(result_with_primary)

    print("\n" + "="*60 + "\n")

    print("=== TEST CASE 3: PRIMARY 6 WEEKS + GENERAL ELECTION 12 FROM SIGNUP DATE ===")
    primary_date_weeks = date.today() + timedelta(weeks=6)
    election_date_weeks = date.today() + timedelta(weeks=12)
    campaign_info_with_primary = CampaignInfo(
        candidate_name="John Doe",
        office_and_jurisdiction="Mayor, Springfield, MA",
        election_date=election_date_weeks,
        primary_date=primary_date_weeks,
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
    print(f"Signup Date: {date.today()}")
    print(f"Primary Date: {cleaned_campaign_info_with_primary.primary_date}")
    print(f"Election Date: {cleaned_campaign_info_with_primary.election_date}")
    print(result_with_primary)

    print("\n" + "="*60 + "\n")

    print("=== TEST CASE 4: PRIMARY 4 WEEKS + GENERAL ELECTION 8 FROM SIGNUP DATE ===")
    primary_date_weeks = date.today() + timedelta(weeks=4)
    election_date_weeks = date.today() + timedelta(weeks=8)
    campaign_info_with_primary = CampaignInfo(
        candidate_name="John Doe",
        office_and_jurisdiction="Mayor, Springfield, MA",
        election_date=election_date_weeks,
        primary_date=primary_date_weeks,
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
    print(f"Signup Date: {date.today()}")
    print(f"Primary Date: {cleaned_campaign_info_with_primary.primary_date}")
    print(f"Election Date: {cleaned_campaign_info_with_primary.election_date}")
    print(result_with_primary)

    print("\n" + "="*60 + "\n")

    print("=== TEST CASE 5: PRIMARY 2 WEEKS + GENERAL ELECTION 6 FROM SIGNUP DATE ===")
    primary_date_weeks = date.today() + timedelta(weeks=2)
    election_date_weeks = date.today() + timedelta(weeks=6)
    campaign_info_with_primary = CampaignInfo(
        candidate_name="John Doe",
        office_and_jurisdiction="Mayor, Springfield, MA",
        election_date=election_date_weeks,
        primary_date=primary_date_weeks,
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
    print(f"Signup Date: {date.today()}")
    print(f"Primary Date: {cleaned_campaign_info_with_primary.primary_date}")
    print(f"Election Date: {cleaned_campaign_info_with_primary.election_date}")
    print(result_with_primary)

    print("\n" + "="*60 + "\n")

    print("=== TEST CASE 6: PRIMARY 1 WEEKS + GENERAL ELECTION 4 FROM SIGNUP DATE ===")
    primary_date_weeks = date.today() + timedelta(weeks=1)
    election_date_weeks = date.today() + timedelta(weeks=4)
    campaign_info_with_primary = CampaignInfo(
        candidate_name="John Doe",
        office_and_jurisdiction="Mayor, Springfield, MA",
        election_date=election_date_weeks,
        primary_date=primary_date_weeks,
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
    print(f"Signup Date: {date.today()}")
    print(f"Primary Date: {cleaned_campaign_info_with_primary.primary_date}")
    print(f"Election Date: {cleaned_campaign_info_with_primary.election_date}")
    print(result_with_primary)

    print("\n" + "="*60 + "\n")

    print("=== CAMPAIGN WITHOUT PRIMARY TEST CASES ===")

    print("\n" + "="*60 + "\n")
    
    print("=== TEST CASE 1: GENERAL ELECTION 16 WEEKS FROM SIGNUP DATE ===")
    election_date_16_weeks = date.today() + timedelta(weeks=16)
    campaign_info_16_weeks = CampaignInfo(
        candidate_name="Michael Chen",
        office_and_jurisdiction="School Board, At-Large",
        election_date=election_date_16_weeks,
        primary_date=None,
        race_type="Nonpartisan",
        seats_available=2,
        number_of_opponents=4,
        win_number=18000,
        total_likely_voters=90000,
        available_cell_phones=18000,
        available_landlines=2000,
        incumbent_status=IncumbentStatus.NOT_APPLICABLE,
        additional_race_context="Focus on education reform and student achievement"
    )

    cleaned_campaign_info_16_weeks = campaign_utils.clean_campaign_info(campaign_info_16_weeks)
    general_campaign_plan = campaign_utils.optimize_contact_strategy(date.today(), cleaned_campaign_info_16_weeks.election_date)
    result_16_weeks = asyncio.run(generator.generate_section(cleaned_campaign_info_16_weeks, general_contact_strategy=general_campaign_plan))
    print(f"Signup Date: {date.today()}")
    print(f"Election Date: {election_date_16_weeks}")
    print(result_16_weeks)

    print("\n" + "="*60 + "\n")
    
    print("=== TEST CASE 2: GENERAL ELECTION 12 WEEKS FROM SIGNUP DATE ===")
    election_date_12_weeks = date.today() + timedelta(weeks=12)
    campaign_info_12_weeks = CampaignInfo(
        candidate_name="Sarah Williams",
        office_and_jurisdiction="County Commissioner, District 2",
        election_date=election_date_12_weeks,
        primary_date=None,
        race_type="Nonpartisan",
        seats_available=1,
        number_of_opponents=2,
        win_number=12000,
        total_likely_voters=60000,
        available_cell_phones=12000,
        available_landlines=1500,
        incumbent_status=IncumbentStatus.NOT_APPLICABLE,
        additional_race_context="Focus on infrastructure and public safety"
    )

    cleaned_campaign_info_12_weeks = campaign_utils.clean_campaign_info(campaign_info_12_weeks)
    general_campaign_plan = campaign_utils.optimize_contact_strategy(date.today(), cleaned_campaign_info_12_weeks.election_date)
    result_12_weeks = asyncio.run(generator.generate_section(cleaned_campaign_info_12_weeks, general_contact_strategy=general_campaign_plan))
    print(f"Signup Date: {date.today()}")
    print(f"Election Date: {election_date_12_weeks}")
    print(result_12_weeks)

    print("\n" + "="*60 + "\n")

    print("=== TEST CASE 3: GENERAL ELECTION 8 WEEKS FROM SIGNUP DATE ===")
    election_date_8_weeks = date.today() + timedelta(weeks=8)
    outreach_start_date = election_date_8_weeks - timedelta(weeks=8)
    campaign_info_no_primary = CampaignInfo(
        candidate_name="Jane Smith",
        office_and_jurisdiction="City Council, District 3, Boston, MA",
        election_date=election_date_8_weeks,
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
    result_8_weeks = asyncio.run(generator.generate_section(cleaned_campaign_info_no_primary, general_contact_strategy=general_campaign_plan))
    print(f"Signup Date: {date.today()}")
    print(f"Election Date: {election_date_8_weeks}")
    print(result_8_weeks)

    print("\n" + "="*60 + "\n")

    print("=== TEST CASE 4: GENERAL ELECTION 4 WEEKS FROM SIGNUP DATE ===")
    election_date_4_weeks = date.today() + timedelta(weeks=4)
    campaign_info_4_weeks = CampaignInfo(
        candidate_name="Bob Johnson",
        office_and_jurisdiction="State Representative, District 5",
        election_date=election_date_4_weeks,
        primary_date=None,
        race_type="Partisan",
        seats_available=1,
        number_of_opponents=1,
        win_number=10000,
        total_likely_voters=40000,
        available_cell_phones=8000,
        available_landlines=1200,
        incumbent_status=IncumbentStatus.NOT_APPLICABLE,
        additional_race_context="Focus on economic development and job creation"
    )

    cleaned_campaign_info_4_weeks = campaign_utils.clean_campaign_info(campaign_info_4_weeks)
    general_campaign_plan = campaign_utils.optimize_contact_strategy(date.today(), cleaned_campaign_info_4_weeks.election_date)
    result_4_weeks = asyncio.run(generator.generate_section(cleaned_campaign_info_4_weeks, general_contact_strategy=general_campaign_plan))
    print(f"Signup Date: {date.today()}")
    print(f"Election Date: {election_date_4_weeks}")
    print(result_4_weeks)

    print("\n" + "="*60 + "\n")

    print("=== TEST CASE 5: GENERAL ELECTION 2 WEEKS FROM SIGNUP DATE ===")
    election_date_2_weeks = date.today() + timedelta(weeks=2)
    campaign_info_2_weeks = CampaignInfo(
        candidate_name="Bob Johnson",
        office_and_jurisdiction="State Representative, District 5",
        election_date=election_date_2_weeks,
        primary_date=None,
        race_type="Partisan",
        seats_available=1,
        number_of_opponents=1,
        win_number=10000,
        total_likely_voters=40000,
        available_cell_phones=8000,
        available_landlines=1200,
        incumbent_status=IncumbentStatus.NOT_APPLICABLE,
        additional_race_context="Focus on economic development and job creation"
    )

    cleaned_campaign_info_2_weeks = campaign_utils.clean_campaign_info(campaign_info_2_weeks)
    general_campaign_plan = campaign_utils.optimize_contact_strategy(date.today(), cleaned_campaign_info_2_weeks.election_date)
    result_2_weeks = asyncio.run(generator.generate_section(cleaned_campaign_info_2_weeks, general_contact_strategy=general_campaign_plan))
    print(f"Signup Date: {date.today()}")
    print(f"Election Date: {election_date_2_weeks}")
    print(result_2_weeks)

    print("\n" + "="*60 + "\n")

    print("=== TEST CASE 6: GENERAL ELECTION 1 WEEK FROM SIGNUP DATE ===")
    election_date_1_weeks = date.today() + timedelta(weeks=1)
    campaign_info_1_weeks = CampaignInfo(
        candidate_name="Bob Johnson",
        office_and_jurisdiction="State Representative, District 5",
        election_date=election_date_1_weeks,
        primary_date=None,
        race_type="Partisan",
        seats_available=1,
        number_of_opponents=1,
        win_number=10000,
        total_likely_voters=40000,
        available_cell_phones=8000,
        available_landlines=1200,
        incumbent_status=IncumbentStatus.NOT_APPLICABLE,
        additional_race_context="Focus on economic development and job creation"
    )

    cleaned_campaign_info_1_weeks = campaign_utils.clean_campaign_info(campaign_info_1_weeks)
    general_campaign_plan = campaign_utils.optimize_contact_strategy(date.today(), cleaned_campaign_info_1_weeks.election_date)
    result_1_weeks = asyncio.run(generator.generate_section(cleaned_campaign_info_1_weeks, general_contact_strategy=general_campaign_plan))
    print(f"Signup Date: {date.today()}")
    print(f"Election Date: {election_date_1_weeks}")
    print(result_1_weeks)

    print("\n" + "="*60 + "\n")