# Campaign Plan Generator API

A FastAPI-based service that generates comprehensive campaign plans in PDF and JSON formats with real-time progress tracking.

## Features

- ✅ **Real-time Progress Tracking**: Server-Sent Events (SSE) for live generation updates
- ✅ **Dual Format Output**: Both PDF and structured JSON formats
- ✅ **Web Form Interface**: User-friendly HTML form for non-technical users
- ✅ **REST API**: Multiple API endpoints for different use cases
- ✅ **Professional PDF Generation**: Custom-styled PDF documents with proper formatting
- ✅ **Structured JSON Data**: Extracted tasks, timeline events, and metadata
- ✅ **Session Management**: TTL-based file storage with automatic cleanup
- ✅ **Clean Architecture**: Separated concerns with dedicated utility classes
- ✅ **Cost Tracking**: Built-in cost tracking for LLM and search API usage

## Quick Start

```bash
# Start the server
uv run ai_generated_campaign_plan/api/api_wrapper.py

# Server runs on http://localhost:8000
# Web interface available at http://localhost:8000/
```

## Core Workflow

1. **Start Generation** → Get session ID
2. **Track Progress** → Real-time SSE updates  
3. **Download Files** → PDF + JSON formats

## API Endpoints

### 1. Start Campaign Plan Generation

- **URL**: `POST /start-campaign-plan-generation`
- **Content-Type**: `application/x-www-form-urlencoded`
- **Description**: Initiates campaign plan generation and returns session ID
- **Response**: `{"session_id": "uuid-string"}`

### 2. Real-time Progress Tracking (SSE)

- **URL**: `GET /progress-stream/{session_id}`
- **Description**: Server-Sent Events stream for real-time progress updates
- **Response**: JSON events with progress, status, messages, and download links

### 3. Progress Polling (Alternative)

- **URL**: `GET /progress/{session_id}`
- **Description**: Get current progress status (alternative to SSE)
- **Response**: JSON object with current progress

### 4. Download PDF

- **URL**: `GET /download-pdf/{session_id}`
- **Description**: Download generated PDF campaign plan
- **Response**: Binary PDF file with proper filename

### 5. Download JSON

- **URL**: `GET /download-json/{session_id}`
- **Description**: Download structured JSON data with extracted tasks
- **Response**: JSON file with campaign plan data

### 6. Web Form Interface

- **URL**: `GET /`
- **Description**: Serves HTML form for non-technical users
- **Features**: Real-time progress tracking, dual downloads, countdown timer

### 7. Direct JSON API

- **URL**: `POST /generate-campaign-plan`
- **Content-Type**: `application/json`
- **Description**: Direct API for immediate PDF generation (no progress tracking)
- **Response**: PDF file download

### 8. Form Submission

- **URL**: `POST /generate-campaign-plan-form`
- **Content-Type**: `application/x-www-form-urlencoded`
- **Description**: Form-based submission for immediate PDF

### 9. Health Check

- **URL**: `GET /health`
- **Response**: `{"status": "healthy", "timestamp": "2025-08-11"}`

## Usage Examples

### With Server-Sent Events (Recommended)

```bash
# Start generation and get session ID
SESSION_ID=$(curl -s -X POST "http://localhost:8000/start-campaign-plan-generation" \
  -F "candidate_name=Sarah Johnson" \
  -F "election_date=2025-11-05" \
  -F "office_and_jurisdiction=School Board, At-Large, Chicopee, MA" \
  -F "race_type=Nonpartisan" \
  -F "incumbent_status=N/A" \
  -F "seats_available=3" \
  -F "number_of_opponents=7" \
  -F "win_number=2500" \
  -F "total_likely_voters=8500" \
  -F "available_cell_phones=1200" \
  -F "available_landlines=300" | jq -r '.session_id')

# Track progress with SSE (in browser or with curl)
curl -N "http://localhost:8000/progress-stream/$SESSION_ID"

# Download files when complete
curl -o campaign_plan.pdf "http://localhost:8000/download-pdf/$SESSION_ID"
curl -o campaign_plan.json "http://localhost:8000/download-json/$SESSION_ID"
```

### JavaScript with SSE

```javascript
// Start generation
const formData = new FormData();
formData.append('candidate_name', 'Sarah Johnson');
formData.append('election_date', '2025-11-05');
// ... other fields

const response = await fetch('/start-campaign-plan-generation', {
  method: 'POST',
  body: formData
});
const { session_id } = await response.json();

// Track progress with SSE
const eventSource = new EventSource(`/progress-stream/${session_id}`);
eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log(`${data.progress}% - ${data.message}`);
  
  if (data.status === 'completed') {
    // Download files
    window.location.href = `/download-pdf/${session_id}`;
    eventSource.close();
  }
};
```

