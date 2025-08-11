# Campaign Plan Generator - JSON API Endpoints

This document describes the new JSON endpoints added to the Campaign Plan Generator API for React webapp integration.

## Overview

The API now supports both PDF and JSON output formats, making it easy to integrate with web applications while maintaining backward compatibility.

## New Endpoints

### 1. Generate Campaign Plan (Flexible Format)

**Endpoint:** `POST /generate-campaign-plan`  
**Query Parameter:** `format` (optional, default: "pdf")

Generate a campaign plan and return it in the specified format.

```bash
# Generate JSON format
curl -X POST "http://localhost:8000/generate-campaign-plan?format=json" \
  -H "Content-Type: application/json" \
  -d '{
    "candidate_name": "Jane Smith",
    "election_date": "2025-11-05",
    "office_and_jurisdiction": "City Council, District 3, Boston, MA",
    "incumbent_status": "NOT_APPLICABLE",
    "race_type": "NONPARTISAN",
    "seats_available": 1,
    "number_of_opponents": 3,
    "win_number": 8000,
    "total_likely_voters": 50000,
    "available_cell_phones": 5000,
    "available_landlines": 500
  }'

# Generate PDF format (default)
curl -X POST "http://localhost:8000/generate-campaign-plan" \
  -H "Content-Type: application/json" \
  -d '{...same JSON data...}'
```

### 2. Generate Campaign Plan JSON Only

**Endpoint:** `POST /generate-campaign-plan-json`

Dedicated endpoint that always returns JSON format.

```bash
curl -X POST "http://localhost:8000/generate-campaign-plan-json" \
  -H "Content-Type: application/json" \
  -d '{...campaign data...}'
```

### 3. Async Generation with Webhook

**Endpoint:** `POST /generate-campaign-plan-async`  
**Content-Type:** `application/json`  
**Body:** CampaignInfo JSON object + optional webhook_url

Start campaign plan generation in background with optional webhook callback.

```bash
curl -X POST "http://localhost:8000/generate-campaign-plan-async" \
  -H "Content-Type: application/json" \
  -d '{
    "candidate_name": "Jane Smith",
    "election_date": "2025-11-05",
    "office_and_jurisdiction": "City Council, District 3, Boston, MA",
    "incumbent_status": "NOT_APPLICABLE",
    "race_type": "NONPARTISAN",
    "seats_available": 1,
    "number_of_opponents": 3,
    "win_number": 8000,
    "total_likely_voters": 50000,
    "available_cell_phones": 5000,
    "available_landlines": 500,
    "webhook_url": "https://your-api.com/campaign-plan-webhook"
  }'
```

**Response:**

```json
{
  "session_id": "abc123",
  "status": "processing",
  "progress_url": "/progress/abc123",
  "download_url": "/download/abc123",
  "download_json_url": "/download/abc123?format=json",
  "webhook_url": "https://your-api.com/campaign-plan-webhook"
}
```

**Webhook Payload:**
When generation completes (or fails), your webhook URL will receive:

```json
{
  "session_id": "abc123",
  "status": "completed", // or "error"
  "timestamp": "2025-01-10T15:30:00",
  "data": {
    // Full campaign plan JSON response (on success)
    // or {"error": "error message"} (on failure)
  }
}
```

### 4. Download with Format Support

**Endpoint:** `GET /download/{session_id}`  
**Query Parameter:** `format` (optional, default: "pdf")

Download generated content from background generation in specified format.

```bash
# Download JSON
curl "http://localhost:8000/download/{session_id}?format=json"

# Download PDF (default)
curl "http://localhost:8000/download/{session_id}"
```

### 4. Convenience Download Endpoints

**PDF Download:** `GET /download-pdf/{session_id}`  
**JSON Download:** `GET /download-json/{session_id}`

```bash
curl "http://localhost:8000/download-pdf/{session_id}"
curl "http://localhost:8000/download-json/{session_id}"
```

**Download Options:**

- **PDF**: `GET /download/{session_id}` or `GET /download/{session_id}?format=pdf`
- **JSON**: `GET /download/{session_id}?format=json` or `GET /download-json/{session_id}`

## JSON Response Format

The JSON response includes structured data with tasks extracted from sections 3 and 6:

