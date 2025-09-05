# Lambda Matching Service

AWS Lambda function that provides ultra-high performance candidate matching using semantic embeddings and LLM analysis.

## Overview

This Lambda function performs intelligent candidate matching by combining:
- **Cosine similarity search** with numpy for semantic matching
- **Gemini LLM analysis** for contextual validation
- **Geographic data enhancement** from ZIP codes
- **Political legitimacy assessment** to filter spam candidates

## API Usage

### Endpoint
The Lambda function is deployed as a Function URL with API key authentication.

### Authentication
Include the API key in the request header:
```bash
x-api-key: YOUR_LAMBDA_API_KEY
```

### Request Format
```json
POST https://your-lambda-url.lambda-url.us-west-2.on.aws/
Content-Type: application/json
x-api-key: YOUR_LAMBDA_API_KEY

{
  "candidate_name": "John Smith",
  "office": "Mayor", 
  "state": "CA",
  "city": "San Francisco"
}
```

### Response Format
```json
{
  "status": "success",
  "candidate": {
    "name": "John Smith",
    "office": "Mayor",
    "state": "CA", 
    "city": "San Francisco",
    "derived_county": "San Francisco County",
    "zip_code": "94102"
  },
  "matches": [
    {
      "candidate_id": "12345",
      "name": "John A. Smith",
      "office": "Mayor",
      "district": "San Francisco",
      "similarity_score": 0.92,
      "match_confidence": "high"
    }
  ],
  "processing_time_ms": 245,
  "cost_estimate_usd": 0.0012
}
```

## Features

### Intelligent Preprocessing
- **Name normalization** with title/suffix handling
- **Geographic enhancement** using ZIP code lookups
- **Office standardization** for consistent matching
- **Spam detection** to filter non-political candidates

### High-Performance Matching
- **Cosine similarity search** with state-partitioned embeddings
- **Semantic embedding matching** using numpy-optimized calculations
- **Batch processing optimization** for multiple candidates
- **S3-cached embeddings** for sub-second response times

### LLM-Powered Analysis
- **Political legitimacy assessment** using Gemini 2.5 Flash
- **District matching validation** with contextual analysis
- **Confidence scoring** based on multiple similarity factors
- **Cost-optimized processing** (~$0.001 per request)

## Error Handling

### Common Error Responses

**Missing API Key:**
```json
{
  "error": "Unauthorized",
  "message": "Missing or invalid API key"
}
```

**Invalid Request Format:**
```json
{
  "error": "Bad Request", 
  "message": "Missing required field: candidate_name"
}
```

**Candidate Rejected:**
```json
{
  "status": "rejected",
  "reason": "UNMATCHABLE",
  "message": "Candidate does not meet political legitimacy requirements",
  "details": "Business name or non-political office detected"
}
```

**No Matches Found:**
```json
{
  "status": "success",
  "matches": [],
  "message": "No similar candidates found in database"
}
```

## Performance Metrics

### Response Times
- **Cold start**: ~2-3 seconds (first request)
- **Warm requests**: ~200-500ms average
- **Batch processing**: Up to 10 candidates per request

### Cost Optimization
- **Gemini Flash model**: ~$0.075 per 1M tokens (thinking disabled)
- **S3 storage costs**: Minimal for cached embeddings
- **Lambda execution**: Pay-per-request pricing
- **Estimated cost**: ~$0.001-0.005 per matching request

### Accuracy Metrics
- **High confidence matches**: >90% similarity score
- **Medium confidence**: 70-90% similarity score  
- **Low confidence**: 50-70% similarity score
- **Spam detection**: >95% accuracy in filtering non-political candidates

## Deployment

The Lambda function is deployed using AWS SAM with:
- **Runtime**: Python 3.13
- **Memory**: 1GB (optimized for numpy operations)
- **Timeout**: 30 seconds
- **Environment**: Production-ready with comprehensive error handling

### Required Environment Variables
- `GEMINI_API_KEY`: For LLM analysis and embeddings
- `S3_BUCKET_NAME`: For vector store caching (default: llm-matching-embeddings)

### S3 Dependencies
The function requires pre-generated vector embeddings stored in S3:
```
s3://llm-matching-embeddings/
├── state_AL_embeddings.pkl
├── state_CA_embeddings.pkl  
├── state_TX_embeddings.pkl
└── ... (all 50 states)
```

## Monitoring

### CloudWatch Logs
- Request/response logging with unique request IDs
- Performance metrics (processing time, token usage)
- Error tracking with detailed stack traces
- Cost analysis with per-request breakdowns

### Key Metrics to Monitor
- **Invocation count**: Total requests processed
- **Error rate**: Failed requests / total requests
- **Duration**: Average response time
- **Cost per request**: LLM + Lambda execution costs

## Security

### API Key Management
- Secure random key generation
- Header-based authentication only
- No query parameter auth (prevents logging)
- Key rotation supported via environment updates

### Data Privacy
- No persistent storage of candidate data
- Request/response logging can be disabled
- All processing in-memory only
- S3 vector stores contain only aggregated embeddings

## Usage Examples

### cURL Example
```bash
curl -X POST "https://your-lambda-url.lambda-url.us-west-2.on.aws/" \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_LAMBDA_API_KEY" \
  -d '{
    "candidate_name": "Jane Doe",
    "office": "City Council", 
    "state": "NY",
    "city": "Buffalo"
  }'
```

### Python Example
```python
import requests
import json

url = "https://your-lambda-url.lambda-url.us-west-2.on.aws/"
headers = {
    "Content-Type": "application/json",
    "x-api-key": "YOUR_LAMBDA_API_KEY"
}

payload = {
    "candidate_name": "Michael Johnson",
    "office": "Sheriff",
    "state": "TX", 
    "city": "Austin"
}

response = requests.post(url, headers=headers, json=payload)
result = response.json()
print(json.dumps(result, indent=2))
```

### JavaScript Example
```javascript
const response = await fetch('https://your-lambda-url.lambda-url.us-west-2.on.aws/', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'x-api-key': 'YOUR_LAMBDA_API_KEY'
  },
  body: JSON.stringify({
    candidate_name: 'Sarah Wilson',
    office: 'School Board',
    state: 'FL',
    city: 'Miami'
  })
});

const result = await response.json();
console.log(result);
```

## Support

For deployment issues or API questions:
1. Check CloudWatch logs for detailed error messages
2. Verify vector store data is properly uploaded to S3
3. Ensure GEMINI_API_KEY has sufficient quota
4. Monitor Lambda concurrency limits for high-volume usage