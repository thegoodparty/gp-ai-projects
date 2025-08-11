# Campaign Plan Generator API Documentation

## Overview

The Campaign Plan Generator API is a FastAPI-based service that creates comprehensive campaign plans in both PDF and JSON formats. It uses Server-Sent Events (SSE) for real-time progress tracking and provides automatic file cleanup with TTL-based session management.

## Base URL

```
http://localhost:8000
```

## Authentication

No authentication required for the current version.

---

## Core Endpoints

### 1. Start Campaign Plan Generation

**Endpoint:** `POST /start-campaign-plan-generation`

**Description:** Initiates campaign plan generation and returns a session ID for tracking progress.

**Content-Type:** `application/x-www-form-urlencoded`

**Request Parameters:**
```
candidate_name: string (required) - Candidate's full name
election_date: string (required) - Election date in YYYY-MM-DD format
office_and_jurisdiction: string (required) - Office and location (e.g., "School Board, At-Large, Chicopee, MA")
race_type: string (required) - Either "Partisan" or "Nonpartisan"
incumbent_status: string (required) - "Elected", "Appointed", or "N/A"
seats_available: integer (required) - Number of seats up for election
number_of_opponents: integer (required) - Number of other candidates
win_number: integer (required) - Estimated votes needed to win
total_likely_voters: integer (required) - Total likely voters in jurisdiction
available_cell_phones: integer (required) - Cell phone contacts available
available_landlines: integer (required) - Landline contacts available
primary_date: string (optional) - Primary date in YYYY-MM-DD format if applicable
additional_race_context: string (optional) - Additional context about the race
```

**Response:**
```json
{
  "session_id": "087df092-4d66-48e7-a779-039f04a732c0"
}
```

**Example:**
```bash
curl -X POST "http://localhost:8000/start-campaign-plan-generation" \
  -F "candidate_name=John Smith" \
  -F "election_date=2024-11-05" \
  -F "office_and_jurisdiction=Mayor, Springfield, IL" \
  -F "race_type=Nonpartisan" \
  -F "incumbent_status=N/A" \
  -F "seats_available=1" \
  -F "number_of_opponents=2" \
  -F "win_number=5000" \
  -F "total_likely_voters=12000" \
  -F "available_cell_phones=8000" \
  -F "available_landlines=4000"
```

### 2. Track Progress (Server-Sent Events)

**Endpoint:** `GET /progress-stream/{session_id}`

**Description:** Real-time progress updates using Server-Sent Events (SSE).

**Response Format:** Server-Sent Events stream with JSON data events

**Event Data Structure:**
```json
{
  "progress": 65,
  "status": "processing",
  "message": "Researching community events and demographics...",
  "logs": [
    "Starting generation for John Smith",
    "Successfully cleaned campaign data",
    "Generating section 5: Community Research"
  ],
  "timestamp": "2025-08-11",
  "has_pdf": false,
  "has_json": false,
  "download_links": {},
  "expires_at": "2025-08-12T12:27:22.123456",
  "expires_at_formatted": "August 12, 2025 at 12:27 PM",
  "files_ready": {
    "pdf": false,
    "json": false,
    "total": 0
  }
}
```

**Status Values:**
- `starting` - Initial setup
- `processing` - Generation in progress (0-99%)
- `completed` - Generation finished (100%)
- `error` - Generation failed

**Example Usage:**
```javascript
const eventSource = new EventSource('/progress-stream/087df092-4d66-48e7-a779-039f04a732c0');

eventSource.onmessage = function(event) {
  const data = JSON.parse(event.data);
  console.log(`Progress: ${data.progress}% - ${data.message}`);
  
  if (data.status === 'completed') {
    console.log('Download links:', data.download_links);
    eventSource.close();
  }
};
```

### 3. Get Progress (Polling Alternative)

**Endpoint:** `GET /progress/{session_id}`

**Description:** Get current progress status (alternative to SSE).

**Response:**
```json
{
  "progress": 100,
  "status": "completed",
  "message": "Campaign plan generation complete!",
  "logs": ["Generation completed successfully"],
  "timestamp": "2025-08-11",
  "expires_at": "2025-08-12T12:27:22.123456",
  "expires_at_formatted": "August 12, 2025 at 12:27 PM"
}
```

### 4. Download PDF

**Endpoint:** `GET /download-pdf/{session_id}`

**Description:** Download the generated PDF campaign plan.

**Response:** Binary PDF file with proper filename in Content-Disposition header.

