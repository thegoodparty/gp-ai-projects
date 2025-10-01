# Monitoring & Troubleshooting Guide

## Overview

This guide covers monitoring, alerting, and troubleshooting for the Serve Messages API unified architecture. The system consists of ALB, Lambda, and DynamoDB components that each require specific monitoring approaches.

## Architecture Monitoring Points

### System Components
1. **Application Load Balancer (ALB)**: HTTPS load balancing and API key validation
2. **ALB Target Groups**: Lambda target group with health checks
3. **Unified Lambda Function**: GET/POST request processing
4. **DynamoDB Table**: Campaign message storage
5. **Route53**: DNS routing

## CloudWatch Monitoring

### Lambda Function Metrics

#### Key Metrics to Monitor

```bash
# Lambda function name
FUNCTION_NAME="serve-message-dev"

# Key metrics
- Duration: Function execution time
- Errors: Function execution errors
- Throttles: Function throttling events
- Invocations: Total invocation count
- ConcurrentExecutions: Number of parallel executions
```

#### Viewing Lambda Metrics

```bash
# Get function metrics (last 24 hours)
AWS_PROFILE=work aws cloudwatch get-metric-statistics \
    --namespace AWS/Lambda \
    --metric-name Duration \
    --dimensions Name=FunctionName,Value=serve-message-dev \
    --start-time $(date -d '24 hours ago' -u +%Y-%m-%dT%H:%M:%S) \
    --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
    --period 3600 \
    --statistics Average,Maximum \
    --region us-west-2

# Get error count
AWS_PROFILE=work aws cloudwatch get-metric-statistics \
    --namespace AWS/Lambda \
    --metric-name Errors \
    --dimensions Name=FunctionName,Value=serve-message-dev \
    --start-time $(date -d '24 hours ago' -u +%Y-%m-%dT%H:%M:%S) \
    --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
    --period 3600 \
    --statistics Sum \
    --region us-west-2
```

### DynamoDB Metrics

#### Key Metrics

```bash
# Table name
TABLE_NAME="serve-messages-dev"

# Key metrics
- ConsumedReadCapacityUnits: Read capacity consumption
- ConsumedWriteCapacityUnits: Write capacity consumption
- SuccessfulRequestLatency: Request latency
- UserErrors: Client-side errors
- SystemErrors: Server-side errors
```

#### Viewing DynamoDB Metrics

```bash
# Get read capacity consumption
AWS_PROFILE=work aws cloudwatch get-metric-statistics \
    --namespace AWS/DynamoDB \
    --metric-name ConsumedReadCapacityUnits \
    --dimensions Name=TableName,Value=serve-messages-dev \
    --start-time $(date -d '24 hours ago' -u +%Y-%m-%dT%H:%M:%S) \
    --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
    --period 3600 \
    --statistics Sum \
    --region us-west-2
```

### ALB Target Group Health

#### Key Metrics

```bash
# Check target group health
AWS_PROFILE=work aws elbv2 describe-target-health \
    --target-group-arn arn:aws:elasticloadbalancing:us-west-2:333022194791:targetgroup/serve-message-dev/<id> \
    --region us-west-2

# Key metrics from ALB
- RequestCount: Total request count
- TargetResponseTime: Lambda response time
- HTTPCode_Target_2XX_Count: Successful responses
- HTTPCode_Target_4XX_Count: Client errors
- HTTPCode_Target_5XX_Count: Server errors
- HealthyHostCount: Number of healthy Lambda targets
- UnHealthyHostCount: Number of unhealthy Lambda targets
```

## Log Monitoring

### Lambda Function Logs

#### Real-time Log Monitoring

