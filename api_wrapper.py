import asyncio
import io
import json
import re
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Union

import aiohttp
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from reportlab.lib.colors import black
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from ai_generated_campaign_plan.orchestrator import CampaignPlanOrchestrator
from ai_generated_campaign_plan.schema.models import (
    CampaignInfo,
    IncumbentStatus,
    RaceType,
)
from shared.logger import get_logger

app = FastAPI(title="Campaign Plan Generator API", version="1.0.0")
logger = get_logger(__name__)

templates = Jinja2Templates(directory="templates")

# Store for progress tracking
progress_store: Dict[str, Dict[str, Any]] = {}

def parse_timeline_tasks(timeline_content: str) -> List[Dict[str, Any]]:
    """Parse timeline tasks from section 3 content into structured format."""
    tasks = []
    lines = timeline_content.split('\n')

    for line in lines:
        line = line.strip()
        # Look for lines matching: - Month DD | Event | Purpose
        if line.startswith('- ') and ' | ' in line:
            parts = line[2:].split(' | ')  # Remove '- ' prefix
            if len(parts) >= 3:
                date_str = parts[0].strip()
                event = parts[1].strip()
                purpose = parts[2].strip()

                # Try to parse the date
                try:
                    # Handle formats like "July 15" or "August 1"
                    current_year = date.today().year
                    date_with_year = f"{date_str}, {current_year}"
                    parsed_date = datetime.strptime(date_with_year, "%B %d, %Y").date()
                except ValueError:
                    # If parsing fails, use the original string
                    parsed_date = None

                tasks.append({
                    "date": date_str,
                    "parsed_date": parsed_date.isoformat() if parsed_date else None,
                    "title": event,
                    "description": purpose,
                    "type": "timeline"
                })

    return tasks

def parse_voter_contact_tasks(contact_content: str) -> List[Dict[str, Any]]:
    """Parse voter contact tasks from section 6 content into structured format."""
    tasks = []
    lines = contact_content.split('\n')

    for line in lines:
        line = line.strip()
        # Look for lines matching: - [MONTH DD] – Contact Type: Message
        if line.startswith('- [') and '] –' in line:
            # Extract date from brackets
            date_start = line.find('[') + 1
            date_end = line.find(']')
            if date_end > date_start:
                date_str = line[date_start:date_end].strip()

                # Extract the rest after ] –
                rest = line[date_end + 2:].strip()  # Skip '] –'

                # Split on colon to get contact type and message
                if ':' in rest:
                    contact_parts = rest.split(':', 1)
                    contact_type = contact_parts[0].strip()
                    message = contact_parts[1].strip() if len(contact_parts) > 1 else ""
                else:
                    contact_type = rest
                    message = ""

                # Try to parse the date
                try:
                    current_year = date.today().year
                    date_with_year = f"{date_str}, {current_year}"
                    parsed_date = datetime.strptime(date_with_year, "%B %d, %Y").date()
                except ValueError:
                    parsed_date = None

                tasks.append({
                    "date": date_str,
                    "parsed_date": parsed_date.isoformat() if parsed_date else None,
                    "title": contact_type,
                    "description": message,
                    "type": "voter_contact"
                })

    return tasks

def convert_campaign_plan_to_json(campaign_plan_text: str, campaign_info: CampaignInfo) -> Dict[str, Any]:
    """Convert campaign plan text to structured JSON format."""
    # Split the plan into sections
    sections = {}
    current_section = None
    current_content = []

    lines = campaign_plan_text.split('\n')
    for line in lines:
        # Check if this is a section header (## N. SECTION NAME)
        if line.strip().startswith('## ') and '. ' in line:
            # Save previous section if exists
            if current_section is not None:
                sections[current_section] = '\n'.join(current_content)

            # Start new section
            section_match = re.match(r'## (\d+)\. (.+)', line.strip())
            if section_match:
                section_num = int(section_match.group(1))
                section_name = section_match.group(2)
                current_section = section_num
                current_content = [line]
            else:
                current_content.append(line)
        else:
            current_content.append(line)

    # Save last section
    if current_section is not None:
        sections[current_section] = '\n'.join(current_content)

    # Parse tasks from sections 3 and 6
    timeline_tasks = []
    voter_contact_tasks = []

    if 3 in sections:
        timeline_tasks = parse_timeline_tasks(sections[3])

    if 6 in sections:
        voter_contact_tasks = parse_voter_contact_tasks(sections[6])

    # Build JSON response
    json_response = {
        "campaign_info": {
            "candidate_name": campaign_info.candidate_name,
            "office_and_jurisdiction": campaign_info.office_and_jurisdiction,
            "election_date": campaign_info.election_date.isoformat(),
            "primary_date": campaign_info.primary_date.isoformat() if campaign_info.primary_date else None,
            "generated_date": date.today().isoformat()
        },
        "sections": {},
        "tasks": {
            "timeline": timeline_tasks,
            "voter_contact": voter_contact_tasks,
            "all_tasks": timeline_tasks + voter_contact_tasks
        }
    }

    # Add all sections with their content in markdown format
    section_names = {
        1: "overview",
        2: "strategic_landscape_electoral_goals",
        3: "campaign_timeline",
        4: "recommended_total_budget",
        5: "know_your_community",
        6: "voter_contact_plan"
    }

    for section_num, content in sections.items():
        section_key = section_names.get(section_num, f"section_{section_num}")
        json_response["sections"][section_key] = content

    return json_response

