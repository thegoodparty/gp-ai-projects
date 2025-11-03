import asyncio
from datetime import date, timedelta
from ai_generated_campaign_plan.schema.models import CleanedCampaignInfo, ContactOptimization, IncumbentStatus
from shared.logger import get_logger
from shared.llm_gemini import GeminiClient



class VoterContactPlanGenerator:

    def __init__(self):
        self.llm_client = GeminiClient()
        self.logger = get_logger(__name__)

    def _calculate_phase_1_dates(self, campaign_info: CleanedCampaignInfo) -> tuple[date, date]:
        """
        Calculate Phase 1 (primary) start and end dates using 49-day rule with 3-day buffer.

        Args:
            campaign_info: Cleaned campaign information with primary_date

        Returns:
            Tuple of (start_date, end_date) for Phase 1
        """
        primary_date = campaign_info.primary_date
        today = date.today()

        days_until_primary = (primary_date - today).days

        if days_until_primary > 52:
            start_date = primary_date - timedelta(days=49)
        else:
            start_date = today + timedelta(days=3)

        if start_date >= primary_date:
            start_date = today
            self.logger.warning(f"Start date would be on or after primary date. Using today as start date.")

        end_date = primary_date

        window_days = (end_date - start_date).days
        if window_days < 1:
            self.logger.error(f"Phase 1 window is {window_days} days. Minimum 1 day required.")
            start_date = max(today, end_date - timedelta(days=1))
            window_days = (end_date - start_date).days
            self.logger.warning(f"Adjusted Phase 1 start to {start_date} ({window_days}-day minimum window)")

        self.logger.info(f"Phase 1 dates: {start_date} to {end_date} ({(end_date - start_date).days} days)")
        return (start_date, end_date)

    def _calculate_phase_2_dates(self, campaign_info: CleanedCampaignInfo) -> tuple[date, date]:
        """
        Calculate Phase 2 (general after primary) start and end dates using 49-day rule, no buffer.
        Phase 2 must start at least 1 day after primary.

        Args:
            campaign_info: Cleaned campaign information with primary_date and election_date

        Returns:
            Tuple of (start_date, end_date) for Phase 2
        """
        primary_date = campaign_info.primary_date
        election_date = campaign_info.election_date

        start_date = max(
            primary_date + timedelta(days=1),
            election_date - timedelta(days=49)
        )

        if start_date >= election_date:
            start_date = primary_date + timedelta(days=1)
            self.logger.warning(f"Phase 2 start would be on or after election. Using day after primary.")

        end_date = election_date

        window_days = (end_date - start_date).days
        if window_days < 1:
            self.logger.error(f"Phase 2 window is {window_days} days. Minimum 1 day required.")
            start_date = max(primary_date + timedelta(days=1), end_date - timedelta(days=1))
            window_days = (end_date - start_date).days
            self.logger.warning(f"Adjusted Phase 2 start to {start_date} ({window_days}-day minimum window)")

        self.logger.info(f"Phase 2 dates: {start_date} to {end_date} ({(end_date - start_date).days} days)")
        return (start_date, end_date)

    def _calculate_general_only_dates(self, campaign_info: CleanedCampaignInfo) -> tuple[date, date]:
        """
        Calculate general-only campaign start and end dates using 49-day rule with 3-day buffer.

        Args:
            campaign_info: Cleaned campaign information with election_date

        Returns:
            Tuple of (start_date, end_date) for general election
        """
        election_date = campaign_info.election_date
        today = date.today()

        days_until_election = (election_date - today).days

        if days_until_election > 52:
            start_date = election_date - timedelta(days=49)
        else:
            start_date = today + timedelta(days=3)

        if start_date >= election_date:
            start_date = today
            self.logger.warning(f"Start date would be on or after election date. Using today as start date.")

        end_date = election_date

        window_days = (end_date - start_date).days
        if window_days < 1:
            self.logger.error(f"General election window is {window_days} days. Minimum 1 day required.")
            start_date = max(today, end_date - timedelta(days=1))
            window_days = (end_date - start_date).days
            self.logger.warning(f"Adjusted general start to {start_date} ({window_days}-day minimum window)")

        self.logger.info(f"General only dates: {start_date} to {end_date} ({(end_date - start_date).days} days)")
        return (start_date, end_date)


    async def generate_section(self, campaign_info: CleanedCampaignInfo) -> str:
        self.logger.info(f"Generating voter contact plan for {campaign_info.candidate_name}")

        has_primary = campaign_info.has_primary
        today = date.today()

        if campaign_info.election_date < today:
            error_msg = f"Election date {campaign_info.election_date} is in the past"
            self.logger.error(error_msg)
            return f"## 6. VOTER CONTACT PLAN\n\nError: {error_msg}. Cannot generate plan for past elections."

        if has_primary:
            if campaign_info.primary_date is None:
                error_msg = "Primary date is None but has_primary is True"
                self.logger.error(error_msg)
                return f"## 6. VOTER CONTACT PLAN\n\nError: {error_msg}. Invalid campaign data."

            if campaign_info.primary_date < today:
                error_msg = f"Primary date {campaign_info.primary_date} is in the past"
                self.logger.error(error_msg)
                return f"## 6. VOTER CONTACT PLAN\n\nError: {error_msg}. Cannot generate plan for past primaries."

            if campaign_info.primary_date >= campaign_info.election_date:
                error_msg = "Primary date must be before election date"
                self.logger.error(error_msg)
                return f"## 6. VOTER CONTACT PLAN\n\nError: {error_msg}. Invalid campaign dates."

        if has_primary:
            phase_1_start, phase_1_end = self._calculate_phase_1_dates(campaign_info)
            phase_2_start, phase_2_end = self._calculate_phase_2_dates(campaign_info)

            phase_1_final_task_buffer = phase_1_end - timedelta(days=1)
            phase_2_final_task_buffer = phase_2_end - timedelta(days=1)

            prompt = f"""
You are a campaign strategist. Generate a voter contact plan with EXACTLY 7 tasks for Phase 1 and EXACTLY 7 tasks for Phase 2.

# CRITICAL DATE FORMAT REQUIREMENT:
ALL dates MUST use this EXACT format: [Full Month Name] [Day Number]
CORRECT EXAMPLES:
- January 15
- February 3
- March 28
- November 5
INCORRECT EXAMPLES (DO NOT USE):
- Jan 15 (abbreviated month)
- 01/15/2025 (numeric format)
- 15 January (reversed order)
- January 15th (ordinal suffix)

# TIMELINE CONTEXT:
Signup Date: {today}
Voter Outreach Phase 1 Start Date: {phase_1_start}
Primary Date: {phase_1_end}
Voter Outreach Phase 2 Start Date: {phase_2_start}
General Election Date: {phase_2_end}

CAMPAIGN CONTEXT:
- Candidate: {campaign_info.candidate_name}
- Office: {campaign_info.office_and_jurisdiction}
- Has Primary: Yes

# GOAL COMPLETION GUIDELINES:
- NO Voter Outreach Task date can be before {phase_1_start}.

## PHASE 1 GUIDELINES:
- ALL 7 Phase 1 Voter Outreach Tasks MUST be between {phase_1_start} and {phase_1_end}.
- There must be AT LEAST 1 Voter Outreach Task every 7 days (MAXIMUM 7 days between Voter Outreach Tasks).
- The FINAL Voter Outreach Task in Phase 1 MUST be on {phase_1_final_task_buffer} OR {phase_1_end}.
- List Voter Outreach Tasks chronologically from {phase_1_start} through {phase_1_end}.

# PHASE 1 TASKS (Format: outreachType: outreachDescription):
1. P2P Text: Early Text – Candidate introduction + race message
2. Robocall: Early Robocall – Candidate introduction + race message
3. P2P Text: Mid campaign Text – Persuasion message
4. Robocall: Mid campaign Robocall – Persuasion message + polling info
5. P2P Text: Late campaign Text – Early vote push
6. Robocall: Final Robocall – GOTV call (morning)
7. P2P Text: Final Text – Primary Day reminder + poll finder

IMPORTANT: THERE MUST BE ALL 7 PHASE 1 VOTER OUTREACH TASKS BETWEEN {phase_1_start} AND {phase_1_end}.

## PHASE 2 GUIDELINES:
- ALL 7 Phase 2 Voter Outreach Tasks MUST be between {phase_2_start} and {phase_2_end}.
- There must be AT LEAST 1 Voter Outreach Task every 7 days (MAXIMUM 7 days between Voter Outreach Tasks).
- The FINAL Voter Outreach Task in Phase 2 MUST be on {phase_2_final_task_buffer} OR {phase_2_end}.
- List Voter Outreach Tasks chronologically from {phase_2_start} through {phase_2_end}.

# PHASE 2 TASKS (Format: outreachType: outreachDescription):
1. P2P Text: Early Text – Candidate reintroduction + race message
2. Robocall: Early Robocall – Candidate reintroduction + race message
3. P2P Text: Mid campaign Text – Persuasion message
4. Robocall: Mid campaign Robocall – Persuasion message + polling info
5. P2P Text: Late campaign Text – Early vote push
6. Robocall: Final Robocall – GOTV call (morning)
7. P2P Text: Final Text – Election Day reminder + poll finder

IMPORTANT: THERE MUST BE ALL 7 PHASE 2 VOTER OUTREACH TASKS BETWEEN {phase_2_start} AND {phase_2_end}.

# OUTPUT FORMAT REQUIREMENTS:
Generate the voter contact plan using this EXACT format with proper date formatting:

CORRECT FORMAT EXAMPLES:
- January 15 – P2P Text: Early Text – Candidate introduction + race message
- January 22 – Robocall: Early Robocall – Candidate introduction + race message
- February 5 – P2P Text: Mid campaign Text – Persuasion message
...
- {phase_1_end.strftime('%B')} {phase_1_end.day} – Primary Election Day
- {phase_2_start.strftime('%B')} {phase_2_start.day} – P2P Text: Early Text – Candidate reintroduction + race message
...
- {phase_2_end.strftime('%B')} {phase_2_end.day} – General Election Day

CRITICAL: Every date MUST be in format "Month DD" (e.g., "March 7" NOT "Mar 7" or "03/07")

Do NOT add thinking or reasoning. Generate ONLY the formatted task list with proper date formatting.
"""
        else:
            start_date, end_date = self._calculate_general_only_dates(campaign_info)
            final_task_buffer = end_date - timedelta(days=1)

            prompt = f"""
You are a campaign strategist. Generate a voter contact plan with EXACTLY 7 tasks.

# CRITICAL DATE FORMAT REQUIREMENT:
ALL dates MUST use this EXACT format: [Full Month Name] [Day Number]
CORRECT EXAMPLES:
- January 15
- February 3
- March 28
- November 5
INCORRECT EXAMPLES (DO NOT USE):
- Jan 15 (abbreviated month)
- 01/15/2025 (numeric format)
- 15 January (reversed order)
- January 15th (ordinal suffix)

# TIMELINE CONTEXT:
Signup Date: {today}
Voter Outreach Start Date: {start_date}
General Election Date: {end_date}

CAMPAIGN CONTEXT:
- Candidate: {campaign_info.candidate_name}
- Office: {campaign_info.office_and_jurisdiction}
- Has Primary: No

# GOAL COMPLETION GUIDELINES:
- NO Voter Outreach Task date can be before {start_date}.
- ALL 7 Voter Outreach Tasks MUST be between {start_date} and {end_date}.
- There must be AT LEAST 1 Voter Outreach Task every 7 days (MAXIMUM 7 days between Voter Outreach Tasks).
- The FINAL Voter Outreach Task MUST be on {final_task_buffer} OR {end_date}.
- List Voter Outreach Tasks chronologically from {start_date} through {end_date}.

# VOTER OUTREACH TASKS (Format: outreachType: outreachDescription):
1. P2P Text: Early Text – Candidate intro + race message
2. Robocall: Early Robocall – Candidate intro + race message
3. P2P Text: Mid campaign Text – Persuasion message
4. Robocall: Mid campaign Robocall – Persuasion message + polling info
5. P2P Text: Late campaign Text – Early vote push
6. Robocall: Final Robocall – GOTV call (morning)
7. P2P Text: Final Text – Election Day reminder + poll finder

IMPORTANT: THERE MUST BE ALL 7 VOTER OUTREACH TASKS BETWEEN {start_date} AND {end_date}.

# OUTPUT FORMAT REQUIREMENTS:
Generate the voter contact plan using this EXACT format with proper date formatting:

CORRECT FORMAT EXAMPLES:
- January 15 – P2P Text: Early Text – Candidate intro + race message
- January 22 – Robocall: Early Robocall – Candidate intro + race message
- February 5 – P2P Text: Mid campaign Text – Persuasion message
...
- {end_date.strftime('%B')} {end_date.day} – General Election Day

CRITICAL: Every date MUST be in format "Month DD" (e.g., "March 7" NOT "Mar 7" or "03/07")

Do NOT add thinking or reasoning. Generate ONLY the formatted task list with proper date formatting.
"""

        try:
            response = self.llm_client.generate_content(
                prompt=prompt,
                system_instruction="You are a campaign strategist. Generate the voter contact plan in the exact format shown with exactly 7 tasks per phase. CRITICAL: All dates must use format 'Month DD' (e.g., 'January 15' NOT 'Jan 15'). Do not add thinking or reasoning.",
                temperature=0.1
            )

            voter_contact_plan = response

            self.logger.info(f"Generated voter contact plan successfully")
            return "\n".join([
                "## 6. VOTER CONTACT PLAN",
                "### Core Tactics (Chronological)",
                voter_contact_plan,
            ])
        except Exception as e:
            self.logger.error(f"Error generating voter contact plan: {str(e)}")
            return "## 6. VOTER CONTACT PLAN\n\n[Error generating voter contact plan]"
            


