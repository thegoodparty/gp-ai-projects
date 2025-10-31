import io
import json
import asyncio
import uuid
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Form
from contextlib import asynccontextmanager
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
import uvicorn

from ai_generated_campaign_plan.orchestrator import CampaignPlanOrchestrator
from ai_generated_campaign_plan.schema.models import CampaignInfo, RaceType, IncumbentStatus
from ai_generated_campaign_plan.api.pdf_storage import PDFStorage
from ai_generated_campaign_plan.api.json_storage import JSONStorage
from ai_generated_campaign_plan.api.pdf_generator import CampaignPlanPDFGenerator
from ai_generated_campaign_plan.task_system.task_orchestrator import AITaskOrchestrator
from shared.logger import get_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle application lifecycle events."""
    # Startup
    logger.info("Starting Campaign Plan Generator API...")
    
    # Start background cleanup tasks for files and sessions
    pdf_cleanup_task = asyncio.create_task(
        pdf_storage.start_cleanup_task(cleanup_interval_hours=1, max_age_hours=24)
    )
    json_cleanup_task = asyncio.create_task(
        json_storage.start_cleanup_task(cleanup_interval_hours=1, max_age_hours=24)
    )
    session_cleanup_task_handle = asyncio.create_task(session_cleanup_task())
    logger.info("Background cleanup tasks started (PDF, JSON, sessions)")
    
    yield
    
    # Shutdown
    logger.info("Shutting down Campaign Plan Generator API...")
    pdf_cleanup_task.cancel()
    json_cleanup_task.cancel()
    session_cleanup_task_handle.cancel()
    try:
        await pdf_cleanup_task
        await json_cleanup_task
        await session_cleanup_task_handle
    except asyncio.CancelledError:
        logger.info("Background cleanup tasks cancelled")

app = FastAPI(
    title="Campaign Plan Generator API", 
    version="1.0.0",
    lifespan=lifespan
)
logger = get_logger(__name__)

# Get the directory where this script is located
current_dir = Path(__file__).parent
templates_dir = current_dir / "templates"

templates = Jinja2Templates(directory=str(templates_dir))

# Initialize storage and extraction utilities
pdf_storage = PDFStorage()
json_storage = JSONStorage()
pdf_generator = CampaignPlanPDFGenerator()
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

def cleanup_expired_sessions():
    """Remove sessions older than 24 hours from progress_store"""
    now = datetime.now()
    expired_sessions = []
    
    for session_id, data in list(progress_store.items()):
        if "expires_at" in data:
            try:
                expires_at = datetime.fromisoformat(data["expires_at"])
                if now > expires_at:
                    expired_sessions.append(session_id)
            except (ValueError, TypeError):
                # Invalid expiration date, clean it up
                expired_sessions.append(session_id)
    
    # Remove expired sessions
    for session_id in expired_sessions:
        del progress_store[session_id]
    
    return len(expired_sessions)

async def session_cleanup_task():
    """Background task to periodically clean up expired sessions"""
    while True:
        try:
            await asyncio.sleep(3600)  # Run every hour
            cleaned_count = cleanup_expired_sessions()
            if cleaned_count > 0:
                logger.info(f"Cleaned up {cleaned_count} expired sessions from memory")
        except Exception as e:
            logger.error(f"Error in session cleanup task: {str(e)}")

@app.get("/", response_class=HTMLResponse)
async def form_page(request: Request):
    """Serve the HTML form for non-technical users."""
    return templates.TemplateResponse("campaign_form.html", {"request": request})

@app.post("/generate-campaign-plan")
async def generate_campaign_plan_json(request: Request):
    """Generate campaign plan from JSON input and optionally return with AI tasks."""
    try:
        # Parse request body
        body = await request.json()
        campaign_info = CampaignInfo(**body.get("campaign_info", body))
        
        # Check if tasks are requested
        include_tasks = body.get("include_tasks", False)
        return_format = body.get("format", "pdf")  # "pdf", "json", or "both"
        
        logger.info(f"Generating campaign plan for {campaign_info.candidate_name} (include_tasks={include_tasks}, format={return_format})")
        
        # Generate campaign plan with structured sections
        orchestrator = CampaignPlanOrchestrator()
        campaign_data = await orchestrator.generate_campaign_plan_with_sections(campaign_info)
        
        campaign_plan_text = campaign_data['full_text']
        sections = campaign_data['sections']
        metadata = campaign_data['metadata']
        
        # Extract AI tasks if requested
        ai_tasks = []
        task_stats = {}
        if include_tasks:
            try:
                # Use structured sections directly (much more efficient than text parsing)
                timeline_section = sections.get(3, "")
                voter_contact_section = sections.get(6, "")
                
                if timeline_section and voter_contact_section:
                    # Initialize AI task orchestrator  
                    task_orchestrator = AITaskOrchestrator()
                    
                    # Generate tasks from sections
                    ai_tasks = await task_orchestrator.generate_tasks_from_sections(
                        timeline_section=timeline_section,
                        voter_contact_section=voter_contact_section,
                        campaign_info=campaign_info,
                        enable_template_mapping=True
                    )
                    
                    task_stats = task_orchestrator.get_task_statistics(ai_tasks)
                    logger.info(f"Successfully generated {len(ai_tasks)} AI tasks")
                else:
                    logger.warning("Timeline or voter contact sections not available for task extraction")
                    
            except Exception as e:
                logger.error(f"AI task generation failed: {str(e)}")
                # Continue without tasks
        
        # Generate JSON data structure
        json_data = {
            "campaign_plan": campaign_plan_text,
            "candidate_name": campaign_info.candidate_name,
            "election_date": str(campaign_info.election_date),
            "office_and_jurisdiction": campaign_info.office_and_jurisdiction,
            "generation_timestamp": datetime.now().isoformat()
        }
        
        # Add tasks to JSON if generated
        if ai_tasks:
            json_data["ai_tasks"] = [task.model_dump(exclude_none=True) for task in ai_tasks]
            json_data["task_metadata"] = {
                "generation_timestamp": datetime.now().isoformat(),
                "statistics": task_stats
            }
        
        # Return based on requested format
        if return_format == "json":
            return JSONResponse(json_data)
        elif return_format == "both":
            # Convert to PDF
            pdf_buffer = pdf_generator.create_pdf_from_text(campaign_plan_text, campaign_info)
            
            # Create filename
            safe_candidate_name = "".join(c for c in campaign_info.candidate_name if c.isalnum() or c in (' ', '-', '_')).strip()
            
            return JSONResponse({
                "campaign_plan_text": campaign_plan_text,
                "json_data": json_data,
                "pdf_filename": f"campaign_plan_{safe_candidate_name.replace(' ', '_')}.pdf",
                "pdf_size_bytes": len(pdf_buffer.getvalue())
            })
        else:
            # Default: Return PDF
            pdf_buffer = pdf_generator.create_pdf_from_text(campaign_plan_text, campaign_info)
            
            # Create filename
            safe_candidate_name = "".join(c for c in campaign_info.candidate_name if c.isalnum() or c in (' ', '-', '_')).strip()
            filename = f"campaign_plan_{safe_candidate_name.replace(' ', '_')}.pdf"
            
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
            sections[6] = await contact_generator.generate_section(cleaned_campaign_info)
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
            pdf_buffer = pdf_generator.create_pdf_from_text(final_plan, campaign_info)
            logger.info(f"Successfully created PDF ({len(pdf_buffer.getvalue())} bytes)")
        except Exception as e:
            logger.error(f"Failed to create PDF: {str(e)}")
            raise
        
        # Store final result
        safe_candidate_name = "".join(c for c in campaign_info.candidate_name if c.isalnum() or c in (' ', '-', '_')).strip()
        # Fallback to generic name if candidate name becomes empty after sanitization
        if not safe_candidate_name:
            safe_candidate_name = "candidate"
        filename = f"campaign_plan_{safe_candidate_name.replace(' ', '_')}.pdf"
        
        logger.info(f"Generated filename: {filename} from candidate name: '{campaign_info.candidate_name}'")
        
        # Ensure session exists in progress_store
        if progress_tracker.session_id not in progress_store:
            progress_store[progress_tracker.session_id] = {}
        
        try:
            # Save PDF to filesystem
            pdf_data = pdf_buffer.getvalue()
            pdf_path = pdf_storage.save_pdf(progress_tracker.session_id, pdf_data, filename)
            
            # Generate AI tasks from sections
            progress_tracker.update(96, "processing", "Extracting personalized campaign tasks...", 
                                  "Analyzing timeline and voter contact plan for actionable tasks")
            
            ai_tasks = []
            task_stats = {}
            task_generation_error = None
            try:
                # Initialize AI task orchestrator
                task_orchestrator = AITaskOrchestrator()
                
                # Extract sections 3 and 6 from the generated content
                timeline_section = sections.get(3, "")
                voter_contact_section = sections.get(6, "")
                
                if timeline_section and voter_contact_section:
                    # Generate tasks from sections
                    ai_tasks = await task_orchestrator.generate_tasks_from_sections(
                        timeline_section=timeline_section,
                        voter_contact_section=voter_contact_section,
                        campaign_info=campaign_info,
                        enable_template_mapping=True
                    )
                    
                    task_stats = task_orchestrator.get_task_statistics(ai_tasks)
                    logger.info(f"Successfully generated {len(ai_tasks)} AI tasks for campaign")
                else:
                    logger.warning("Timeline or voter contact sections not available for task extraction")
                    
            except Exception as e:
                logger.error(f"AI task generation failed, continuing without: {str(e)}")
                task_generation_error = str(e)
            
            # Generate JSON data structure
            progress_tracker.update(98, "processing", "Generating JSON format...", 
                                  "Creating structured JSON data with AI tasks")
            
            json_data = {
                "campaign_plan": final_plan,
                "candidate_name": campaign_info.candidate_name,
                "election_date": str(campaign_info.election_date),
                "office_and_jurisdiction": campaign_info.office_and_jurisdiction,
                "generation_timestamp": datetime.now().isoformat()
            }
            
            # Add tasks to JSON data
            if ai_tasks:
                json_data["ai_tasks"] = [task.model_dump(exclude_none=True) for task in ai_tasks]
                json_data["task_metadata"] = {
                    "generation_timestamp": datetime.now().isoformat(),
                    "statistics": task_stats
                }
            else:
                json_data["ai_tasks"] = []
                if task_generation_error:
                    json_data["task_generation_error"] = task_generation_error
            
            # Save JSON to filesystem using dedicated JSON storage
            json_filename = filename.replace('.pdf', '.json')
            json_path = json_storage.save_json(progress_tracker.session_id, json_data, json_filename)
            
            # Calculate expiration time (24 hours from now)
            expiration_time = datetime.now() + timedelta(hours=24)
            
            # Store expiration time in progress_store
            progress_store[progress_tracker.session_id]["expires_at"] = expiration_time.isoformat()
            progress_store[progress_tracker.session_id]["expires_at_formatted"] = expiration_time.strftime("%B %d, %Y at %I:%M %p")
            
            # Debug logging
            logger.info(f"Campaign plan generation completed for session {progress_tracker.session_id}")
            logger.info(f"PDF saved: {filename} ({len(pdf_data)} bytes)")
            logger.info(f"JSON saved: {json_filename}")
            logger.info(f"Files expire at: {expiration_time.strftime('%B %d, %Y at %I:%M %p')}")
            
            # Verify files were saved
            pdf_file_path = Path(pdf_path)
            json_file_path = Path(json_path)
            if pdf_file_path.exists() and json_file_path.exists():
                logger.info("✓ Both PDF and JSON files successfully saved and verified")
            else:
                logger.error(f"✗ File verification failed - PDF exists: {pdf_file_path.exists()}, JSON exists: {json_file_path.exists()}")
                raise Exception("File save verification failed")
            
        except Exception as e:
            logger.error(f"Failed to save files: {str(e)}")
            raise
        
        progress_tracker.update(100, "completed", "Campaign plan generation complete!", 
                              f"Generated campaign plan with {len(ai_tasks)} personalized tasks - PDF and JSON ready for download")
        
    except Exception as e:
        logger.error(f"Error in background generation: {str(e)}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        progress_tracker.update(0, "error", f"Error: {str(e)}", 
                              f"Generation failed: {str(e)}")

@app.get("/progress/{session_id}")
async def get_progress(session_id: str):
    """Get progress for a specific session."""
    # First check if session is in memory (active generation)
    if session_id in progress_store:
        return progress_store[session_id]
    
    # Check if completed files exist on disk
    has_pdf = pdf_storage.get_pdf_path(session_id) is not None
    has_json = json_storage.get_json_path(session_id) is not None
    
    if has_pdf and has_json:
        # Session completed, build response from file metadata
        pdf_metadata = pdf_storage.get_metadata(session_id)
        if pdf_metadata:
            created_at = datetime.fromisoformat(pdf_metadata["created_at"])
            expiration_time = created_at + timedelta(hours=24)
            
            # Check if expired
            if datetime.now() > expiration_time:
                raise HTTPException(status_code=404, detail="Session expired")
            
            return {
                "progress": 100,
                "status": "completed",
                "message": "Campaign plan generation complete!",
                "logs": ["Files available for download"],
                "timestamp": created_at.date().isoformat(),
                "has_pdf": True,
                "has_json": True,
                "expires_at": expiration_time.isoformat(),
                "expires_at_formatted": expiration_time.strftime("%B %d, %Y at %I:%M %p"),
                "download_links": {
                    "pdf": f"/download-pdf/{session_id}",
                    "json": f"/download-json/{session_id}"
                },
                "files_ready": {
                    "pdf": True,
                    "json": True,
                    "total": 2
                }
            }
    
    raise HTTPException(status_code=404, detail="Session not found")

@app.get("/progress-stream/{session_id}")
async def progress_stream(session_id: str):
    """Stream progress updates using Server-Sent Events."""
    
    async def event_generator():
        last_progress = -1
        while True:
            if session_id in progress_store:
                # Active generation - stream progress updates
                current_data = progress_store[session_id]
                current_progress = current_data.get("progress", 0)
                
                # Only send update if progress changed
                if current_progress != last_progress:
                    # Check if files exist using storage systems
                    has_pdf = pdf_storage.get_pdf_path(session_id) is not None
                    has_json = json_storage.get_json_path(session_id) is not None
                    
                    # Build download links if generation is complete
                    download_links = {}
                    if current_data.get("status") == "completed":
                        if has_pdf:
                            download_links["pdf"] = f"/download-pdf/{session_id}"
                        if has_json:
                            download_links["json"] = f"/download-json/{session_id}"
                    
                    filtered_data = {
                        "progress": current_data.get("progress", 0),
                        "status": current_data.get("status", "unknown"),
                        "message": current_data.get("message", ""),
                        "logs": current_data.get("logs", []),
                        "timestamp": current_data.get("timestamp", ""),
                        "has_pdf": has_pdf,
                        "has_json": has_json,
                        "download_links": download_links,
                        "expires_at": current_data.get("expires_at"),
                        "expires_at_formatted": current_data.get("expires_at_formatted"),
                        "files_ready": {
                            "pdf": has_pdf,
                            "json": has_json,
                            "total": sum([has_pdf, has_json])
                        }
                    }
                    
                    yield f"data: {json.dumps(filtered_data)}\n\n"
                    last_progress = current_progress
                
                # Stop streaming if completed or error
                if current_data.get("status") in ["completed", "error"]:
                    break
            else:
                # Check if completed files exist on disk
                has_pdf = pdf_storage.get_pdf_path(session_id) is not None
                has_json = json_storage.get_json_path(session_id) is not None
                
                if has_pdf and has_json:
                    # Session completed, send final status
                    final_data = {
                        "progress": 100,
                        "status": "completed",
                        "message": "Campaign plan generation complete!",
                        "logs": ["Files available for download"],
                        "has_pdf": True,
                        "has_json": True,
                        "download_links": {
                            "pdf": f"/download-pdf/{session_id}",
                            "json": f"/download-json/{session_id}"
                        },
                        "files_ready": {
                            "pdf": True,
                            "json": True,
                            "total": 2
                        }
                    }
                    yield f"data: {json.dumps(final_data)}\n\n"
                    break
                else:
                    # Session not found
                    error_data = {"error": "Session not found"}
                    yield f"data: {json.dumps(error_data)}\n\n"
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

@app.get("/download-pdf/{session_id}")
async def download_pdf(session_id: str):
    """Download the generated PDF."""
    logger.info(f"PDF download requested for session: {session_id}")
    
    # Check if PDF file exists directly
    pdf_path = pdf_storage.get_pdf_path(session_id)
    if not pdf_path or not pdf_path.exists():
        logger.error(f"PDF file not found for session {session_id}")
        raise HTTPException(status_code=404, detail="PDF file not found")
    
    # Get filename from metadata
    metadata = pdf_storage.get_metadata(session_id)
    if metadata and metadata.get("original_filename"):
        filename = metadata["original_filename"]
    else:
        filename = "campaign_plan.pdf"
    
    logger.info(f"Serving PDF: {filename}, path: {pdf_path}")
    
    # Stream file from disk
    def file_streamer():
        with open(pdf_path, "rb") as f:
            while chunk := f.read(8192):
                yield chunk
    
    return StreamingResponse(
        file_streamer(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.get("/download-json/{session_id}")
async def download_json(session_id: str):
    """Download the generated JSON."""
    logger.info(f"JSON download requested for session: {session_id}")
    
    # Load JSON data from filesystem directly
    json_data = json_storage.load_json(session_id)
    if not json_data:
        logger.error(f"JSON file not found for session {session_id}")
        raise HTTPException(status_code=404, detail="JSON file not found")
    
    # Get filename from metadata
    metadata = json_storage.get_metadata(session_id)
    filename = (metadata.get("original_filename") if metadata else "campaign_plan.json")
    
    logger.info(f"Serving JSON: {filename}")
    
    # Create JSON string for download
    json_str = json.dumps(json_data, indent=2, ensure_ascii=False)
    json_bytes = json_str.encode('utf-8')
    
    return StreamingResponse(
        io.BytesIO(json_bytes),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )






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