class ProgressTracker:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.progress = 0
        self.status = "starting"
        self.message = "Initializing campaign plan generation..."
        self.logs = []

    def update(self, progress: int, status: str, message: str, log_entry: str = None) -> None:
        self.progress = progress
        self.status = status
        self.message = message
        if log_entry:
            self.logs.append(log_entry)

        # Get existing session data to preserve it
        existing_data = progress_store.get(self.session_id, {})

        # Update only the progress-related fields, preserving other data
        progress_store[self.session_id] = {
            **existing_data,  # Preserve existing data like pdf_data, filename
            "progress": self.progress,
            "status": self.status,
            "message": self.message,
            "logs": self.logs[-10:],  # Keep last 10 log entries
            "timestamp": date.today().isoformat()
        }

@app.get("/", response_class=HTMLResponse)
async def form_page(request: Request) -> HTMLResponse:
    """Serve the HTML form for non-technical users."""
    return templates.TemplateResponse("campaign_form.html", {"request": request})

@app.post("/generate-campaign-plan", response_model=None)
async def generate_campaign_plan(
    campaign_info: CampaignInfo, 
    format: str = Query("pdf", pattern="^(pdf|json)$")
) -> Union[StreamingResponse, JSONResponse]:
    """Generate campaign plan from JSON input and return as PDF or JSON."""
    try:
        logger.info(f"Generating campaign plan for {campaign_info.candidate_name} in {format} format")

        # Generate campaign plan using async method
        orchestrator = CampaignPlanOrchestrator()
        campaign_plan_text = await orchestrator.generate_complete_campaign_plan(campaign_info)

        if format == "json":
            # Convert to structured JSON
            json_data = convert_campaign_plan_to_json(campaign_plan_text, campaign_info)
            return JSONResponse(content=json_data)

        else:  # format == "pdf"
            # Convert to PDF
            pdf_buffer = create_pdf_from_text(campaign_plan_text, campaign_info)

            # Create filename
            safe_candidate_name = "".join(c for c in campaign_info.candidate_name if c.isalnum() or c in (' ', '-', '_')).strip()
            filename = f"campaign_plan_{safe_candidate_name.replace(' ', '_')}.pdf"

            # Return as downloadable PDF
            return StreamingResponse(
                io.BytesIO(pdf_buffer.getvalue()),
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )

    except Exception as e:
        logger.error(f"Error generating campaign plan: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error generating campaign plan: {e!s}")

@app.post("/generate-campaign-plan-json")
async def generate_campaign_plan_json_only(campaign_info: CampaignInfo):
    """Generate campaign plan from JSON input and return as JSON only."""
    try:
        logger.info(f"Generating JSON campaign plan for {campaign_info.candidate_name}")

        # Generate campaign plan using async method
        orchestrator = CampaignPlanOrchestrator()
        campaign_plan_text = await orchestrator.generate_complete_campaign_plan(campaign_info)

        # Convert to structured JSON
        json_data = convert_campaign_plan_to_json(campaign_plan_text, campaign_info)
        return JSONResponse(content=json_data)

    except Exception as e:
        logger.error(f"Error generating JSON campaign plan: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error generating campaign plan: {e!s}")