```bash
# Monitor logs in real-time
AWS_PROFILE=work aws logs tail /aws/lambda/serve-message-dev --follow --region us-west-2

# Filter for errors only
AWS_PROFILE=work aws logs tail /aws/lambda/serve-message-dev \
    --filter-pattern "ERROR" \
    --follow \
    --region us-west-2

# Search for specific patterns
AWS_PROFILE=work aws logs filter-log-events \
    --log-group-name /aws/lambda/serve-message-dev \
    --filter-pattern "campaign_id" \
    --start-time $(date -d '1 hour ago' +%s)000 \
    --region us-west-2
```

#### Log Analysis Queries

```bash
# Find all POST requests
AWS_PROFILE=work aws logs filter-log-events \
    --log-group-name /aws/lambda/serve-message-dev \
    --filter-pattern "\"HTTP Method: POST\"" \
    --region us-west-2

# Find DynamoDB errors
AWS_PROFILE=work aws logs filter-log-events \
    --log-group-name /aws/lambda/serve-message-dev \
    --filter-pattern "dynamodb" \
    --region us-west-2

# Find API key validation errors
AWS_PROFILE=work aws logs filter-log-events \
    --log-group-name /aws/lambda/serve-message-dev \
    --filter-pattern "x-api-key" \
    --region us-west-2
```

### ALB Access Logs

ALB access logs are stored in S3:

```bash
# View recent ALB access logs
AWS_PROFILE=work aws s3 ls s3://serve-messages-alb-logs-dev-<suffix>/serve-messages-dev/ --recursive

# Download and analyze recent logs
AWS_PROFILE=work aws s3 cp s3://serve-messages-alb-logs-dev-<suffix>/serve-messages-dev/ . --recursive

# Parse for 403 errors
grep "403" *.log | less

# Parse for API key validation failures
grep "x-api-key" *.log | less
```

### ALB Metrics

```bash
# Get ALB request count
AWS_PROFILE=work aws cloudwatch get-metric-statistics \
    --namespace AWS/ApplicationELB \
    --metric-name RequestCount \
    --dimensions Name=LoadBalancer,Value=app/serve-messages-dev/<id> \
    --start-time $(date -d '1 hour ago' -u +%Y-%m-%dT%H:%M:%S) \
    --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
    --period 300 \
    --statistics Sum \
    --region us-west-2

# Get ALB target response time
AWS_PROFILE=work aws cloudwatch get-metric-statistics \
    --namespace AWS/ApplicationELB \
    --metric-name TargetResponseTime \
    --dimensions Name=LoadBalancer,Value=app/serve-messages-dev/<id> \
    --start-time $(date -d '1 hour ago' -u +%Y-%m-%dT%H:%M:%S) \
    --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
    --period 300 \
    --statistics Average \
    --region us-west-2
```

## Performance Monitoring

### Lambda Performance

#### Cold Start Monitoring

```bash
# Find cold start patterns
AWS_PROFILE=work aws logs filter-log-events \
    --log-group-name /aws/lambda/serve-message-dev \
    --filter-pattern "INIT_START" \
    --region us-west-2

# Analyze init duration
AWS_PROFILE=work aws logs filter-log-events \
    --log-group-name /aws/lambda/serve-message-dev \
    --filter-pattern "INIT_REPORT" \
    --region us-west-2
```

#### Response Time Analysis

```bash
# Find slow requests (>1000ms)
AWS_PROFILE=work aws logs filter-log-events \
    --log-group-name /aws/lambda/serve-message-dev \
    --filter-pattern "Duration: [1000-9999]" \
    --region us-west-2
```

### DynamoDB Performance

#### Query Performance

```bash
# Monitor DynamoDB latency
AWS_PROFILE=work aws cloudwatch get-metric-statistics \
    --namespace AWS/DynamoDB \
    --metric-name SuccessfulRequestLatency \
    --dimensions Name=TableName,Value=serve-messages-dev Name=Operation,Value=Query \
    --start-time $(date -d '1 hour ago' -u +%Y-%m-%dT%H:%M:%S) \
    --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
    --period 300 \
    --statistics Average,Maximum \
    --region us-west-2
```

