# Campaign Plan Generator API

A FastAPI-based web service that wraps the campaign plan orchestrator to generate comprehensive campaign plans as downloadable PDFs or structured JSON.

## Features

- ✅ **Web Form Interface**: User-friendly HTML form for non-technical users
- ✅ **REST API**: JSON API endpoint for programmatic access
- ✅ **Multiple Output Formats**: PDF download and structured JSON response
- ✅ **Task Extraction**: Automatic parsing of campaign tasks for todo lists
- ✅ **React Integration**: JSON endpoints optimized for web app integration
- ✅ **PDF Generation**: Automatic conversion to professionally formatted PDF
- ✅ **Slack Integration**: Webhook support for Slack slash commands
- ✅ **AWS Lambda Ready**: Containerized deployment for AWS Lambda
- ✅ **Cost Tracking**: Built-in cost tracking for LLM and search API usage

## API Endpoints

### 1. Web Form (Non-Technical Users)

- **URL**: `GET /`
- **Description**: Serves an HTML form for easy campaign plan generation
- **Usage**: Open in browser, fill form, submit to get PDF download

### 2. JSON API (Flexible Format)

- **URL**: `POST /generate-campaign-plan`
- **Content-Type**: `application/json`
- **Query Parameter**: `format` (optional: "pdf" or "json", default: "pdf")
- **Body**: CampaignInfo JSON object
- **Response**: PDF file download or JSON response

### 3. JSON API (JSON Only)

- **URL**: `POST /generate-campaign-plan-json`
- **Content-Type**: `application/json`
- **Body**: CampaignInfo JSON object
- **Response**: Structured JSON with campaign plan and extracted tasks

### 4. Form Submission

- **URL**: `POST /generate-campaign-plan-form`
- **Content-Type**: `application/x-www-form-urlencoded`
- **Usage**: Internal endpoint for web form submissions

### 5. Background Generation with Progress Tracking

- **Start**: `POST /start-campaign-plan-generation`
- **Progress**: `GET /progress/{session_id}`
- **Download PDF**: `GET /download/{session_id}` or `GET /download-pdf/{session_id}`
- **Download JSON**: `GET /download/{session_id}?format=json` or `GET /download-json/{session_id}`

### 6. Slack Webhook

- **URL**: `POST /slack-webhook`
- **Description**: Handles Slack slash commands for campaign plan generation

### 7. Health Check

- **URL**: `GET /health`
- **Response**: `{"status": "healthy", "timestamp": "2024-01-01"}`

## JSON API Examples

### Generate PDF (Default)

```bash
curl -X POST "https://your-api-url/generate-campaign-plan" \
  -H "Content-Type: application/json" \
  -d '{
    "candidate_name": "Sarah Johnson",
    "election_date": "2025-11-05",
    "primary_date": "2025-09-15",
    "office_and_jurisdiction": "School Board, At-Large, Chicopee, MA",
    "incumbent_status": "N/A",
    "race_type": "Nonpartisan",
    "seats_available": 3,
    "number_of_opponents": 7,
    "win_number": 2500,
    "total_likely_voters": 8500,
    "available_cell_phones": 1200,
    "available_landlines": 300,
    "additional_race_context": "Focus on education funding"
  }' \
  --output campaign_plan.pdf
```

### Generate JSON

```bash
curl -X POST "https://your-api-url/generate-campaign-plan?format=json" \
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
  }'
```

### JSON Response Structure

The JSON response includes extracted tasks for easy integration with todo lists:

```json
{
  "campaign_info": {
    "candidate_name": "Sarah Johnson",
    "office_and_jurisdiction": "School Board, At-Large, Chicopee, MA",
    "election_date": "2025-11-05",
    "primary_date": "2025-09-15",
    "generated_date": "2025-01-10"
  },
  "sections": {
    "overview": "## 1. OVERVIEW\n\n[Markdown content...]",
    "campaign_timeline": "## 3. CAMPAIGN TIMELINE\n\n[Markdown content...]",
    "voter_contact_plan": "## 6. VOTER CONTACT PLAN\n\n[Markdown content...]"
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
      // Combined timeline and voter_contact tasks, sorted by date
    ]
  }
}
```

For detailed JSON API documentation, see [API_JSON_ENDPOINTS.md](API_JSON_ENDPOINTS.md).

## Local Development

### Prerequisites

- Python 3.11+
- pip or uv

### Setup

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
# Edit .env file and add your API keys:
# TAVILY_API_KEY=your_tavily_api_key
# GEMINI_API_KEY=your_gemini_api_key
# TOGETHER_API_KEY=your_together_api_key (optional)

# Run development server
python api_wrapper.py
```

The API will be available at `http://localhost:8000`

## AWS Lambda Deployment

### Prerequisites

- AWS CLI configured
- Docker installed
- AWS IAM role with Lambda execution permissions