@app.post("/generate-campaign-plan-async")
async def generate_campaign_plan_async(
    request: Request
):
    """Start async campaign plan generation with optional webhook callback."""
    try:
        # Parse the JSON body manually to get both campaign_info and webhook_url
        body = await request.json()
        
        # Extract webhook_url if present
        webhook_url = body.pop("webhook_url", None)
        
        # Create CampaignInfo from remaining body
        campaign_info = CampaignInfo(**body)
        
        logger.info(f"Starting async generation for {campaign_info.candidate_name}")

        # Generate unique session ID
        session_id = str(uuid.uuid4())

        # Initialize progress tracker
        progress_tracker = ProgressTracker(session_id)

        # Start background task for generation
        asyncio.create_task(generate_campaign_plan_background(campaign_info, progress_tracker, webhook_url))

        return {
            "session_id": session_id,
            "status": "processing",
            "progress_url": f"/progress/{session_id}",
            "download_url": f"/download/{session_id}",
            "download_json_url": f"/download/{session_id}?format=json",
            "webhook_url": webhook_url
        }

    except Exception as e:
        logger.error(f"Error starting async campaign plan generation: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error starting generation: {e!s}")

# Add convenient aliases for download endpoints
@app.get("/download-pdf/{session_id}")
async def download_pdf_alias(session_id: str):
    """Download the generated PDF (convenience alias)."""
    return await download_pdf(session_id, format="pdf")

@app.get("/download-json/{session_id}")
async def download_json_alias(session_id: str):
    """Download the generated JSON (convenience alias)."""
    return await download_pdf(session_id, format="json")

@app.post("/start-campaign-plan-generation")
async def start_campaign_plan_generation(
    request: Request,
    candidate_name: str = Form(...),
    election_date: str = Form(...),
    office_and_jurisdiction: str = Form(...),
    race_type: str = Form(...),
    incumbent_status: str = Form(...),
    seats_available: int = Form(...),
    number_of_opponents: int = Form(...),
    win_number: int = Form(...),
    total_likely_voters: int = Form(...),
    available_cell_phones: int = Form(...),
    available_landlines: int = Form(...),
    primary_date: str | None = Form(None),
    additional_race_context: str | None = Form(None),
    webhook_url: str | None = Form(None)
):
    """Start campaign plan generation and return session ID for progress tracking."""
    try:
        # Parse dates
        election_date_parsed = date.fromisoformat(election_date)
        primary_date_parsed = date.fromisoformat(primary_date) if primary_date else None

        # Create CampaignInfo object
        campaign_info = CampaignInfo(
            candidate_name=candidate_name,
            primary_date=primary_date_parsed,
            election_date=election_date_parsed,
            office_and_jurisdiction=office_and_jurisdiction,
            incumbent_status=IncumbentStatus(incumbent_status),
            race_type=RaceType(race_type),
            seats_available=seats_available,
            number_of_opponents=number_of_opponents,
            win_number=win_number,
            total_likely_voters=total_likely_voters,
            available_cell_phones=available_cell_phones,
            available_landlines=available_landlines,
            additional_race_context=additional_race_context
        )

        # Generate unique session ID
        session_id = str(uuid.uuid4())

        # Initialize progress tracker
        progress_tracker = ProgressTracker(session_id)

        # Start background task for generation
        asyncio.create_task(generate_campaign_plan_background(campaign_info, progress_tracker, webhook_url))

        return {"session_id": session_id}

    except ValidationError as e:
        logger.error(f"Validation error: {e!s}")
        raise HTTPException(status_code=400, detail=f"Validation error: {e!s}")
    except Exception as e:
        logger.error(f"Error starting campaign plan generation: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error starting generation: {e!s}")