**Example:**
```bash
curl -o campaign_plan.pdf "http://localhost:8000/download-pdf/087df092-4d66-48e7-a779-039f04a732c0"
```

### 5. Download JSON

**Endpoint:** `GET /download-json/{session_id}`

**Description:** Download the structured JSON data.

**Response:** JSON file with campaign plan data and extracted tasks.

**Example:**
```bash
curl -o campaign_plan.json "http://localhost:8000/download-json/087df092-4d66-48e7-a779-039f04a732c0"
```

---

## File Formats

### PDF Format

The PDF contains:
- **Title Page:** Candidate information and election details
- **Section 1:** Campaign Strategy Overview
- **Section 2:** Strategic Landscape & Electoral Goals
- **Section 3:** Campaign Timeline
- **Section 4:** Recommended Total Budget
- **Section 5:** Know Your Community (events, media contacts)
- **Section 6:** Voter Contact Plan

**Features:**
- Professional formatting with custom styles
- Proper markdown rendering (headers, bullets, bold/italic)
- Candidate-specific filename: `campaign_plan_John_Smith.pdf`

### JSON Format

Structured data format containing:

```json
{
  "campaign_info": {
    "candidate_name": "John Smith",
    "office_and_jurisdiction": "Mayor, Springfield, IL",
    "election_date": "2024-11-05",
    "primary_date": null,
    "race_type": "Nonpartisan",
    "incumbent_status": "N/A",
    "seats_available": 1,
    "number_of_opponents": 2,
    "win_number": 5000,
    "total_likely_voters": 12000,
    "available_cell_phones": 8000,
    "available_landlines": 4000,
    "additional_race_context": null,
    "generated_date": "2025-08-11"
  },
  "sections": {
    "overview": "## 1. CAMPAIGN STRATEGY OVERVIEW\n\n...",
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
        "parsed_date": "2025-08-18",
        "title": "P2P Text #1",
        "description": "Voter intro + early voting alert",
        "type": "voter_contact",
        "category": "voter_outreach",
        "contact_method": "text_message"
      }
    ],
    "all_tasks": [],
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

**Task Categories:**
- `timeline` - Campaign events and milestones
- `voter_contact` - Outreach activities with contact methods

**Contact Methods:**
- `text_message` - SMS/P2P texting
- `phone_call` - Robocalls/phone banking
- `direct_mail` - Mailers/postcards
- `door_to_door` - Canvassing
- `digital_outreach` - Social media/online
- `event` - Campaign events/rallies
- `other` - Other contact types

---

## Additional Endpoints

### Web Interface

**Endpoint:** `GET /`

**Description:** Serves HTML form for non-technical users.

### Form Submission

**Endpoint:** `POST /generate-campaign-plan-form`

**Description:** Alternative form-based submission that generates PDF directly.

### JSON API (Direct)

**Endpoint:** `POST /generate-campaign-plan`

**Content-Type:** `application/json`

**Description:** Direct JSON API for immediate PDF generation (no progress tracking).

**Request Body:**
```json
{
  "candidate_name": "John Smith",
  "election_date": "2024-11-05",
  "office_and_jurisdiction": "Mayor, Springfield, IL",
  "race_type": "Nonpartisan",
  "incumbent_status": "N/A",
  "seats_available": 1,
  "number_of_opponents": 2,
  "win_number": 5000,
  "total_likely_voters": 12000,
  "available_cell_phones": 8000,
  "available_landlines": 4000
}
```

### Health Check

**Endpoint:** `GET /health`

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2025-08-11"
}
```

---

## Error Handling

### Common Error Responses

**400 Bad Request:**
```json
{
  "detail": "Validation error: 'INVALID_VALUE' is not a valid RaceType"
}
```

**404 Not Found:**
```json
{
  "detail": "Session not found"
}
```

**500 Internal Server Error:**
```json
{
  "detail": "Generation failed: [error message]"
}
```

### Session States and Errors

- **Session not found:** Session expired or invalid
- **Generation not completed:** Trying to download before completion
- **Generation failed:** Error during plan creation

---

## Session Management

### File Storage

- Files stored in temporary directories with secure permissions (0o600)
- Separate storage for PDF and JSON files
- Automatic cleanup of old files

### TTL and Expiration

- **Session TTL:** 24 hours from generation completion
- **File TTL:** 24 hours from creation
- **Background Cleanup:** Runs every hour
- **Memory Cleanup:** Expired sessions removed from progress store

### Storage Structure

