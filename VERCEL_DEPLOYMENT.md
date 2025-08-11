# Vercel Deployment Guide

This guide explains how to deploy the Campaign Plan Generator API to Vercel for serverless hosting.

## Prerequisites

1. **Vercel Account**: Sign up at [vercel.com](https://vercel.com)
2. **Vercel CLI**: Install with `npm install -g vercel`
3. **Environment Variables**: You'll need API keys for:
   - OpenAI API key (`OPENAI_API_KEY`)
   - Google Gemini API key (`GOOGLE_API_KEY`)
   - Tavily API key (`TAVILY_API_KEY`)

## Deployment Steps

### 1. Install Vercel CLI

```bash
npm install -g vercel
```

### 2. Login to Vercel

```bash
vercel login
```

### 3. Deploy the Project

From the project root directory:

```bash
vercel
```

During deployment, Vercel will ask:

- **Set up and deploy?** → `y`
- **Which scope?** → Choose your account/team
- **Link to existing project?** → `n` (for new deployment)
- **Project name?** → `gp-campaign-api` (or your preferred name)
- **Directory with code?** → `./` (press Enter)
- **Override settings?** → `n`

### 4. Configure Environment Variables

After initial deployment, add your API keys:

```bash
# Add OpenAI API key
vercel env add OPENAI_API_KEY

# Add Google Gemini API key
vercel env add GOOGLE_API_KEY

# Add Tavily API key
vercel env add TAVILY_API_KEY
```

Or configure them through the Vercel dashboard:

1. Go to your project dashboard
2. Navigate to Settings → Environment Variables
3. Add each variable for Production, Preview, and Development

### 5. Redeploy with Environment Variables

```bash
vercel --prod
```

## API Endpoints

Once deployed, your API will be available at `https://your-project.vercel.app/`

All existing endpoints from your FastAPI application are available:

#### 1. Health Check

```bash
GET https://your-project.vercel.app/health
```

#### 2. Generate Campaign Plan (JSON Only)

```bash
POST https://your-project.vercel.app/generate-campaign-plan-json
Content-Type: application/json

{
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
}
```

#### 3. Generate Campaign Plan (PDF or JSON)

```bash
POST https://your-project.vercel.app/generate-campaign-plan?format=json
Content-Type: application/json

{
  "candidate_name": "Jane Smith",
  // ... same data as above
}
```

#### 4. Async Generation with Progress Tracking

```bash
POST https://your-project.vercel.app/generate-campaign-plan-async
Content-Type: application/json

{
  "candidate_name": "Jane Smith",
  // ... same data as above
  "webhook_url": "https://your-webhook-endpoint.com/webhook"
}
```

#### 5. Progress Tracking

```bash
GET https://your-project.vercel.app/progress/{session_id}
```

For a complete list of endpoints, see your `API_JSON_ENDPOINTS.md` file.

## Example Usage

### JavaScript/React Integration

```javascript
const generateCampaignPlan = async (campaignData) => {
  try {
    const response = await fetch(
      "https://your-project.vercel.app/generate-campaign-plan-json",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(campaignData),
      }
    );

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const campaignPlan = await response.json();
    return campaignPlan;
  } catch (error) {
    console.error("Error generating campaign plan:", error);
    throw error;
  }
};

// Usage example
const campaignData = {
  candidate_name: "Jane Smith",
  election_date: "2025-11-05",
  office_and_jurisdiction: "City Council, District 3, Boston, MA",
  incumbent_status: "NOT_APPLICABLE",
  race_type: "NONPARTISAN",
  seats_available: 1,
  number_of_opponents: 3,
  win_number: 8000,
  total_likely_voters: 50000,
  available_cell_phones: 5000,
  available_landlines: 500,
};

generateCampaignPlan(campaignData)
  .then((plan) => {
    console.log("Campaign plan generated:", plan);
    // Process the returned campaign plan
  })
  .catch((error) => {
    console.error("Failed to generate plan:", error);
  });
```

### cURL Testing

```bash
# Test health endpoint
curl https://your-project.vercel.app/health

# Generate campaign plan
curl -X POST https://your-project.vercel.app/generate-campaign-plan-json \
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
```

## Response Format

The API returns a structured JSON response with:

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
    "timeline": [...],
    "voter_contact": [...],
    "all_tasks": [...]
  }
}
```

## Configuration Files

The following files are configured for Vercel deployment:

- **`vercel.json`**: Vercel configuration and routing
- **`pyproject.toml`**: Python dependencies (used by Vercel)
- **`.vercelignore`**: Files to exclude from deployment
- **`api/index.py`**: Single serverless function handler that wraps FastAPI app

## Troubleshooting

### Common Issues:

1. **Environment Variables**: Ensure all required API keys are set in Vercel dashboard
2. **Function Timeout**: Campaign plan generation can take 2-5 minutes; the timeout is set to 300 seconds
3. **Memory Limits**: Set to 512MB for AI processing requirements
4. **Import Errors**: Make sure all dependencies are in `pyproject.toml` dependencies section

### View Logs:

```bash
vercel logs your-project-url
```

### Local Testing:

```bash
# Test locally with Vercel dev environment
vercel dev

# Or test the FastAPI app directly with uv
uv run api_wrapper.py
```

This starts a local development server that mimics Vercel's serverless environment.

## Monitoring

- Monitor function execution in the Vercel dashboard
- Check logs for errors and performance metrics
- Set up alerts for function failures

## Production Considerations

1. **Rate Limiting**: Consider implementing rate limiting for production use
2. **Caching**: Add caching for repeated requests with same parameters
3. **Error Handling**: Enhanced error handling and user feedback
4. **Monitoring**: Set up monitoring and alerting for production issues
5. **API Keys**: Rotate API keys regularly and use environment-specific keys
