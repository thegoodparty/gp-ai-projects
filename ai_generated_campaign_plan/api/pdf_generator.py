import re
import io
from typing import List

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib.colors import black, blue

from ai_generated_campaign_plan.schema.models import CampaignInfo
from shared.logger import get_logger

logger = get_logger(__name__)

class CampaignPlanPDFGenerator:
    """Generates PDF documents from campaign plan text."""
    
    def __init__(self):
        self.styles = getSampleStyleSheet()
        self._setup_custom_styles()
    
    def _setup_custom_styles(self):
        """Create custom styles for PDF formatting."""
        self.title_style = ParagraphStyle(
            'CustomTitle',
            parent=self.styles['Heading1'],
            fontSize=20,
            spaceAfter=30,
            alignment=TA_CENTER,
            textColor=black
        )
        
        self.h1_style = ParagraphStyle(
            'CustomH1',
            parent=self.styles['Heading1'],
            fontSize=16,
            spaceAfter=15,
            spaceBefore=20,
            textColor=black
        )
        
        self.h2_style = ParagraphStyle(
            'CustomH2',
            parent=self.styles['Heading2'],
            fontSize=14,
            spaceAfter=12,
            spaceBefore=18,
            textColor=black
        )
        
        self.h3_style = ParagraphStyle(
            'CustomH3',
            parent=self.styles['Heading3'],
            fontSize=12,
            spaceAfter=10,
            spaceBefore=15,
            textColor=black
        )
        
        self.normal_style = ParagraphStyle(
            'CustomNormal',
            parent=self.styles['Normal'],
            fontSize=10,
            spaceAfter=6,
            alignment=TA_LEFT
        )
        
        self.bullet_style = ParagraphStyle(
            'CustomBullet',
            parent=self.styles['Normal'],
            fontSize=10,
            spaceAfter=3,
            leftIndent=20,
            alignment=TA_LEFT
        )
    
    def create_pdf_from_text(self, text: str, campaign_info: CampaignInfo) -> io.BytesIO:
        """Convert campaign plan text to PDF with proper markdown formatting."""
        logger.info(f"Creating PDF for {campaign_info.candidate_name}")
        
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer, 
            pagesize=letter, 
            rightMargin=72, 
            leftMargin=72, 
            topMargin=72, 
            bottomMargin=18
        )
        
        story = []
        
        # Process the main content (includes its own title page)
        story.extend(self._parse_markdown_content(text))
        
        # Build the PDF
        doc.build(story)
        buffer.seek(0)
        
        logger.info(f"PDF generated successfully ({len(buffer.getvalue())} bytes)")
        return buffer
    
    
    def _parse_markdown_content(self, text: str) -> List:
        """Parse markdown content and convert to PDF elements."""
        elements = []
        lines = text.split('\n')
        current_paragraph = []
        
        for line in lines:
            line = line.strip()
            
            if not line:
                # Empty line - finish current paragraph if exists
                if current_paragraph:
                    para_text = ' '.join(current_paragraph)
                    para_text = self._escape_text(para_text)
                    elements.append(Paragraph(para_text, self.normal_style))
                    current_paragraph = []
                elements.append(Spacer(1, 6))
                continue
            
            # Headers and special formatting
            if line.startswith('CAMPAIGN PLAN'):
                # Main title
                elements.append(Paragraph(self._escape_text(line), self.title_style))
            elif line.startswith('═'):
                # Separator lines
                elements.append(Spacer(1, 12))
            elif line.startswith('## '):
                # Finish current paragraph
                if current_paragraph:
                    para_text = ' '.join(current_paragraph)
                    para_text = self._escape_text(para_text)
                    elements.append(Paragraph(para_text, self.normal_style))
                    current_paragraph = []
                
                header_text = self._escape_text(line[3:].strip())
                elements.append(Paragraph(header_text, self.h1_style))
                
            elif line.startswith('### '):
                if current_paragraph:
                    para_text = ' '.join(current_paragraph)
                    para_text = self._escape_text(para_text)
                    elements.append(Paragraph(para_text, self.normal_style))
                    current_paragraph = []
                
                header_text = self._escape_text(line[4:].strip())
                elements.append(Paragraph(header_text, self.h2_style))
                
            elif line.startswith('#### '):
                if current_paragraph:
                    para_text = ' '.join(current_paragraph)
                    para_text = self._escape_text(para_text)
                    elements.append(Paragraph(para_text, self.normal_style))
                    current_paragraph = []
                
                header_text = self._escape_text(line[5:].strip())
                elements.append(Paragraph(header_text, self.h3_style))
                
            # Bullet points
            elif line.startswith('- ') or line.startswith('* '):
                if current_paragraph:
                    para_text = ' '.join(current_paragraph)
                    para_text = self._escape_text(para_text)
                    elements.append(Paragraph(para_text, self.normal_style))
                    current_paragraph = []
                
                bullet_text = self._escape_text(line[2:].strip())
                elements.append(Paragraph(f"• {bullet_text}", self.bullet_style))
                
            else:
                # Regular text - add to current paragraph
                current_paragraph.append(line)
        
        # Finish any remaining paragraph
        if current_paragraph:
            para_text = ' '.join(current_paragraph)
            para_text = self._escape_text(para_text)
            elements.append(Paragraph(para_text, self.normal_style))
        
        return elements
    
    def _escape_text(self, text: str) -> str:
        """Escape special characters for ReportLab."""
        # Handle bold markdown
        text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
        
        # Escape XML special characters
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        
        # Restore bold/italic tags
        text = text.replace('&lt;b&gt;', '<b>')
        text = text.replace('&lt;/b&gt;', '</b>')
        text = text.replace('&lt;i&gt;', '<i>')
        text = text.replace('&lt;/i&gt;', '</i>')
        
        return text