import io
import re
import json
import asyncio
import uuid
from datetime import date
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
import uvicorn
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib.colors import black, blue

from ai_generated_campaign_plan.orchestrator import CampaignPlanOrchestrator
from ai_generated_campaign_plan.schema.models import CampaignInfo, RaceType, IncumbentStatus
from shared.logger import get_logger

app = FastAPI(title="Campaign Plan Generator API", version="1.0.0")
logger = get_logger(__name__)

templates = Jinja2Templates(directory="templates")

# Store for progress tracking
progress_store: Dict[str, Dict[str, Any]] = {}

class ProgressTracker:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.progress = 0
        self.status = "starting"
        self.message = "Initializing campaign plan generation..."
        self.logs = []
        
    def update(self, progress: int, status: str, message: str, log_entry: str = None):
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
async def form_page(request: Request):
    """Serve the HTML form for non-technical users."""
    return templates.TemplateResponse("campaign_form.html", {"request": request})

@app.post("/generate-campaign-plan")
async def generate_campaign_plan_json(campaign_info: CampaignInfo):
    """Generate campaign plan from JSON input and return as downloadable PDF."""
    try:
        logger.info(f"Generating campaign plan for {campaign_info.candidate_name}")
        
        # Generate campaign plan using async method
        orchestrator = CampaignPlanOrchestrator()
        campaign_plan_text = await orchestrator.generate_complete_campaign_plan(campaign_info)
        
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
        logger.error(f"Error generating campaign plan: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error generating campaign plan: {str(e)}")

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
    primary_date: Optional[str] = Form(None),
    additional_race_context: Optional[str] = Form(None)
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
        asyncio.create_task(generate_campaign_plan_background(campaign_info, progress_tracker))
        
        return {"session_id": session_id}
        
    except ValidationError as e:
        logger.error(f"Validation error: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Validation error: {str(e)}")
    except Exception as e:
        logger.error(f"Error starting campaign plan generation: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error starting generation: {str(e)}")