## Error Monitoring and Alerting

### Common Error Patterns

#### 1. API Key Authentication Errors

```bash
# Find 403 Forbidden errors
curl -s -X GET "https://ai-dev.goodparty.org/serve/messages/test-campaign"
# Should return: 403 Forbidden from ALB

# Check ALB access logs for 403s
AWS_PROFILE=work aws s3 cp s3://serve-messages-alb-logs-dev-<suffix>/serve-messages-dev/ . --recursive
grep "403" *.log | less
```

**Resolution**: Verify API key in request headers:
```bash
curl -H "x-api-key: YOUR_API_KEY_HERE" \
    "https://ai-dev.goodparty.org/serve/messages/test-campaign"

# Verify ALB listener rules
AWS_PROFILE=work aws elbv2 describe-rules \
    --listener-arn <listener-arn> \
    --region us-west-2
```

#### 2. Lambda Function Errors

```bash
# Find Lambda execution errors
AWS_PROFILE=work aws logs filter-log-events \
    --log-group-name /aws/lambda/serve-message-dev \
    --filter-pattern "ERROR" \
    --region us-west-2

# Common error patterns to search for:
# - "Cannot find module"
# - "DynamoDB"
# - "TypeError"
# - "ReferenceError"
```

#### 3. DynamoDB Errors

```bash
# Find DynamoDB permission errors
AWS_PROFILE=work aws logs filter-log-events \
    --log-group-name /aws/lambda/serve-message-dev \
    --filter-pattern "AccessDeniedException" \
    --region us-west-2

# Find DynamoDB throttling
AWS_PROFILE=work aws logs filter-log-events \
    --log-group-name /aws/lambda/serve-message-dev \
    --filter-pattern "ProvisionedThroughputExceededException" \
    --region us-west-2
```

### Setting up CloudWatch Alarms

#### Lambda Error Rate Alarm

```bash
# Create alarm for Lambda errors
AWS_PROFILE=work aws cloudwatch put-metric-alarm \
    --alarm-name "serve-message-dev-errors" \
    --alarm-description "Lambda function error rate" \
    --metric-name Errors \
    --namespace AWS/Lambda \
    --statistic Sum \
    --period 300 \
    --threshold 5 \
    --comparison-operator GreaterThanThreshold \
    --dimensions Name=FunctionName,Value=serve-message-dev \
    --evaluation-periods 2 \
    --alarm-actions arn:aws:sns:us-west-2:333022194791:alerts \
    --region us-west-2
```

#### Lambda Duration Alarm

```bash
# Create alarm for slow Lambda execution
AWS_PROFILE=work aws cloudwatch put-metric-alarm \
    --alarm-name "serve-message-dev-duration" \
    --alarm-description "Lambda function duration" \
    --metric-name Duration \
    --namespace AWS/Lambda \
    --statistic Average \
    --period 300 \
    --threshold 5000 \
    --comparison-operator GreaterThanThreshold \
    --dimensions Name=FunctionName,Value=serve-message-dev \
    --evaluation-periods 2 \
    --region us-west-2
```

#### DynamoDB Error Alarm

```bash
# Create alarm for DynamoDB errors
AWS_PROFILE=work aws cloudwatch put-metric-alarm \
    --alarm-name "serve-messages-dev-errors" \
    --alarm-description "DynamoDB error rate" \
    --metric-name UserErrors \
    --namespace AWS/DynamoDB \
    --statistic Sum \
    --period 300 \
    --threshold 10 \
    --comparison-operator GreaterThanThreshold \
    --dimensions Name=TableName,Value=serve-messages-dev \
    --evaluation-periods 2 \
    --region us-west-2
```

## Troubleshooting Scenarios

### Scenario 1: API Returns 502/503 Errors

**Symptoms**: ALB returns 502 Bad Gateway or 503 Service Unavailable