async def generate_campaign_plan_background(campaign_info: CampaignInfo, progress_tracker: ProgressTracker, webhook_url: str = None):
    """Background task to generate campaign plan with progress tracking."""
    try:
        progress_tracker.update(10, "processing", "Cleaning and validating campaign data...",
                              f"Starting generation for {campaign_info.candidate_name}")

        # Create orchestrator with progress tracking
        orchestrator = CampaignPlanOrchestrator()

        # Step 1: Clean campaign data
        progress_tracker.update(20, "processing", "Extracting location and date information...",
                              "Cleaning campaign information using AI")

        try:
            cleaned_campaign_info = orchestrator.campaign_utils.clean_campaign_info(campaign_info)
            logger.info(f"Successfully cleaned campaign data for {campaign_info.candidate_name}")
        except Exception as e:
            logger.error(f"Failed to clean campaign data: {e!s}")
            raise

        progress_tracker.update(30, "processing", "Generating campaign overview...",
                              "Successfully cleaned campaign data")

        # Step 2: Generate sections with progress updates
        sections = {}

        # Generate contact strategies first (needed for Section 6)
        progress_tracker.update(30, "processing", "Generating contact strategies...",
                              "Calculating optimal contact strategies")

        try:
            if cleaned_campaign_info.has_primary:
                primary_contact_strategy = orchestrator.campaign_utils.optimize_contact_strategy(
                    date.today(),
                    cleaned_campaign_info.primary_date
                )
                general_contact_strategy = orchestrator.campaign_utils.optimize_contact_strategy(
                    cleaned_campaign_info.primary_date,
                    cleaned_campaign_info.election_date
                )
            else:
                general_contact_strategy = orchestrator.campaign_utils.optimize_contact_strategy(
                    date.today(),
                    cleaned_campaign_info.election_date
                )
                primary_contact_strategy = None
            logger.info("Successfully generated contact strategies")
        except Exception as e:
            logger.error(f"Failed to generate contact strategies: {e!s}")
            raise

        # Section 1: Overview
        progress_tracker.update(35, "processing", "Creating campaign strategy overview...",
                              "Generating section 1: Campaign Overview")

        try:
            from ai_generated_campaign_plan.sections.one_overview import (
                generate_campaign_overview,
            )
            sections[1] = generate_campaign_overview(
                incumbent_status=campaign_info.incumbent_status,
                office_and_jurisdiction=campaign_info.office_and_jurisdiction
            )
            logger.info("Successfully generated section 1: Overview")
        except Exception as e:
            logger.error(f"Failed to generate section 1: {e!s}")
            sections[1] = "1. CAMPAIGN OVERVIEW\n\nSection could not be generated due to an error."

        # Section 2: Strategic Landscape
        progress_tracker.update(45, "processing", "Analyzing strategic landscape and electoral goals...",
                              "Generating section 2: Strategic Landscape")

        try:
            from ai_generated_campaign_plan.sections.two_strategic_landscape_electoral_goals import (
                StrategicLandscapeElectoralGoalsGenerator,
            )
            strategic_generator = StrategicLandscapeElectoralGoalsGenerator()
            if hasattr(strategic_generator, 'llm_client'):
                strategic_generator.llm_client = orchestrator.llm_client
            sections[2] = strategic_generator.generate_section(campaign_info)
            logger.info("Successfully generated section 2: Strategic Landscape")
        except Exception as e:
            logger.error(f"Failed to generate section 2: {e!s}")
            sections[2] = "2. STRATEGIC LANDSCAPE & ELECTORAL GOALS\n\nSection could not be generated due to an error."

        # Section 4: Budget (generate before timeline as it doesn't depend on other sections)
        progress_tracker.update(55, "processing", "Calculating recommended budget...",
                              "Generating section 4: Budget Recommendations")

        try:
            from ai_generated_campaign_plan.sections.four_recommended_total_budget import (
                generate_recommended_total_budget,
            )
            sections[4] = generate_recommended_total_budget(cleaned_campaign_info)
            logger.info("Successfully generated section 4: Budget")
        except Exception as e:
            logger.error(f"Failed to generate section 4: {e!s}")
            sections[4] = "4. RECOMMENDED TOTAL BUDGET\n\nSection could not be generated due to an error."

        # Section 5: Community Research
        progress_tracker.update(65, "processing", "Researching community events and demographics...",
                              "Generating section 5: Community Research (this may take longer)")

        try:
            from ai_generated_campaign_plan.sections.five_know_your_community import (
                KnowYourCommunityGenerator,
            )
            community_generator = KnowYourCommunityGenerator()
            if hasattr(community_generator, 'llm_client'):
                community_generator.llm_client = orchestrator.llm_client
            sections[5] = await community_generator.generate_section(cleaned_campaign_info)
            logger.info("Successfully generated section 5: Community Research")
        except Exception as e:
            logger.error(f"Failed to generate section 5: {e!s}")
            sections[5] = "5. KNOW YOUR COMMUNITY\n\nSection could not be generated due to an error."

        # Section 6: Voter Contact Plan
        progress_tracker.update(75, "processing", "Creating voter contact strategy...",
                              "Generating section 6: Voter Contact Plan")

        try:
            from ai_generated_campaign_plan.sections.six_voter_contact_plan import (
                VoterContactPlanGenerator,
            )
            contact_generator = VoterContactPlanGenerator()
            if hasattr(contact_generator, 'llm_client'):
                contact_generator.llm_client = orchestrator.llm_client
            sections[6] = await contact_generator.generate_section(cleaned_campaign_info, primary_contact_strategy, general_contact_strategy)
            logger.info("Successfully generated section 6: Voter Contact Plan")
        except Exception as e:
            logger.error(f"Failed to generate section 6: {e!s}")
            sections[6] = "6. VOTER CONTACT PLAN\n\nSection could not be generated due to an error."

        # Section 3: Campaign Timeline (depends on sections 5 and 6)
        progress_tracker.update(85, "processing", "Creating campaign timeline...",
                              "Generating section 3: Campaign Timeline")

        try:
            from ai_generated_campaign_plan.sections.three_campaign_timeline import (
                CampaignTimelineGenerator,
            )
            timeline_generator = CampaignTimelineGenerator()
            if hasattr(timeline_generator, 'llm_client'):
                timeline_generator.llm_client = orchestrator.llm_client
            sections[3] = await timeline_generator.generate_section(cleaned_campaign_info, sections[5], sections[6])
            logger.info("Successfully generated section 3: Campaign Timeline")
        except Exception as e:
            logger.error(f"Failed to generate section 3: {e!s}")
            sections[3] = "3. CAMPAIGN TIMELINE\n\nSection could not be generated due to an error."

        # Assemble final document
        progress_tracker.update(95, "processing", "Assembling final campaign plan document...",
                              "Combining all sections into final document")

        try:
            final_plan = orchestrator._assemble_final_document(campaign_info, sections)
            logger.info(f"Successfully assembled final document ({len(final_plan)} characters)")
        except Exception as e:
            logger.error(f"Failed to assemble final document: {e!s}")
            raise

        # Convert to PDF
        progress_tracker.update(98, "processing", "Converting to PDF format...",
                              "Creating PDF document")

        try:
            pdf_buffer = create_pdf_from_text(final_plan, campaign_info)
            logger.info(f"Successfully created PDF ({len(pdf_buffer.getvalue())} bytes)")
        except Exception as e:
            logger.error(f"Failed to create PDF: {e!s}")
            raise

        # Store final result
        safe_candidate_name = "".join(c for c in campaign_info.candidate_name if c.isalnum() or c in (' ', '-', '_')).strip()
        filename = f"campaign_plan_{safe_candidate_name.replace(' ', '_')}.pdf"

        # Ensure session exists in progress_store
        if progress_tracker.session_id not in progress_store:
            progress_store[progress_tracker.session_id] = {}

        try:
            # Store the PDF data and JSON data
            pdf_data = pdf_buffer.getvalue()
            json_data = convert_campaign_plan_to_json(final_plan, campaign_info)

            progress_store[progress_tracker.session_id]["pdf_data"] = pdf_data
            progress_store[progress_tracker.session_id]["json_data"] = json_data
            progress_store[progress_tracker.session_id]["filename"] = filename

            # Debug logging
            logger.info(f"PDF stored for session {progress_tracker.session_id}, filename: {filename}")
            logger.info(f"PDF data size: {len(pdf_data)} bytes")
            logger.info(f"Current progress_store keys: {list(progress_store.keys())}")
            logger.info(f"Session data keys: {list(progress_store[progress_tracker.session_id].keys())}")

            # Verify the data was stored
            if "pdf_data" in progress_store[progress_tracker.session_id]:
                logger.info("✓ PDF data successfully stored and verified")
            else:
                logger.error("✗ PDF data not found after storage attempt")
                raise Exception("PDF data storage failed")

        except Exception as e:
            logger.error(f"Failed to store PDF data: {e!s}")
            raise

        progress_tracker.update(100, "completed", "Campaign plan generation complete!",
                              "PDF ready for download")

        # Call webhook if provided
        if webhook_url:
            await call_webhook(webhook_url, progress_tracker.session_id, "completed", json_data)

    except Exception as e:
        logger.error(f"Error in background generation: {e!s}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        progress_tracker.update(0, "error", f"Error: {e!s}",
                              f"Generation failed: {e!s}")
        
        # Call webhook on error if provided
        if webhook_url:
            await call_webhook(webhook_url, progress_tracker.session_id, "error", {"error": str(e)})