if __name__ == "__main__":
    from ai_generated_campaign_plan.schema.models import CampaignInfo, RaceType
    from ai_generated_campaign_plan.utils.utils import CampaignUtils

    campaign_utils = CampaignUtils()
    generator = VoterContactPlanGenerator()

    today = date.today()

    print("="*80)
    print("COMPREHENSIVE VOTER CONTACT PLAN TEST SUITE")
    print("="*80)
    print(f"\nToday's Date: {today}\n")

    test_cases = [
        {
            "name": "TEST 1: Long Timeline with Primary (100+ days to both phases)",
            "description": "Primary in 75 days, General 60 days after primary",
            "campaign_info": CampaignInfo(
                candidate_name="John Doe",
                office_and_jurisdiction="Mayor, Springfield, MA",
                election_date=today + timedelta(days=135),
                primary_date=today + timedelta(days=75),
                race_type=RaceType.NONPARTISAN,
                seats_available=1,
                number_of_opponents=2,
                win_number=15000,
                total_likely_voters=100000,
                available_cell_phones=10000,
                available_landlines=1000,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on education funding and infrastructure improvements"
            ),
            "expected_behavior": "Phase 1: Start 49 days before primary. Phase 2: Start 49 days before general."
        },
        {
            "name": "TEST 2: Long Timeline without Primary (100+ days to election)",
            "description": "General election in 100 days",
            "campaign_info": CampaignInfo(
                candidate_name="Jane Smith",
                office_and_jurisdiction="City Council, District 3, Boston, MA",
                election_date=today + timedelta(days=100),
                primary_date=None,
                race_type=RaceType.NONPARTISAN,
                seats_available=1,
                number_of_opponents=3,
                win_number=8000,
                total_likely_voters=50000,
                available_cell_phones=5000,
                available_landlines=500,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on neighborhood safety and small business support"
            ),
            "expected_behavior": "Start 49 days before election (max window)."
        },
        {
            "name": "TEST 3: Medium Timeline with Primary (40-50 days)",
            "description": "Primary in 45 days, General 50 days after primary",
            "campaign_info": CampaignInfo(
                candidate_name="Robert Chen",
                office_and_jurisdiction="State Representative, District 12, MA",
                election_date=today + timedelta(days=95),
                primary_date=today + timedelta(days=45),
                race_type=RaceType.PARTISAN,
                seats_available=1,
                number_of_opponents=4,
                win_number=8500,
                total_likely_voters=35000,
                available_cell_phones=6000,
                available_landlines=800,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on healthcare access"
            ),
            "expected_behavior": "Phase 1: Start 3 days from today (45 < 52). Phase 2: Start 1 day after primary (50 > 49)."
        },
        {
            "name": "TEST 4: Short Timeline with Primary (20-30 days to primary)",
            "description": "Primary in 25 days, General 55 days after primary",
            "campaign_info": CampaignInfo(
                candidate_name="Maria Garcia",
                office_and_jurisdiction="Mayor, Somerville, MA",
                election_date=today + timedelta(days=80),
                primary_date=today + timedelta(days=25),
                race_type=RaceType.PARTISAN,
                seats_available=1,
                number_of_opponents=3,
                win_number=12000,
                total_likely_voters=45000,
                available_cell_phones=8000,
                available_landlines=1500,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on affordable housing"
            ),
            "expected_behavior": "Phase 1: Start 3 days from today (22-day window). Phase 2: Start 49 days before general."
        },
        {
            "name": "TEST 5: Very Short Primary Phase (<20 days)",
            "description": "Primary in 15 days, General 70 days after primary",
            "campaign_info": CampaignInfo(
                candidate_name="David Park",
                office_and_jurisdiction="School Board, At-Large, Worcester, MA",
                election_date=today + timedelta(days=85),
                primary_date=today + timedelta(days=15),
                race_type=RaceType.NONPARTISAN,
                seats_available=2,
                number_of_opponents=5,
                win_number=6000,
                total_likely_voters=25000,
                available_cell_phones=4000,
                available_landlines=500,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on curriculum development"
            ),
            "expected_behavior": "Phase 1: Start 3 days from today (12-day window, tasks compressed). Phase 2: Start 49 days before general."
        },
        {
            "name": "TEST 6: Short General Phase after Primary",
            "description": "Primary in 60 days, General only 25 days after primary",
            "campaign_info": CampaignInfo(
                candidate_name="Lisa Wang",
                office_and_jurisdiction="City Clerk, Lowell, MA",
                election_date=today + timedelta(days=85),
                primary_date=today + timedelta(days=60),
                race_type=RaceType.NONPARTISAN,
                seats_available=1,
                number_of_opponents=2,
                win_number=7500,
                total_likely_voters=30000,
                available_cell_phones=5500,
                available_landlines=700,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on government transparency"
            ),
            "expected_behavior": "Phase 1: Start 49 days before primary. Phase 2: Start 1 day after primary (25 < 49, immediate start)."
        },
        {
            "name": "TEST 7: Medium Timeline without Primary (40 days)",
            "description": "General election in 40 days",
            "campaign_info": CampaignInfo(
                candidate_name="Sarah Johnson",
                office_and_jurisdiction="School Committee, Quincy, MA",
                election_date=today + timedelta(days=40),
                primary_date=None,
                race_type=RaceType.NONPARTISAN,
                seats_available=3,
                number_of_opponents=6,
                win_number=9000,
                total_likely_voters=38000,
                available_cell_phones=7000,
                available_landlines=1000,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on special education programs"
            ),
            "expected_behavior": "Start 3 days from today (37-day window)."
        },
        {
            "name": "TEST 8: Short Timeline without Primary (25 days)",
            "description": "General election in 25 days",
            "campaign_info": CampaignInfo(
                candidate_name="Alex Johnson",
                office_and_jurisdiction="Selectboard, Amherst, MA",
                election_date=today + timedelta(days=25),
                primary_date=None,
                race_type=RaceType.NONPARTISAN,
                seats_available=1,
                number_of_opponents=2,
                win_number=5000,
                total_likely_voters=20000,
                available_cell_phones=3500,
                available_landlines=400,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on environmental sustainability"
            ),
            "expected_behavior": "Start 3 days from today (22-day window, tight spacing)."
        },
        {
            "name": "TEST 9: Very Short Timeline without Primary (12 days) - EDGE CASE",
            "description": "General election in 12 days",
            "campaign_info": CampaignInfo(
                candidate_name="Michael Brown",
                office_and_jurisdiction="Town Council, Brookline, MA",
                election_date=today + timedelta(days=12),
                primary_date=None,
                race_type=RaceType.NONPARTISAN,
                seats_available=1,
                number_of_opponents=1,
                win_number=3500,
                total_likely_voters=15000,
                available_cell_phones=2500,
                available_landlines=300,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on local business support"
            ),
            "expected_behavior": "Start 3 days from today (9-day window, tasks may be same day)."
        },
        {
            "name": "TEST 10: Immediate Phase 2 Start - EDGE CASE",
            "description": "Primary in 20 days, General only 15 days after primary",
            "campaign_info": CampaignInfo(
                candidate_name="Jennifer Lee",
                office_and_jurisdiction="Alderperson, Ward 5, Cambridge, MA",
                election_date=today + timedelta(days=35),
                primary_date=today + timedelta(days=20),
                race_type=RaceType.PARTISAN,
                seats_available=1,
                number_of_opponents=3,
                win_number=4200,
                total_likely_voters=18000,
                available_cell_phones=3200,
                available_landlines=250,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on traffic congestion"
            ),
            "expected_behavior": "Phase 1: Start 3 days from today (17-day window). Phase 2: Start 1 day after primary (14-day window, compressed)."
        },
        {
            "name": "TEST 11: General 4 Weeks After Primary - EXTREME COMPRESSION",
            "description": "Primary in 60 days, General only 4 weeks (28 days) after primary",
            "campaign_info": CampaignInfo(
                candidate_name="Thomas Martinez",
                office_and_jurisdiction="School Committee, Revere, MA",
                election_date=today + timedelta(days=88),
                primary_date=today + timedelta(days=60),
                race_type=RaceType.NONPARTISAN,
                seats_available=2,
                number_of_opponents=5,
                win_number=5500,
                total_likely_voters=22000,
                available_cell_phones=4200,
                available_landlines=600,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on teacher retention"
            ),
            "expected_behavior": "Phase 1: Start 49 days before primary (11-day window). Phase 2: Start 1 day after primary (27-day window, <49 day rule triggers immediate start)."
        },
        {
            "name": "TEST 12: General in 1 Week - EXTREME EDGE CASE",
            "description": "General election in 7 days, no primary",
            "campaign_info": CampaignInfo(
                candidate_name="Patricia O'Brien",
                office_and_jurisdiction="Planning Board, Medford, MA",
                election_date=today + timedelta(days=7),
                primary_date=None,
                race_type=RaceType.NONPARTISAN,
                seats_available=1,
                number_of_opponents=2,
                win_number=2800,
                total_likely_voters=12000,
                available_cell_phones=2200,
                available_landlines=300,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on zoning reform"
            ),
            "expected_behavior": "Start 3 days from today (4-day window, all 7 tasks extremely compressed, likely multiple same-day tasks)."
        },
        {
            "name": "TEST 13: General in 4 Days - EXTREME EDGE CASE",
            "description": "General election in 4 days, no primary",
            "campaign_info": CampaignInfo(
                candidate_name="Kevin Nguyen",
                office_and_jurisdiction="Library Trustee, Arlington, MA",
                election_date=today + timedelta(days=4),
                primary_date=None,
                race_type=RaceType.NONPARTISAN,
                seats_available=1,
                number_of_opponents=1,
                win_number=1500,
                total_likely_voters=8000,
                available_cell_phones=1800,
                available_landlines=200,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on digital literacy programs"
            ),
            "expected_behavior": "Start 3 days from today (1-day window, all 7 tasks must be on same day or extremely compressed)."
        },
        {
            "name": "TEST 14: General in 2 Days - IMPOSSIBLE TIMELINE EDGE CASE",
            "description": "General election in 2 days, no primary",
            "campaign_info": CampaignInfo(
                candidate_name="Rachel Green",
                office_and_jurisdiction="Housing Authority, Malden, MA",
                election_date=today + timedelta(days=2),
                primary_date=None,
                race_type=RaceType.NONPARTISAN,
                seats_available=1,
                number_of_opponents=1,
                win_number=1200,
                total_likely_voters=6500,
                available_cell_phones=1500,
                available_landlines=150,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Focus on affordable housing"
            ),
            "expected_behavior": "Start date calculation (today + 3) would exceed election date. System uses today as start (2-day window). All 7 tasks must fit (requires same-day scheduling)."
        },
        {
            "name": "TEST 15: BUG FIX - Past Election Date",
            "description": "Election date is in the past (should return error)",
            "campaign_info": CampaignInfo(
                candidate_name="Test Candidate",
                office_and_jurisdiction="City Council, Test City, MA",
                election_date=today - timedelta(days=30),
                primary_date=None,
                race_type=RaceType.NONPARTISAN,
                seats_available=1,
                number_of_opponents=1,
                win_number=1000,
                total_likely_voters=5000,
                available_cell_phones=1000,
                available_landlines=100,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Test past election"
            ),
            "expected_behavior": "Should return error message: 'Election date is in the past. Cannot generate plan for past elections.'"
        },
        {
            "name": "TEST 16: BUG FIX - Past Primary Date",
            "description": "Primary date is in the past (should return error)",
            "campaign_info": CampaignInfo(
                candidate_name="Test Candidate 2",
                office_and_jurisdiction="School Board, Test City, MA",
                election_date=today + timedelta(days=60),
                primary_date=today - timedelta(days=10),
                race_type=RaceType.PARTISAN,
                seats_available=1,
                number_of_opponents=2,
                win_number=2000,
                total_likely_voters=8000,
                available_cell_phones=1500,
                available_landlines=200,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Test past primary"
            ),
            "expected_behavior": "Should return error message: 'Primary date is in the past. Cannot generate plan for past primaries.'"
        },
        {
            "name": "TEST 17: BUG FIX - Primary After Election",
            "description": "Primary date is after election date (should return error)",
            "campaign_info": CampaignInfo(
                candidate_name="Test Candidate 3",
                office_and_jurisdiction="Mayor, Test City, MA",
                election_date=today + timedelta(days=30),
                primary_date=today + timedelta(days=60),
                race_type=RaceType.PARTISAN,
                seats_available=1,
                number_of_opponents=3,
                win_number=5000,
                total_likely_voters=15000,
                available_cell_phones=3000,
                available_landlines=500,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Test invalid date order"
            ),
            "expected_behavior": "Should return error message: 'Primary date must be before election date. Invalid campaign dates.'"
        },
        {
            "name": "TEST 18: BUG FIX - Zero-Day Window (Election = Today + 3)",
            "description": "Election exactly 3 days from today (tests >= fix)",
            "campaign_info": CampaignInfo(
                candidate_name="Test Candidate 4",
                office_and_jurisdiction="Selectboard, Test City, MA",
                election_date=today + timedelta(days=3),
                primary_date=None,
                race_type=RaceType.NONPARTISAN,
                seats_available=1,
                number_of_opponents=1,
                win_number=800,
                total_likely_voters=3000,
                available_cell_phones=600,
                available_landlines=50,
                incumbent_status=IncumbentStatus.NOT_APPLICABLE,
                additional_race_context="Test zero-day window fix"
            ),
            "expected_behavior": "Should trigger >= safety check and create minimum 1-day window (today to today+3 becomes today to election)."
        },
    ]

    for i, test in enumerate(test_cases, 1):
        print("="*80)
        print(f"\n{test['name']}")
        print(f"Description: {test['description']}")
        print(f"Expected Behavior: {test['expected_behavior']}\n")
        print("-"*80)

        try:
            cleaned_info = campaign_utils.clean_campaign_info(test['campaign_info'])
            result = asyncio.run(generator.generate_section(cleaned_info))
            print(result)
        except Exception as e:
            print(f"ERROR: {str(e)}")
            import traceback
            traceback.print_exc()

        print("\n")

    print("="*80)
    print("TEST SUITE COMPLETE")
    print("="*80)