**Diagnosis Steps**:
1. Check Lambda target group health:
```bash
AWS_PROFILE=work aws elbv2 describe-target-health \
    --target-group-arn <target-group-arn> \
    --region us-west-2
```

2. Check Lambda function health:
```bash
# Test Lambda health endpoint directly
curl -X GET "https://ai-dev.goodparty.org/serve/messages/health"

# Check Lambda function status
AWS_PROFILE=work aws lambda get-function \
    --function-name serve-message-dev \
    --region us-west-2
```

3. Check ALB target group attachment:
```bash
AWS_PROFILE=work aws elbv2 describe-target-groups \
    --names serve-message-dev \
    --region us-west-2
```

**Resolution**:
- Verify Lambda permission for ALB invocation
- Check target group health check configuration
- Review Lambda CloudWatch logs for errors

### Scenario 2: POST Requests Fail with 500 Errors

**Symptoms**: GET requests work, but POST returns 500 Internal Server Error

**Diagnosis Steps**:
1. Check Lambda logs for POST-specific errors:
```bash
AWS_PROFILE=work aws logs filter-log-events \
    --log-group-name /aws/lambda/serve-message-dev \
    --filter-pattern "\"HTTP Method: POST\"" \
    --region us-west-2
```

2. Test POST directly to Lambda:
```bash
curl -X POST "https://3fggdev3yb6pshpnjbzdtt7thq0izgyb.lambda-url.us-west-2.on.aws/serve/messages/test-campaign" \
    -H "x-api-key: YOUR_API_KEY_HERE" \
    -H "Content-Type: application/json" \
    -d '{"campaign_id":"test-campaign","test":"data"}'
```

3. Check DynamoDB permissions:
```bash
AWS_PROFILE=work aws iam get-role-policy \
    --role-name campaign-data-set-lambda-role-dev \
    --policy-name campaign-data-set-dynamodb-policy-dev
```

**Common Causes**:
- Missing DynamoDB PutItem permissions
- Invalid JSON in request body
- Missing Content-Type header

### Scenario 3: High Lambda Duration/Costs

**Symptoms**: Lambda execution time increases, costs rise

**Diagnosis Steps**:
1. Analyze Lambda duration trends:
```bash
AWS_PROFILE=work aws cloudwatch get-metric-statistics \
    --namespace AWS/Lambda \
    --metric-name Duration \
    --dimensions Name=FunctionName,Value=serve-message-dev \
    --start-time $(date -d '7 days ago' -u +%Y-%m-%dT%H:%M:%S) \
    --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
    --period 86400 \
    --statistics Average,Maximum \
    --region us-west-2
```

2. Check for large datasets:
```bash
# Check DynamoDB item count
AWS_PROFILE=work aws dynamodb describe-table \
    --table-name serve-messages-dev \
    --query 'Table.ItemCount' \
    --region us-west-2
```

3. Analyze memory usage:
```bash
AWS_PROFILE=work aws logs filter-log-events \
    --log-group-name /aws/lambda/serve-message-dev \
    --filter-pattern "Max Memory Used" \
    --region us-west-2
```

**Optimization**:
- Increase Lambda memory for faster execution
- Implement pagination for large result sets
- Add result caching
- Consider reserved capacity for predictable workloads

### Scenario 4: API Key Authentication Issues

**Symptoms**: Intermittent 403 Forbidden errors

**Diagnosis Steps**:
1. Check ALB listener rules:
```bash
AWS_PROFILE=work aws elbv2 describe-rules \
    --listener-arn <https-listener-arn> \
    --region us-west-2
```

2. Test API key validation:
```bash
# Valid key
curl -H "x-api-key: YOUR_API_KEY_HERE" \
    "https://ai-dev.goodparty.org/serve/messages/test-campaign"

# Invalid key
curl -H "x-api-key: invalid" \
    "https://ai-dev.goodparty.org/serve/messages/test-campaign"
```