### Deploy Steps

1. **Set Environment Variables**

```bash
export GEMINI_API_KEY="your_gemini_api_key"
export TOGETHER_API_KEY="your_together_api_key"
export TAVILY_API_KEY="your_tavily_api_key"
```

2. **Create IAM Role** (if not exists)

```bash
aws iam create-role --role-name lambda-execution-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": {
          "Service": "lambda.amazonaws.com"
        },
        "Action": "sts:AssumeRole"
      }
    ]
  }'

aws iam attach-role-policy \
  --role-name lambda-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

3. **Deploy with Script**

```bash
chmod +x deploy_lambda.sh
./deploy_lambda.sh
```

4. **Manual Deployment** (alternative)

```bash
# Build and push Docker image
docker build -t campaign-plan-generator -f Dockerfile.lambda .
docker tag campaign-plan-generator:latest $IMAGE_URI
docker push $IMAGE_URI

# Create Lambda function
aws lambda create-function \
  --function-name campaign-plan-generator \
  --package-type Image \
  --code ImageUri=$IMAGE_URI \
  --role arn:aws:iam::ACCOUNT_ID:role/lambda-execution-role \
  --timeout 300 \
  --memory-size 1024
```

### Lambda Configuration

- **Timeout**: 300 seconds (5 minutes)
- **Memory**: 1024 MB
- **Environment Variables**:
  - `GEMINI_API_KEY`
  - `TOGETHER_API_KEY`
  - `TAVILY_API_KEY`

## Slack Integration

### Setup Slack App

1. **Create Slack App**

   - Go to https://api.slack.com/apps
   - Create new app
   - Enable Slash Commands

2. **Configure Slash Command**

   - Command: `/campaign-plan`
   - Request URL: `https://your-api-url/slack-webhook`
   - Short Description: "Generate campaign plan"

3. **Usage Example**

```
/campaign-plan candidate:"John Smith" election:"2025-11-04" office:"City Council, Boston, MA" race_type:"Nonpartisan" incumbent_status:"N/A" seats:"1" opponents:"3" win_number:"5000" total_voters:"15000" cell_phones:"2000" landlines:"500"
```

## API Gateway Integration

After Lambda deployment, set up API Gateway:

1. **Create API Gateway**

```bash
aws apigateway create-rest-api --name campaign-plan-generator
```

2. **Configure Resources and Methods**

   - Create resource: `/{proxy+}`
   - Add method: `ANY`
   - Integration type: Lambda Proxy
   - Lambda function: `campaign-plan-generator`

3. **Deploy API**

```bash
aws apigateway create-deployment \
  --rest-api-id YOUR_API_ID \
  --stage-name prod
```

## Cost Estimation

The API includes built-in cost tracking:

- **LLM Costs**: ~$0.10-0.30 per campaign plan
- **Search Costs**: ~$0.05 per plan (Tavily searches)
- **AWS Lambda**: $0.0000002 per 100ms + $0.0000166667 per GB-second
- **Total**: ~$0.15-0.35 per campaign plan

## Security Considerations

- **Environment Variables**: Store API keys securely
- **VPC Deployment**: Deploy Lambda in VPC for internal use
- **Authentication**: Add API key or JWT authentication if needed
- **Rate Limiting**: Implement rate limiting for production use

## Monitoring

- **CloudWatch Logs**: Monitor Lambda execution logs
- **Cost Tracking**: Built-in cost reporting in application logs
- **Health Check**: Use `/health` endpoint for monitoring

## Troubleshooting

### Common Issues

1. **PDF Generation Errors**

   - Check reportlab installation
   - Verify text encoding

2. **Lambda Timeout**

   - Increase timeout to 300 seconds
   - Optimize LLM calls

3. **Memory Issues**
   - Increase Lambda memory to 1024 MB
   - Monitor memory usage

### Debug Mode

Enable debug logging:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## File Structure

```
gp-ai-campaign/
├── api_wrapper.py              # Main FastAPI application
├── lambda_handler.py           # AWS Lambda handler
├── requirements_api.txt        # Python dependencies
├── Dockerfile.lambda          # Docker configuration
├── deploy_lambda.sh           # Deployment script
├── slack_example.py           # Slack integration example
├── templates/
│   └── campaign_form.html     # HTML form template
├── ai_generated_campaign_plan/ # Core campaign plan logic
├── shared/                    # Shared utilities
└── README_API.md             # This file
```

## Next Steps

1. **Add Authentication**: Implement API key or OAuth
2. **Database Integration**: Store generated plans
3. **Advanced Slack Features**: File uploads, interactive buttons
4. **Caching**: Cache common responses
5. **Monitoring**: Add comprehensive monitoring and alerting

## Support

For issues or questions, refer to the campaign plan orchestrator documentation or create an issue in the project repository.