```
/tmp/campaign_pdf/{session_id}/
├── campaign_plan_John_Smith.pdf
└── metadata.json

/tmp/campaign_json/{session_id}/
├── campaign_plan_John_Smith.json
└── metadata.json
```

---

## Implementation Details

### Server-Sent Events (SSE)

The API uses SSE for real-time progress tracking:

1. **Non-blocking:** SSE events don't block the server
2. **Automatic reconnection:** Clients can reconnect if connection drops
3. **Real-time updates:** Progress updates every 500ms
4. **Efficient:** Only sends updates when progress changes

### Background Processing

Campaign generation runs asynchronously:

1. **Parallel Execution:** Independent sections generated concurrently
2. **Dependency Management:** Timeline waits for community/voter contact sections
3. **Error Handling:** Individual section failures don't crash entire process
4. **Resource Tracking:** LLM token usage and costs tracked

### Architecture

- **PDF Generation:** Dedicated `CampaignPlanPDFGenerator` class
- **JSON Extraction:** Dedicated `CampaignPlanJSONExtractor` class
- **File Storage:** Separate `PDFStorage` and `JSONStorage` classes
- **Progress Tracking:** In-memory progress store with TTL cleanup

---

## Rate Limits and Quotas

Currently no rate limiting implemented. Consider implementing:
- Request rate limits per IP
- Concurrent generation limits
- Storage quotas per session

---

## Usage Examples

### Complete Workflow (Node.js)

```javascript
const http = require('http');
const fs = require('fs');

async function generateCampaignPlan() {
  // 1. Start generation
  const formData = new FormData();
  formData.append('candidate_name', 'John Smith');
  formData.append('election_date', '2024-11-05');
  // ... other fields
  
  const startResponse = await fetch('http://localhost:8000/start-campaign-plan-generation', {
    method: 'POST',
    body: formData
  });
  
  const { session_id } = await startResponse.json();
  console.log('Session started:', session_id);
  
  // 2. Track progress with SSE
  const eventSource = new EventSource(`http://localhost:8000/progress-stream/${session_id}`);
  
  eventSource.onmessage = function(event) {
    const data = JSON.parse(event.data);
    console.log(`Progress: ${data.progress}% - ${data.message}`);
    
    if (data.status === 'completed') {
      console.log('Generation complete! Download links available.');
      eventSource.close();
      downloadFiles(session_id);
    }
  };
}

async function downloadFiles(sessionId) {
  // Download PDF
  const pdfResponse = await fetch(`http://localhost:8000/download-pdf/${sessionId}`);
  const pdfBuffer = await pdfResponse.arrayBuffer();
  fs.writeFileSync('campaign_plan.pdf', Buffer.from(pdfBuffer));
  
  // Download JSON
  const jsonResponse = await fetch(`http://localhost:8000/download-json/${sessionId}`);
  const jsonData = await jsonResponse.json();
  fs.writeFileSync('campaign_plan.json', JSON.stringify(jsonData, null, 2));
  
  console.log('Files downloaded successfully!');
}
```

### Polling Alternative (Python)

```python
import requests
import time
import json

def generate_campaign_plan():
    # Start generation
    data = {
        'candidate_name': 'John Smith',
        'election_date': '2024-11-05',
        # ... other fields
    }
    
    response = requests.post('http://localhost:8000/start-campaign-plan-generation', data=data)
    session_id = response.json()['session_id']
    
    # Poll for progress
    while True:
        progress_response = requests.get(f'http://localhost:8000/progress/{session_id}')
        progress_data = progress_response.json()
        
        print(f"Progress: {progress_data['progress']}% - {progress_data['message']}")
        
        if progress_data['status'] == 'completed':
            print("Generation complete!")
            break
        elif progress_data['status'] == 'error':
            print(f"Error: {progress_data['message']}")
            return
            
        time.sleep(2)  # Poll every 2 seconds
    
    # Download files
    pdf_response = requests.get(f'http://localhost:8000/download-pdf/{session_id}')
    with open('campaign_plan.pdf', 'wb') as f:
        f.write(pdf_response.content)
    
    json_response = requests.get(f'http://localhost:8000/download-json/{session_id}')
    with open('campaign_plan.json', 'w') as f:
        json.dump(json_response.json(), f, indent=2)
```

---

## Changelog

**v1.0.0**
- Initial API release
- PDF and JSON generation
- SSE progress tracking
- TTL-based session management
- Clean architecture with dedicated utility classes