3. Check ALB access logs for API key headers:
```bash
AWS_PROFILE=work aws s3 cp s3://serve-messages-alb-logs-dev-<suffix>/serve-messages-dev/ . --recursive
grep "x-api-key" *.log | less
```

4. Verify listener rule conditions:
```bash
# List all listener rules and their conditions
AWS_PROFILE=work aws elbv2 describe-rules \
    --listener-arn <https-listener-arn> \
    --query 'Rules[*].[Priority,Conditions]' \
    --region us-west-2
```

## Health Checks and Monitoring Scripts

### Automated Health Check Script

```bash
#!/bin/bash
# health-monitor.sh
# Automated monitoring script for Serve Messages API

ENVIRONMENT=${1:-dev}
API_KEY="YOUR_API_KEY_HERE"
SLACK_WEBHOOK=${SLACK_WEBHOOK:-""}

# Configuration based on environment
case $ENVIRONMENT in
    dev)
        BASE_URL="https://ai-dev.goodparty.org"
        FUNCTION_NAME="serve-message-dev"
        ;;
    staging)
        BASE_URL="https://ai-staging.goodparty.org"
        FUNCTION_NAME="serve-message-staging"
        ;;
    prod)
        BASE_URL="https://ai.goodparty.org"
        FUNCTION_NAME="serve-message-prod"
        ;;
esac

echo "🔍 Health check for $ENVIRONMENT environment"

# Test GET endpoint
echo "Testing GET endpoint..."
GET_RESPONSE=$(curl -s -w "%{http_code}" -X GET "$BASE_URL/serve/messages/health-check" \
    -H "x-api-key: $API_KEY" \
    -o /dev/null)

if [[ $GET_RESPONSE == "200" ]]; then
    echo "✅ GET endpoint healthy"
else
    echo "❌ GET endpoint failed: HTTP $GET_RESPONSE"
    # Send alert if webhook configured
    if [[ -n $SLACK_WEBHOOK ]]; then
        curl -X POST -H 'Content-type: application/json' \
            --data "{\"text\":\"🚨 $ENVIRONMENT GET endpoint failed: HTTP $GET_RESPONSE\"}" \
            $SLACK_WEBHOOK
    fi
fi

# Test POST endpoint
echo "Testing POST endpoint..."
POST_RESPONSE=$(curl -s -w "%{http_code}" -X POST "$BASE_URL/serve/messages/health-check" \
    -H "x-api-key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"campaign_id":"health-check","test":"automated"}' \
    -o /dev/null)

if [[ $POST_RESPONSE == "200" ]]; then
    echo "✅ POST endpoint healthy"
else
    echo "❌ POST endpoint failed: HTTP $POST_RESPONSE"
    if [[ -n $SLACK_WEBHOOK ]]; then
        curl -X POST -H 'Content-type: application/json' \
            --data "{\"text\":\"🚨 $ENVIRONMENT POST endpoint failed: HTTP $POST_RESPONSE\"}" \
            $SLACK_WEBHOOK
    fi
fi

# Check Lambda metrics
echo "Checking Lambda metrics..."
ERROR_COUNT=$(AWS_PROFILE=work aws cloudwatch get-metric-statistics \
    --namespace AWS/Lambda \
    --metric-name Errors \
    --dimensions Name=FunctionName,Value=$FUNCTION_NAME \
    --start-time $(date -d '5 minutes ago' -u +%Y-%m-%dT%H:%M:%S) \
    --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
    --period 300 \
    --statistics Sum \
    --region us-west-2 \
    --query 'Datapoints[0].Sum' \
    --output text)

if [[ $ERROR_COUNT != "None" && $ERROR_COUNT -gt 0 ]]; then
    echo "⚠️  Lambda errors detected: $ERROR_COUNT in last 5 minutes"
    if [[ -n $SLACK_WEBHOOK ]]; then
        curl -X POST -H 'Content-type: application/json' \
            --data "{\"text\":\"⚠️ $ENVIRONMENT Lambda errors: $ERROR_COUNT in last 5 minutes\"}" \
            $SLACK_WEBHOOK
    fi
else
    echo "✅ No Lambda errors in last 5 minutes"
fi

echo "Health check complete for $ENVIRONMENT"
```