@app.get("/progress/{session_id}")
async def get_progress(session_id: str):
    """Get progress for a specific session."""
    if session_id not in progress_store:
        raise HTTPException(status_code=404, detail="Session not found")

    # Filter out non-serializable data like PDF bytes
    session_data = progress_store[session_id]
    filtered_data = {
        "progress": session_data.get("progress", 0),
        "status": session_data.get("status", "unknown"),
        "message": session_data.get("message", ""),
        "logs": session_data.get("logs", []),
        "timestamp": session_data.get("timestamp", ""),
        "has_pdf": "pdf_data" in session_data,
        "has_json": "json_data" in session_data
    }
    
    return filtered_data

@app.get("/progress-stream/{session_id}")
async def progress_stream(session_id: str):
    """Stream progress updates using Server-Sent Events."""

    async def event_generator():
        last_progress = -1
        while True:
            if session_id in progress_store:
                current_data = progress_store[session_id]
                current_progress = current_data.get("progress", 0)

                # Only send update if progress changed
                if current_progress != last_progress:
                    # Filter out non-serializable data like PDF bytes
                    filtered_data = {
                        "progress": current_data.get("progress", 0),
                        "status": current_data.get("status", "unknown"),
                        "message": current_data.get("message", ""),
                        "logs": current_data.get("logs", []),
                        "timestamp": current_data.get("timestamp", ""),
                        "has_pdf": "pdf_data" in current_data  # Just indicate if PDF exists
                    }

                    yield f"data: {json.dumps(filtered_data)}\n\n"
                    last_progress = current_progress

                # Stop streaming if completed or error
                if current_data.get("status") in ["completed", "error"]:
                    break

            await asyncio.sleep(0.5)  # Check every 500ms

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Cache-Control"
        }
    )