### Direct JSON API (No Progress Tracking)

```bash
curl -X POST "http://localhost:8000/generate-campaign-plan" \
  -H "Content-Type: application/json" \
  -d '{
    "candidate_name": "Sarah Johnson",
    "election_date": "2025-11-05",
    "office_and_jurisdiction": "School Board, At-Large, Chicopee, MA",
    "incumbent_status": "N/A",
    "race_type": "Nonpartisan",
    "seats_available": 3,
    "number_of_opponents": 7,
    "win_number": 2500,
    "total_likely_voters": 8500,
    "available_cell_phones": 1200,
    "available_landlines": 300
  }' \
  --output campaign_plan.pdf
```

## Data Formats

### PDF Output
Professional campaign plan with:
- Title page with candidate information
- 6 strategic sections: Overview, Strategic Landscape, Timeline, Budget, Community Research, Voter Contact
- Proper formatting with headers, bullets, and styling
- Filename: `campaign_plan_Candidate_Name.pdf`

### JSON Output Structure
```json
{
  "campaign_info": {
    "candidate_name": "Sarah Johnson",
    "office_and_jurisdiction": "School Board, At-Large, Chicopee, MA",
    "election_date": "2025-11-05",
    "race_type": "Nonpartisan",
    "generated_date": "2025-08-11"
  },
  "sections": {
    "overview": "## 1. CAMPAIGN STRATEGY OVERVIEW\n...",
    "strategic_landscape_electoral_goals": "## 2. STRATEGIC LANDSCAPE...",
    "campaign_timeline": "## 3. CAMPAIGN TIMELINE...",
    "recommended_total_budget": "## 4. RECOMMENDED TOTAL BUDGET...",
    "know_your_community": "## 5. KNOW YOUR COMMUNITY...",
    "voter_contact_plan": "## 6. VOTER CONTACT PLAN..."
  },
  "tasks": {
    "timeline": [
      {
        "date": "August 12",
        "parsed_date": "2025-08-12",
        "title": "School Committee Meeting",
        "description": "Stay informed on current issues",
        "type": "timeline",
        "category": "campaign_timeline"
      }
    ],
    "voter_contact": [
      {
        "date": "August 18",
        "title": "P2P Text #1", 
        "description": "Voter intro + early voting alert",
        "contact_method": "text_message"
      }
    ],
    "all_tasks": [...],
    "total_count": 17
  },
  "metadata": {
    "format_version": "1.0",
    "extraction_date": "2025-08-11T12:04:54.253354",
    "sections_count": 6,
    "total_tasks": 17
  }
}
```

### Server-Sent Events Format
```json
{
  "progress": 65,
  "status": "processing",
  "message": "Generating voter contact strategy...",
  "logs": ["Starting generation", "Section 1 complete"],
  "download_links": {
    "pdf": "/download-pdf/session_id",
    "json": "/download-json/session_id"
  },
  "expires_at_formatted": "August 12, 2025 at 12:27 PM",
  "files_ready": {
    "pdf": true,
    "json": true,
    "total": 2
  }
}
```

## Session Management

- **TTL**: Files available for 24 hours after generation
- **Cleanup**: Automatic background cleanup of expired sessions and files
- **Storage**: Secure temporary file storage with proper permissions (0o600)
- **Progress Tracking**: In-memory progress store with expiration timestamps
- **Architecture**: Dedicated storage classes for PDF and JSON with metadata

## Local Development

### Prerequisites

- Python 3.11+
- `uv` package manager (recommended)

### Setup

```bash
# Navigate to project directory
cd gp-ai-projects

# Install dependencies using uv
uv sync

# Set environment variables
export GEMINI_API_KEY="your_gemini_api_key"
  
export TAVILY_API_KEY="your_tavily_api_key"

# Run development server
uv run ai_generated_campaign_plan/api/api_wrapper.py
```

The API will be available at `http://localhost:8000`

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `GEMINI_API_KEY` | Google Gemini API key (primary LLM) | ✓ |
| `TAVILY_API_KEY` | Tavily search API key | ✓ |
| `ENVIRONMENT` | Set to "development" for debug logging | |

## Architecture

### Core Components

- **API Wrapper** (`api_wrapper.py`) - FastAPI application with all endpoints
- **PDF Generator** (`pdf_generator.py`) - Dedicated PDF creation with custom styling
- **JSON Extractor** (`json_extractor.py`) - Extracts structured data and timeline tasks
- **Storage Classes** (`pdf_storage.py`, `json_storage.py`) - File management with TTL cleanup
- **Orchestrator** - Coordinates campaign plan generation across all sections

### Key Features