### Performance Monitoring Script

```bash
#!/bin/bash
# performance-monitor.sh
# Monitor performance metrics

ENVIRONMENT=${1:-dev}
FUNCTION_NAME="serve-message-$ENVIRONMENT"

echo "📊 Performance metrics for $ENVIRONMENT"

# Average duration last hour
AVG_DURATION=$(AWS_PROFILE=work aws cloudwatch get-metric-statistics \
    --namespace AWS/Lambda \
    --metric-name Duration \
    --dimensions Name=FunctionName,Value=$FUNCTION_NAME \
    --start-time $(date -d '1 hour ago' -u +%Y-%m-%dT%H:%M:%S) \
    --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
    --period 3600 \
    --statistics Average \
    --region us-west-2 \
    --query 'Datapoints[0].Average' \
    --output text)

echo "Average duration (last hour): ${AVG_DURATION}ms"

# Invocation count last hour
INVOCATIONS=$(AWS_PROFILE=work aws cloudwatch get-metric-statistics \
    --namespace AWS/Lambda \
    --metric-name Invocations \
    --dimensions Name=FunctionName,Value=$FUNCTION_NAME \
    --start-time $(date -d '1 hour ago' -u +%Y-%m-%dT%H:%M:%S) \
    --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
    --period 3600 \
    --statistics Sum \
    --region us-west-2 \
    --query 'Datapoints[0].Sum' \
    --output text)

echo "Invocations (last hour): $INVOCATIONS"

# DynamoDB read capacity
READ_CAPACITY=$(AWS_PROFILE=work aws cloudwatch get-metric-statistics \
    --namespace AWS/DynamoDB \
    --metric-name ConsumedReadCapacityUnits \
    --dimensions Name=TableName,Value=serve-messages-$ENVIRONMENT \
    --start-time $(date -d '1 hour ago' -u +%Y-%m-%dT%H:%M:%S) \
    --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
    --period 3600 \
    --statistics Sum \
    --region us-west-2 \
    --query 'Datapoints[0].Sum' \
    --output text)

echo "DynamoDB read capacity (last hour): $READ_CAPACITY"

echo "Performance monitoring complete"
```

## Dashboard Creation

### CloudWatch Dashboard JSON

Save as `cloudwatch-dashboard-dev.json`:

```json
{
    "widgets": [
        {
            "type": "metric",
            "properties": {
                "metrics": [
                    [ "AWS/Lambda", "Duration", "FunctionName", "serve-message-dev" ],
                    [ ".", "Errors", ".", "." ],
                    [ ".", "Invocations", ".", "." ]
                ],
                "period": 300,
                "stat": "Average",
                "region": "us-west-2",
                "title": "Lambda Metrics"
            }
        },
        {
            "type": "metric",
            "properties": {
                "metrics": [
                    [ "AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", "serve-messages-dev" ],
                    [ ".", "ConsumedWriteCapacityUnits", ".", "." ]
                ],
                "period": 300,
                "stat": "Sum",
                "region": "us-west-2",
                "title": "DynamoDB Capacity"
            }
        }
    ]
}
```

Deploy dashboard:
```bash
AWS_PROFILE=work aws cloudwatch put-dashboard \
    --dashboard-name "serve-message-dev" \
    --dashboard-body file://cloudwatch-dashboard-dev.json
```

---

**Last Updated**: September 26, 2025
**Version**: v3 (ALB + Lambda Architecture)
**Monitoring Stack**: CloudWatch + ALB + Lambda + DynamoDB