```json
{
  "campaign_info": {
    "candidate_name": "Jane Smith",
    "office_and_jurisdiction": "City Council, District 3, Boston, MA",
    "election_date": "2025-11-05",
    "primary_date": null,
    "generated_date": "2025-01-10"
  },
  "sections": {
    "overview": "## 1. OVERVIEW\n\n[Markdown content...]",
    "strategic_landscape_electoral_goals": "## 2. STRATEGIC LANDSCAPE...",
    "campaign_timeline": "## 3. CAMPAIGN TIMELINE...",
    "recommended_total_budget": "## 4. RECOMMENDED TOTAL BUDGET...",
    "know_your_community": "## 5. KNOW YOUR COMMUNITY...",
    "voter_contact_plan": "## 6. VOTER CONTACT PLAN..."
  },
  "tasks": {
    "timeline": [
      {
        "date": "July 15",
        "parsed_date": "2025-07-15",
        "title": "Campaign Launch Event",
        "description": "Official campaign announcement",
        "type": "timeline"
      }
    ],
    "voter_contact": [
      {
        "date": "JULY 15",
        "parsed_date": "2025-07-15",
        "title": "P2P Text #1",
        "description": "Candidate intro and vote-by-mail awareness",
        "type": "voter_contact"
      }
    ],
    "all_tasks": [
      // Combined timeline and voter_contact tasks
    ]
  }
}
```

## Task Object Structure

Each task object contains:

- `date`: Original date string from the content
- `parsed_date`: ISO format date (YYYY-MM-DD) or null if parsing failed
- `title`: Task title/event name
- `description`: Task description/purpose
- `type`: "timeline" or "voter_contact"

## Usage in React

### Fetch Campaign Plan as JSON

```javascript
const generateCampaignPlan = async (campaignData) => {
  try {
    const response = await fetch("/generate-campaign-plan?format=json", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(campaignData),
    });

    const data = await response.json();
    return data;
  } catch (error) {
    console.error("Error generating campaign plan:", error);
    throw error;
  }
};
```

### Convert Tasks to Todo List

```javascript
const convertTasksToTodos = (tasks) => {
  return tasks.all_tasks.map((task) => ({
    id: `${task.type}-${task.date}`,
    title: task.title,
    description: task.description,
    date: task.parsed_date,
    completed: false,
    category: task.type === "timeline" ? "Campaign Event" : "Voter Contact",
  }));
};
```

### Background Generation with Progress

```javascript
const generateWithProgress = async (campaignData) => {
  // Start generation
  const startResponse = await fetch("/start-campaign-plan-generation", {
    method: "POST",
    body: new FormData(campaignData), // or convert to FormData
  });

  const { session_id } = await startResponse.json();

  // Poll for progress
  const pollProgress = async () => {
    const response = await fetch(`/progress/${session_id}`);
    const progress = await response.json();

    if (progress.status === "completed") {
      // Download JSON
      const jsonResponse = await fetch(`/download-json/${session_id}`);
      return await jsonResponse.json();
    } else if (progress.status === "error") {
      throw new Error(progress.message);
    }

    // Continue polling
    setTimeout(pollProgress, 1000);
  };

  return pollProgress();
};
```

### Server-to-Server Usage (Recommended)

For server-to-server calls where timeout is a concern, use the async endpoint:

```javascript
// Your API endpoint that generates campaign plans
app.post("/api/generate-campaign", async (req, res) => {
  try {
    // Start async generation with webhook
    const response = await fetch(
      "http://campaign-api:8000/generate-campaign-plan-async",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...req.body,
          webhook_url: "https://your-api.com/webhooks/campaign-complete",
        }),
      }
    );

    const { session_id } = await response.json();

    // Return immediately - don't wait for completion
    res.json({
      session_id,
      status: "processing",
      message:
        "Campaign plan generation started. You will receive a webhook when complete.",
    });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

// Webhook endpoint to receive completion notification
app.post("/webhooks/campaign-complete", (req, res) => {
  const { session_id, status, data } = req.body;

  if (status === "completed") {
    // Store the campaign plan or notify user
    console.log(`Campaign plan ${session_id} completed!`);
    // data contains the full campaign plan JSON
  } else if (status === "error") {
    console.error(`Campaign plan ${session_id} failed:`, data.error);
  }

  res.status(200).send("OK");
});
```

## Error Handling

All endpoints return standard HTTP status codes:

- `200`: Success
- `400`: Bad request (validation error)
- `404`: Session not found
- `500`: Internal server error

Error responses include a `detail` field with the error message:

```json
{
  "detail": "Error generating campaign plan: [specific error]"
}
```

## Backward Compatibility

All existing endpoints remain unchanged:

- `/generate-campaign-plan` without format parameter still returns PDF
- All form-based endpoints continue to work as before
- Progress tracking and download functionality is enhanced, not replaced

## Testing

Use the provided test script to verify functionality:

```bash
python test_json_endpoints.py
```

This will test all new endpoints and verify the JSON structure.