- **Async Processing**: Non-blocking background generation with asyncio
- **Real-time Updates**: Server-Sent Events for progress tracking without polling overhead
- **Clean Architecture**: Separated concerns with dedicated utility classes
- **Automatic Cleanup**: TTL-based session and file management with background tasks
- **Error Handling**: Graceful handling of individual section failures
- **Secure Storage**: Proper file permissions and temporary storage
- **Cost Tracking**: Built-in monitoring of LLM and search API usage

## Error Handling

Common error responses:

```json
// 400 - Validation Error
{
  "detail": "Validation error: 'INVALID' is not a valid RaceType"
}

// 404 - Session Not Found  
{
  "detail": "Session not found"
}

// 500 - Generation Failed
{
  "detail": "Generation failed: [specific error message]"
}
```

### Session States

| Status | Progress | Description |
|--------|----------|-------------|
| `starting` | 0-10% | Initializing and cleaning data |
| `processing` | 10-99% | Generating campaign sections |
| `completed` | 100% | Files ready for download |
| `error` | 0% | Generation failed |

## Cost Estimation

The API includes built-in cost tracking:

- **LLM Costs**: ~$0.10-0.30 per campaign plan (Gemini + fallback)
- **Search Costs**: ~$0.05 per plan (Tavily web searches)
- **Total**: ~$0.15-0.35 per campaign plan

## Security Considerations

- **Environment Variables**: Store API keys securely, never commit to repository
- **File Permissions**: Secure temporary file storage with restricted access (0o600)
- **Session Management**: Automatic cleanup prevents data accumulation
- **Input Validation**: Comprehensive validation of all input parameters
- **Rate Limiting**: Consider implementing rate limiting for production use

## Monitoring and Logging

- **Built-in Logging**: Comprehensive logging with colored output and file rotation
- **Cost Tracking**: Detailed tracking of LLM and search API usage costs
- **Health Check**: Use `/health` endpoint for monitoring
- **Progress Logs**: Real-time logs available through SSE progress stream
- **Error Tracking**: Detailed error messages and stack traces in logs

## Troubleshooting

### Common Issues

1. **Server-Sent Events Connection Issues**
   ```bash
   # Test SSE connection manually
   curl -N "http://localhost:8000/progress-stream/SESSION_ID"
   
   # Check browser developer tools for SSE errors
   # Ensure session ID is valid and generation is active
   ```

2. **PDF Generation Errors**
   - Check reportlab installation: `uv sync`
   - Verify text encoding and special characters
   - Check PDF generator logs for specific errors

3. **Session Not Found Errors**
   - Verify session ID is correct (36-character UUID)
   - Check if session expired (24-hour TTL)
   - Confirm generation completed successfully

4. **File Download Issues**
   - Ensure generation status is "completed"
   - Check file permissions in temporary storage
   - Verify storage cleanup hasn't removed files early

5. **Generation Timeouts or Failures**
   - Check API key validity (GEMINI_API_KEY, TAVILY_API_KEY)
   - Monitor logs for LLM provider failures
   - Review individual section generation errors

### Debug Mode

Enable debug logging:

```bash
# Set environment variable for detailed logging
export ENVIRONMENT=development

# Run with debug logging
uv run ai_generated_campaign_plan/api/api_wrapper.py
```

### Log Files

Check application logs:
```bash
# View recent logs
tail -f ai_generated_campaign_plan/api/logs/api_wrapper.log

# View error logs
tail -f ai_generated_campaign_plan/api/logs/api_wrapper_errors.log
```

### Testing Individual Components

```bash
# Test PDF generation
uv run ai_generated_campaign_plan/sections/one_overview.py

# Test JSON extraction
uv run ai_generated_campaign_plan/api/json_extractor.py

# Test complete orchestrator
uv run ai_generated_campaign_plan/orchestrator.py
```

## File Structure

```
ai_generated_campaign_plan/api/
├── api_wrapper.py              # Main FastAPI application
├── pdf_generator.py            # Dedicated PDF creation class
├── json_extractor.py           # JSON data extraction class
├── pdf_storage.py              # PDF file management with TTL
├── json_storage.py             # JSON file management with TTL
├── templates/
│   └── campaign_form.html      # HTML form with SSE integration
├── logs/                       # Application log files
├── README_API.md               # This documentation
└── API_DOCUMENTATION.md        # Complete API reference
```

## Additional Resources

- **Complete API Documentation**: See [API_DOCUMENTATION.md](./API_DOCUMENTATION.md) for detailed endpoint specifications
- **Campaign Plan Orchestrator**: Core campaign generation logic
- **Shared Utilities**: LLM client, logger, and Tavily search integration
- **Test Scripts**: `test_campaign_generator.js` for end-to-end testing

---

For detailed technical specifications, see the complete [API Documentation](./API_DOCUMENTATION.md).
