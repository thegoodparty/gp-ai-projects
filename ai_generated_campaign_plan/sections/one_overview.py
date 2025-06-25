from typing import Optional
from ai_generated_campaign_plan.schema.models import IncumbentStatus


def generate_campaign_overview(incumbent_status: Optional[IncumbentStatus], office_and_jurisdiction: str) -> str:
    """
    Generate a comprehensive campaign overview text.
    
    Args:
        incumbent_status (Optional[IncumbentStatus]): The incumbent status (ELECTED, APPOINTED, or N/A)
        office_and_jurisdiction (str): The name/title of the electoral race
    
    Returns:
        str: Formatted campaign overview section text. This will be fed into ai one more time at the very end to be added to the campaign plan.
    """

    is_incumbent = incumbent_status in [IncumbentStatus.ELECTED, IncumbentStatus.APPOINTED]
    election_type = "re-election" if is_incumbent else "election"
    
    overview_text = (
        f"This plan provides a comprehensive campaign roadmap for your {election_type} "
        f"to the {office_and_jurisdiction}. It is designed to translate strategic objectives into "
        "actionable fieldwork, communications, and voter outreach benchmarks. The plan "
        "supports alignment of resources, messaging, and execution around a single "
        "priority: reaching and turning out enough voters to win."
    )
    
    return overview_text


if __name__ == "__main__":
    print(generate_campaign_overview(IncumbentStatus.ELECTED, "School Board"))
    print("---")
    print(generate_campaign_overview(IncumbentStatus.NOT_APPLICABLE, "School Board"))