@app.get("/download/{session_id}")
async def download_pdf(session_id: str, format: str = Query("pdf", pattern="^(pdf|json)$")):
    """Download the generated PDF or JSON."""
    logger.info(f"Download requested for session: {session_id}")
    logger.info(f"Available sessions: {list(progress_store.keys())}")

    if session_id not in progress_store:
        logger.error(f"Session {session_id} not found in progress_store")
        logger.error(f"Available sessions: {list(progress_store.keys())}")
        raise HTTPException(status_code=404, detail="Session not found")

    session_data = progress_store[session_id]
    logger.info(f"Session data keys: {list(session_data.keys())}")
    logger.info(f"Session status: {session_data.get('status', 'unknown')}")

    if session_data.get("status") != "completed":
        current_status = session_data.get("status", "unknown")
        logger.error(f"Generation not completed, status: {current_status}")

        # Provide more helpful error messages based on status
        if current_status == "error":
            error_msg = session_data.get("message", "Unknown error occurred")
            raise HTTPException(status_code=500, detail=f"Generation failed: {error_msg}")
        else:
            raise HTTPException(status_code=400, detail=f"Generation not completed (status: {current_status})")

    if format == "json":
        if "json_data" not in session_data:
            logger.error(f"JSON data not found in session {session_id}")
            logger.error(f"Session data contents: {list(session_data.keys())}")

            # Check if we have detailed error information
            if "logs" in session_data:
                logger.error(f"Session logs: {session_data['logs']}")

            raise HTTPException(status_code=404, detail="JSON not found - generation may have failed")

        json_data = session_data["json_data"]
        if not json_data:
            logger.error(f"JSON data is empty for session {session_id}")
            raise HTTPException(status_code=500, detail="JSON data is empty")

        logger.info(f"Serving JSON for session {session_id}")

        # Clean up session data after download
        asyncio.create_task(cleanup_session(session_id))

        return JSONResponse(content=json_data)

    else:  # format == "pdf"
        if "pdf_data" not in session_data:
            logger.error(f"PDF data not found in session {session_id}")
            logger.error(f"Session data contents: {list(session_data.keys())}")

            # Check if we have detailed error information
            if "logs" in session_data:
                logger.error(f"Session logs: {session_data['logs']}")

            raise HTTPException(status_code=404, detail="PDF not found - generation may have failed")

        # Additional validation
        pdf_data = session_data["pdf_data"]
        if not pdf_data or len(pdf_data) == 0:
            logger.error(f"PDF data is empty for session {session_id}")
            raise HTTPException(status_code=500, detail="PDF data is empty")

        filename = session_data.get("filename", "campaign_plan.pdf")
        logger.info(f"Serving PDF: {filename}, size: {len(pdf_data)} bytes")

        # Clean up session data after download
        asyncio.create_task(cleanup_session(session_id))

        return StreamingResponse(
            io.BytesIO(pdf_data),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

async def call_webhook(webhook_url: str, session_id: str, status: str, data: Dict[str, Any]):
    """Call webhook URL with completion status and data."""
    try:
        webhook_payload = {
            "session_id": session_id,
            "status": status,
            "timestamp": datetime.utcnow().isoformat(),
            "data": data
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                json=webhook_payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    logger.info(f"Successfully called webhook for session {session_id}")
                else:
                    logger.warning(f"Webhook returned status {response.status} for session {session_id}")
                    
    except Exception as e:
        logger.error(f"Failed to call webhook for session {session_id}: {e!s}")
        # Don't re-raise - webhook failure shouldn't fail the generation

async def cleanup_session(session_id: str):
    """Clean up session data after 15 minutes."""
    await asyncio.sleep(900)  # Wait 15 minutes (300 seconds)
    if session_id in progress_store:
        logger.info(f"Cleaning up session {session_id}")
        del progress_store[session_id]

@app.post("/generate-campaign-plan-form")
async def generate_campaign_plan_form(
    request: Request,
    candidate_name: str = Form(...),
    election_date: str = Form(...),
    office_and_jurisdiction: str = Form(...),
    race_type: str = Form(...),
    incumbent_status: str = Form(...),
    seats_available: int = Form(...),
    number_of_opponents: int = Form(...),
    win_number: int = Form(...),
    total_likely_voters: int = Form(...),
    available_cell_phones: int = Form(...),
    available_landlines: int = Form(...),
    primary_date: str | None = Form(None),
    additional_race_context: str | None = Form(None)
):
    """Generate campaign plan from form submission."""
    try:
        # Parse dates
        election_date_parsed = date.fromisoformat(election_date)
        primary_date_parsed = date.fromisoformat(primary_date) if primary_date else None

        # Create CampaignInfo object
        campaign_info = CampaignInfo(
            candidate_name=candidate_name,
            primary_date=primary_date_parsed,
            election_date=election_date_parsed,
            office_and_jurisdiction=office_and_jurisdiction,
            incumbent_status=IncumbentStatus(incumbent_status),
            race_type=RaceType(race_type),
            seats_available=seats_available,
            number_of_opponents=number_of_opponents,
            win_number=win_number,
            total_likely_voters=total_likely_voters,
            available_cell_phones=available_cell_phones,
            available_landlines=available_landlines,
            additional_race_context=additional_race_context
        )

        return await generate_campaign_plan_json_only(campaign_info)

    except ValidationError as e:
        logger.error(f"Validation error: {e!s}")
        raise HTTPException(status_code=400, detail=f"Validation error: {e!s}")
    except Exception as e:
        logger.error(f"Error processing form: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error processing form: {e!s}")

@app.post("/slack-webhook")
async def slack_webhook(request: Request):
    """Handle Slack webhook for campaign plan generation."""
    try:
        body = await request.json()

        # Extract campaign info from Slack message
        # This is a simplified example - you'd need to parse the actual Slack payload
        if "text" in body:
            # Parse text for campaign info or use slash command parameters
            # For now, return instructions
            return {
                "response_type": "ephemeral",
                "text": "Please provide campaign information in JSON format or use the web form at /",
                "attachments": [
                    {
                        "color": "good",
                        "fields": [
                            {
                                "title": "Web Form",
                                "value": "Visit the web form to fill out campaign details",
                                "short": True
                            },
                            {
                                "title": "API Endpoint",
                                "value": "POST /generate-campaign-plan with JSON payload",
                                "short": True
                            }
                        ]
                    }
                ]
            }

        return {"response_type": "ephemeral", "text": "Invalid request format"}

    except Exception as e:
        logger.error(f"Error processing Slack webhook: {e!s}")
        return {"response_type": "ephemeral", "text": f"Error: {e!s}"}

def create_pdf_from_text(text: str, campaign_info: CampaignInfo) -> io.BytesIO:
    """Convert campaign plan text to PDF with proper markdown formatting."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=72, leftMargin=72, topMargin=72, bottomMargin=18)

    styles = getSampleStyleSheet()

    # Create custom styles
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=20,
        spaceAfter=30,
        alignment=TA_CENTER,
        textColor=black
    )

    h1_style = ParagraphStyle(
        'CustomH1',
        parent=styles['Heading1'],
        fontSize=16,
        spaceAfter=15,
        spaceBefore=20,
        textColor=black
    )

    h2_style = ParagraphStyle(
        'CustomH2',
        parent=styles['Heading2'],
        fontSize=14,
        spaceAfter=12,
        spaceBefore=18,
        textColor=black
    )

    h3_style = ParagraphStyle(
        'CustomH3',
        parent=styles['Heading3'],
        fontSize=12,
        spaceAfter=10,
        spaceBefore=15,
        textColor=black
    )

    normal_style = ParagraphStyle(
        'CustomNormal',
        parent=styles['Normal'],
        fontSize=10,
        spaceAfter=6,
        alignment=TA_LEFT
    )

    bullet_style = ParagraphStyle(
        'CustomBullet',
        parent=styles['Normal'],
        fontSize=10,
        spaceAfter=4,
        leftIndent=20,
        alignment=TA_LEFT
    )

    def process_markdown_formatting(text):
        """Process basic markdown formatting in text."""
        # Handle bold text **text**
        text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)

        # Handle italic text *text*
        text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)

        # Handle inline code `code`
        text = re.sub(r'`(.*?)`', r'<font name="Courier">\1</font>', text)

        # Escape any remaining special characters for reportlab
        text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        # Restore our formatting tags
        text = text.replace('&lt;b&gt;', '<b>').replace('&lt;/b&gt;', '</b>')
        text = text.replace('&lt;i&gt;', '<i>').replace('&lt;/i&gt;', '</i>')
        text = text.replace('&lt;font name="Courier"&gt;', '<font name="Courier">').replace('&lt;/font&gt;', '</font>')

        return text

    # Build the document
    story = []

    # Split text into lines and process
    lines = text.split('\n')

    for line in lines:
        original_line = line
        line = line.strip()

        if not line:
            story.append(Spacer(1, 6))
            continue

        # Handle markdown headers
        if line.startswith('# '):
            # H1 header
            header_text = line[2:].strip()
            story.append(Paragraph(process_markdown_formatting(header_text), h1_style))
        elif line.startswith('## '):
            # H2 header
            header_text = line[3:].strip()
            story.append(Paragraph(process_markdown_formatting(header_text), h2_style))
        elif line.startswith('### '):
            # H3 header
            header_text = line[4:].strip()
            story.append(Paragraph(process_markdown_formatting(header_text), h3_style))
        elif line.startswith('#### '):
            # H4 header (treat as H3)
            header_text = line[5:].strip()
            story.append(Paragraph(process_markdown_formatting(header_text), h3_style))
        # Handle bullet points
        elif line.startswith('- ') or line.startswith('* '):
            bullet_text = line[2:].strip()
            story.append(Paragraph(f"• {process_markdown_formatting(bullet_text)}", bullet_style))
        # Handle numbered lists
        elif re.match(r'^\d+\.\s', line):
            story.append(Paragraph(process_markdown_formatting(line), bullet_style))
        # Title (first line)
        elif line.startswith('CAMPAIGN PLAN'):
            story.append(Paragraph(line, title_style))
        # Section headers (lines with numbers followed by periods - fallback for non-markdown)
        elif line.startswith(('1.', '2.', '3.', '4.', '5.', '6.')) and line.count('.') == 1:
            story.append(Paragraph(process_markdown_formatting(line), h1_style))
        # Subsection headers (lines with letters or multiple numbers - fallback)
        elif line.startswith(('A.', 'B.', 'C.', 'D.', 'E.')) or ('.' in line[:10] and re.match(r'^[A-Z0-9]+\.', line)):
            story.append(Paragraph(process_markdown_formatting(line), h2_style))
        # Separator lines
        elif line.startswith('═'):
            story.append(Spacer(1, 12))
        # Handle code blocks (simple detection)
        elif line.startswith('```'):
            story.append(Spacer(1, 6))
            continue
        # Regular content
        # Check if line has significant indentation (preserve it)
        elif original_line.startswith('    ') or original_line.startswith('\t'):
            # Treat as code or indented content
            story.append(Paragraph(f'<font name="Courier">{process_markdown_formatting(line)}</font>', normal_style))
        else:
            story.append(Paragraph(process_markdown_formatting(line), normal_style))

    # Build PDF
    doc.build(story)
    buffer.seek(0)
    return buffer

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": date.today().isoformat()}

# Error handlers
@app.exception_handler(ValidationError)
async def validation_exception_handler(request: Request, exc: ValidationError):
    logger.error(f"Validation error: {exc!s}")
    return JSONResponse(status_code=400, content={"detail": str(exc)})

@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: Exception):
    logger.error(f"Internal server error: {exc!s}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
