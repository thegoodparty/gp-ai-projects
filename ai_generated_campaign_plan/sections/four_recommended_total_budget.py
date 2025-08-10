from ai_generated_campaign_plan.schema.models import CleanedCampaignInfo
from shared.logger import get_logger

logger = get_logger(__name__)

def generate_recommended_total_budget(campaign_info: CleanedCampaignInfo) -> str:
    """
    Generate recommended total budget section based on campaign information.
    
    Args:
        campaign_info: Cleaned campaign information containing win_number
        
    Returns:
        str: Formatted budget recommendation text
    """
    logger.info(f"Generating budget recommendation for candidate: {campaign_info.candidate_name}")

    win_number = campaign_info.win_number
    logger.debug(f"Win number extracted: {win_number}")

    calculated_budget = (win_number * 3) * 0.30 / 0.60
    logger.debug(f"Budget calculation: ({win_number} * 3) * 0.30 / 0.60 = {calculated_budget}")

    budget_formatted = f"${calculated_budget:,.0f}"
    logger.debug(f"Budget formatted: {budget_formatted}")

    logger.info(f"Budget recommendation generated successfully: {budget_formatted}")

    return f"""## 4. RECOMMENDED TOTAL BUDGET

A budget of roughly {budget_formatted} will support your full voter contact program, combining volunteer-powered in-person outreach with targeted paid digital communication strategies. The entirety of this budget will be invested in get out the vote tactics, assuming we are leveraging a combination of: peer to peer messaging, robocalls, and digital/newspaper advertising."""

if __name__ == "__main__":
    from datetime import date

    from ai_generated_campaign_plan.schema.models import (
        CleanedCampaignInfo,
        IncumbentStatus,
        RaceType,
    )

    logger.info("Starting budget generation test")

    cleaned_campaign_info = CleanedCampaignInfo(
        candidate_name="John Doe",
        primary_date=None,
        election_date=date(2025, 11, 5),
        office_and_jurisdiction="School Board, At-Large, Chicopee, MA",
        incumbent_status=IncumbentStatus.NOT_APPLICABLE,
        race_type=RaceType.NONPARTISAN,
        seats_available=1,
        number_of_opponents=1,
        win_number=None,
        total_likely_voters=8429,
        available_cell_phones=4505,
        available_landlines=3780,
        additional_race_context="Focus on education funding and infrastructure improvements",
        city="Chicopee",
        state="MA",
        state_full="Massachusetts",
        election_date_formatted="2025-11-05",
        has_primary=False,
        primary_date_formatted=None
    )

    logger.debug("Test campaign info created")
    result = generate_recommended_total_budget(cleaned_campaign_info)
    logger.info("Test completed successfully")
    print(result)

    logger.info("\n--- Testing with zero win number ---")
    zero_win_campaign = CleanedCampaignInfo(
        candidate_name="Jane Smith",
        primary_date=None,
        election_date=date(2025, 11, 5),
        office_and_jurisdiction="City Council, District 1, Springfield, MA",
        incumbent_status=IncumbentStatus.NOT_APPLICABLE,
        race_type=RaceType.NONPARTISAN,
        seats_available=1,
        number_of_opponents=2,
        win_number=-1,
        total_likely_voters=5000,
        available_cell_phones=2500,
        available_landlines=2000,
        additional_race_context="Focus on local infrastructure",
        city="Springfield",
        state="MA",
        state_full="Massachusetts",
        election_date_formatted="2025-11-05",
        has_primary=False,
        primary_date_formatted=None
    )

    zero_result = generate_recommended_total_budget(zero_win_campaign)
    print("\n" + zero_result)
