
from ai_generated_campaign_plan.schema.models import IncumbentStatus
from shared.logger import get_logger

logger = get_logger(__name__)

def generate_campaign_overview(incumbent_status: IncumbentStatus | None, office_and_jurisdiction: str) -> str:
    """
    Generate a comprehensive campaign overview text.
    
    Args:
        incumbent_status (Optional[IncumbentStatus]): The incumbent status (ELECTED, APPOINTED, or N/A)
        office_and_jurisdiction (str): The name/title of the electoral race
    
    Returns:
        str: Formatted campaign overview section text. This will be fed into ai one more time at the very end to be added to the campaign plan.
    """

    logger.debug(f"Starting campaign overview generation with incumbent_status={incumbent_status}, office_and_jurisdiction='{office_and_jurisdiction}'")

    is_incumbent = incumbent_status in [IncumbentStatus.ELECTED, IncumbentStatus.APPOINTED]
    election_type = "re-election" if is_incumbent else "election"

    logger.debug(f"Determined election type: {election_type} (is_incumbent={is_incumbent})")

    overview_text = f"""
## 1. CAMPAIGN STRATEGY OVERVIEW

This plan provides a **comprehensive campaign roadmap** for your {election_type} to the {office_and_jurisdiction}. It is designed to translate strategic objectives into actionable fieldwork, communications, and voter outreach benchmarks. The plan supports alignment of resources, messaging, and execution around a single priority: reaching and turning out enough voters to win.
"""

    logger.info(f"Generated campaign overview for {election_type} to {office_and_jurisdiction}")
    logger.debug(f"Overview text length: {len(overview_text)} characters")

    return overview_text


if __name__ == "__main__":
    logger.info("Starting campaign overview generation examples")

    print(generate_campaign_overview(IncumbentStatus.ELECTED, "School Board"))
    print("---")
    print(generate_campaign_overview(IncumbentStatus.NOT_APPLICABLE, "School Board"))

    logger.info("Completed campaign overview generation examples")
