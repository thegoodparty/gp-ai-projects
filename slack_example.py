import json
import requests
from datetime import date
from typing import Dict, Any

from ai_generated_campaign_plan.orchestrator import CampaignPlanOrchestrator
from ai_generated_campaign_plan.schema.models import CampaignInfo, RaceType, IncumbentStatus

def parse_slack_command(text: str) -> Dict[str, Any]:
    """
    Parse Slack slash command text to extract campaign information.
    Example format: /campaign-plan candidate:"John Smith" election:"2025-11-04" office:"City Council, Boston, MA"
    """
    params = {}
    
    # Simple parsing - in production, you'd want more robust parsing
    parts = text.split()
    for part in parts:
        if ":" in part:
            key, value = part.split(":", 1)
            params[key.strip()] = value.strip('"')
    
    return params

def handle_slack_webhook_enhanced(request_body: Dict[str, Any]) -> Dict[str, Any]:
    """Enhanced Slack webhook handler with campaign plan generation."""
    
    # Extract command information
    command = request_body.get("command", "")
    text = request_body.get("text", "")
    user_id = request_body.get("user_id", "")
    channel_id = request_body.get("channel_id", "")
    
    if command == "/campaign-plan":
        try:
            # Parse parameters from text
            params = parse_slack_command(text)
            
            # Validate required parameters
            required_fields = [
                "candidate", "election", "office", "race_type", "incumbent_status",
                "seats", "opponents", "win_number", "total_voters", "cell_phones", "landlines"
            ]
            
            missing_fields = [field for field in required_fields if field not in params]
            
            if missing_fields:
                return {
                    "response_type": "ephemeral",
                    "text": f"Missing required fields: {', '.join(missing_fields)}",
                    "attachments": [{
                        "color": "warning",
                        "text": """Usage: /campaign-plan candidate:"John Smith" election:"2025-11-04" office:"City Council, Boston, MA" race_type:"Nonpartisan" incumbent_status:"N/A" seats:"1" opponents:"3" win_number:"5000" total_voters:"15000" cell_phones:"2000" landlines:"500" """
                    }]
                }
            
            # Create CampaignInfo object
            campaign_info = CampaignInfo(
                candidate_name=params["candidate"],
                election_date=date.fromisoformat(params["election"]),
                primary_date=date.fromisoformat(params["primary"]) if params.get("primary") else None,
                office_and_jurisdiction=params["office"],
                incumbent_status=IncumbentStatus(params["incumbent_status"]),
                race_type=RaceType(params["race_type"]),
                seats_available=int(params["seats"]),
                number_of_opponents=int(params["opponents"]),
                win_number=int(params["win_number"]),
                total_likely_voters=int(params["total_voters"]),
                available_cell_phones=int(params["cell_phones"]),
                available_landlines=int(params["landlines"]),
                additional_race_context=params.get("context", "")
            )
            
            # Generate campaign plan (this would typically be async)
            orchestrator = CampaignPlanOrchestrator()
            campaign_plan = orchestrator.generate_campaign_plan_sync(campaign_info)
            
            # Since Slack has response limits, you'd typically:
            # 1. Send immediate response saying "generating..."
            # 2. Use delayed response webhook to send the actual plan
            # 3. Or upload as a file to Slack
            
            return {
                "response_type": "in_channel",
                "text": f"Campaign plan generated for {params['candidate']}!",
                "attachments": [{
                    "color": "good",
                    "text": "Campaign plan has been generated successfully. Due to size limits, the full plan will be sent as a file.",
                    "fields": [
                        {
                            "title": "Candidate",
                            "value": params["candidate"],
                            "short": True
                        },
                        {
                            "title": "Election Date",
                            "value": params["election"],
                            "short": True
                        },
                        {
                            "title": "Office",
                            "value": params["office"],
                            "short": False
                        }
                    ]
                }]
            }
            
        except Exception as e:
            return {
                "response_type": "ephemeral",
                "text": f"Error generating campaign plan: {str(e)}",
                "attachments": [{
                    "color": "danger",
                    "text": "Please check your parameters and try again."
                }]
            }
    
    return {
        "response_type": "ephemeral",
        "text": "Unknown command. Use /campaign-plan with required parameters."
    }

# Example usage for testing
if __name__ == "__main__":
    # Example Slack webhook payload
    example_payload = {
        "command": "/campaign-plan",
        "text": 'candidate:"Sarah Johnson" election:"2025-11-05" office:"School Board, At-Large, Chicopee, MA" race_type:"Nonpartisan" incumbent_status:"N/A" seats:"3" opponents:"7" win_number:"2500" total_voters:"8500" cell_phones:"1200" landlines:"300"',
        "user_id": "U123456",
        "channel_id": "C123456"
    }
    
    response = handle_slack_webhook_enhanced(example_payload)
    print(json.dumps(response, indent=2)) 