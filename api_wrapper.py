import asyncio
import io
import json
import re
from datetime import date
from typing import Optional
from fastapi import FastAPI, HTTPException, Request, Form, File, UploadFile
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
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