async def generate_campaign_plan_background(campaign_info: CampaignInfo, progress_tracker: ProgressTracker):
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
            logger.error(f"Failed to clean campaign data: {str(e)}")
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
            logger.error(f"Failed to generate contact strategies: {str(e)}")
            raise

        # Section 1: Overview
        progress_tracker.update(35, "processing", "Creating campaign strategy overview...", 
                              "Generating section 1: Campaign Overview")
        
        try:
            from ai_generated_campaign_plan.sections.one_overview import generate_campaign_overview
            sections[1] = generate_campaign_overview(
                incumbent_status=campaign_info.incumbent_status,
                office_and_jurisdiction=campaign_info.office_and_jurisdiction
            )
            logger.info("Successfully generated section 1: Overview")
        except Exception as e:
            logger.error(f"Failed to generate section 1: {str(e)}")
            sections[1] = "1. CAMPAIGN OVERVIEW\n\nSection could not be generated due to an error."
        
        # Section 2: Strategic Landscape
        progress_tracker.update(45, "processing", "Analyzing strategic landscape and electoral goals...", 
                              "Generating section 2: Strategic Landscape")
        
        try:
            from ai_generated_campaign_plan.sections.two_strategic_landscape_electoral_goals import StrategicLandscapeElectoralGoalsGenerator
            strategic_generator = StrategicLandscapeElectoralGoalsGenerator()
            if hasattr(strategic_generator, 'llm_client'):
                strategic_generator.llm_client = orchestrator.llm_client
            sections[2] = strategic_generator.generate_section(campaign_info)
            logger.info("Successfully generated section 2: Strategic Landscape")
        except Exception as e:
            logger.error(f"Failed to generate section 2: {str(e)}")
            sections[2] = "2. STRATEGIC LANDSCAPE & ELECTORAL GOALS\n\nSection could not be generated due to an error."
        
        # Section 4: Budget (generate before timeline as it doesn't depend on other sections)
        progress_tracker.update(55, "processing", "Calculating recommended budget...", 
                              "Generating section 4: Budget Recommendations")
        
        try:
            from ai_generated_campaign_plan.sections.four_recommended_total_budget import generate_recommended_total_budget
            sections[4] = generate_recommended_total_budget(cleaned_campaign_info)
            logger.info("Successfully generated section 4: Budget")
        except Exception as e:
            logger.error(f"Failed to generate section 4: {str(e)}")
            sections[4] = "4. RECOMMENDED TOTAL BUDGET\n\nSection could not be generated due to an error."
        
        # Section 5: Community Research
        progress_tracker.update(65, "processing", "Researching community events and demographics...", 
                              "Generating section 5: Community Research (this may take longer)")
        
        try:
            from ai_generated_campaign_plan.sections.five_know_your_community import KnowYourCommunityGenerator
            community_generator = KnowYourCommunityGenerator()
            if hasattr(community_generator, 'llm_client'):
                community_generator.llm_client = orchestrator.llm_client
            sections[5] = await community_generator.generate_section(cleaned_campaign_info)
            logger.info("Successfully generated section 5: Community Research")
        except Exception as e:
            logger.error(f"Failed to generate section 5: {str(e)}")
            sections[5] = "5. KNOW YOUR COMMUNITY\n\nSection could not be generated due to an error."
        
        # Section 6: Voter Contact Plan
        progress_tracker.update(75, "processing", "Creating voter contact strategy...", 
                              "Generating section 6: Voter Contact Plan")
        
        try:
            from ai_generated_campaign_plan.sections.six_voter_contact_plan import VoterContactPlanGenerator
            contact_generator = VoterContactPlanGenerator()
            if hasattr(contact_generator, 'llm_client'):
                contact_generator.llm_client = orchestrator.llm_client
            sections[6] = await contact_generator.generate_section(cleaned_campaign_info, primary_contact_strategy, general_contact_strategy)
            logger.info("Successfully generated section 6: Voter Contact Plan")
        except Exception as e:
            logger.error(f"Failed to generate section 6: {str(e)}")
            sections[6] = "6. VOTER CONTACT PLAN\n\nSection could not be generated due to an error."
        
        # Section 3: Campaign Timeline (depends on sections 5 and 6)
        progress_tracker.update(85, "processing", "Creating campaign timeline...", 
                              "Generating section 3: Campaign Timeline")
        
        try:
            from ai_generated_campaign_plan.sections.three_campaign_timeline import CampaignTimelineGenerator
            timeline_generator = CampaignTimelineGenerator()
            if hasattr(timeline_generator, 'llm_client'):
                timeline_generator.llm_client = orchestrator.llm_client
            sections[3] = await timeline_generator.generate_section(cleaned_campaign_info, sections[5], sections[6])
            logger.info("Successfully generated section 3: Campaign Timeline")
        except Exception as e:
            logger.error(f"Failed to generate section 3: {str(e)}")
            sections[3] = "3. CAMPAIGN TIMELINE\n\nSection could not be generated due to an error."
        
        # Assemble final document
        progress_tracker.update(95, "processing", "Assembling final campaign plan document...", 
                              "Combining all sections into final document")
        
        try:
            final_plan = orchestrator._assemble_final_document(campaign_info, sections)
            logger.info(f"Successfully assembled final document ({len(final_plan)} characters)")
        except Exception as e:
            logger.error(f"Failed to assemble final document: {str(e)}")
            raise
        
        # Convert to PDF
        progress_tracker.update(98, "processing", "Converting to PDF format...", 
                              "Creating PDF document")
        
        try:
            pdf_buffer = create_pdf_from_text(final_plan, campaign_info)
            logger.info(f"Successfully created PDF ({len(pdf_buffer.getvalue())} bytes)")
        except Exception as e:
            logger.error(f"Failed to create PDF: {str(e)}")
            raise
        
        # Store final result
        safe_candidate_name = "".join(c for c in campaign_info.candidate_name if c.isalnum() or c in (' ', '-', '_')).strip()
        filename = f"campaign_plan_{safe_candidate_name.replace(' ', '_')}.pdf"
        
        # Ensure session exists in progress_store
        if progress_tracker.session_id not in progress_store:
            progress_store[progress_tracker.session_id] = {}
        
        try:
            # Store the PDF data
            pdf_data = pdf_buffer.getvalue()
            progress_store[progress_tracker.session_id]["pdf_data"] = pdf_data
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
            logger.error(f"Failed to store PDF data: {str(e)}")
            raise
        
        progress_tracker.update(100, "completed", "Campaign plan generation complete!", 
                              "PDF ready for download")
        
    except Exception as e:
        logger.error(f"Error in background generation: {str(e)}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        progress_tracker.update(0, "error", f"Error: {str(e)}", 
                              f"Generation failed: {str(e)}")

@app.get("/progress/{session_id}")
async def get_progress(session_id: str):
    """Get progress for a specific session."""
    if session_id not in progress_store:
        raise HTTPException(status_code=404, detail="Session not found")
    
    return progress_store[session_id]

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
async def download_pdf(session_id: str):
    """Download the generated PDF."""
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
    primary_date: Optional[str] = Form(None),
    additional_race_context: Optional[str] = Form(None)
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
        
        return await generate_campaign_plan_json(campaign_info)
        
    except ValidationError as e:
        logger.error(f"Validation error: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Validation error: {str(e)}")
    except Exception as e:
        logger.error(f"Error processing form: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error processing form: {str(e)}")

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
        logger.error(f"Error processing Slack webhook: {str(e)}")
        return {"response_type": "ephemeral", "text": f"Error: {str(e)}"}

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
        else:
            # Check if line has significant indentation (preserve it)
            if original_line.startswith('    ') or original_line.startswith('\t'):
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
    logger.error(f"Validation error: {str(exc)}")
    return JSONResponse(status_code=400, content={"detail": str(exc)})

@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: Exception):
    logger.error(f"Internal server error: {str(exc)}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000) 