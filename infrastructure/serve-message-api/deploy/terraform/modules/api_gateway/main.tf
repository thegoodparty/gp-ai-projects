# API Gateway REST API
resource "aws_api_gateway_rest_api" "campaign_data_api" {
  name        = "serve-message-api-${var.environment}"
  description = "API for Serve Message management (${var.environment})"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = {
    Name        = "Serve Message API"
    Environment = var.environment
  }
}

# API Gateway Resource: /serve
resource "aws_api_gateway_resource" "serve" {
  rest_api_id = aws_api_gateway_rest_api.campaign_data_api.id
  parent_id   = aws_api_gateway_rest_api.campaign_data_api.root_resource_id
  path_part   = "serve"
}

# API Gateway Resource: /serve/messages
resource "aws_api_gateway_resource" "messages" {
  rest_api_id = aws_api_gateway_rest_api.campaign_data_api.id
  parent_id   = aws_api_gateway_resource.serve.id
  path_part   = "messages"
}

# API Gateway Resource: /serve/messages/{campaign_id}
resource "aws_api_gateway_resource" "campaign_id" {
  rest_api_id = aws_api_gateway_rest_api.campaign_data_api.id
  parent_id   = aws_api_gateway_resource.messages.id
  path_part   = "{campaign_id}"
}

# POST Method for SET operation (with IAM auth)
resource "aws_api_gateway_method" "set_campaign_data" {
  rest_api_id   = aws_api_gateway_rest_api.campaign_data_api.id
  resource_id   = aws_api_gateway_resource.campaign_id.id
  http_method   = "POST"
  authorization = "AWS_IAM"
}

# GET Method for RETRIEVE operation (with API Key)
resource "aws_api_gateway_method" "get_campaign_data" {
  rest_api_id   = aws_api_gateway_rest_api.campaign_data_api.id
  resource_id   = aws_api_gateway_resource.campaign_id.id
  http_method   = "GET"
  authorization = "NONE"
  api_key_required = true
}

# Lambda Integration for SET
resource "aws_api_gateway_integration" "set_lambda_integration" {
  rest_api_id = aws_api_gateway_rest_api.campaign_data_api.id
  resource_id = aws_api_gateway_resource.campaign_id.id
  http_method = aws_api_gateway_method.set_campaign_data.http_method

  integration_http_method = "POST"
  type                   = "AWS_PROXY"
  uri                    = var.set_lambda_invoke_arn
}

# Lambda Integration for RETRIEVE
resource "aws_api_gateway_integration" "retrieve_lambda_integration" {
  rest_api_id = aws_api_gateway_rest_api.campaign_data_api.id
  resource_id = aws_api_gateway_resource.campaign_id.id
  http_method = aws_api_gateway_method.get_campaign_data.http_method

  integration_http_method = "POST"
  type                   = "AWS_PROXY"
  uri                    = var.retrieve_lambda_invoke_arn
}

# Lambda permissions for API Gateway
resource "aws_lambda_permission" "set_lambda_permission" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = var.set_lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.campaign_data_api.execution_arn}/*/*"
}

resource "aws_lambda_permission" "retrieve_lambda_permission" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = var.retrieve_lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.campaign_data_api.execution_arn}/*/*"
}

# API Gateway Deployment
resource "aws_api_gateway_deployment" "campaign_data_deployment" {
  depends_on = [
    aws_api_gateway_method.set_campaign_data,
    aws_api_gateway_method.get_campaign_data,
    aws_api_gateway_integration.set_lambda_integration,
    aws_api_gateway_integration.retrieve_lambda_integration,
  ]

  rest_api_id = aws_api_gateway_rest_api.campaign_data_api.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.campaign_id.id,
      aws_api_gateway_method.set_campaign_data.id,
      aws_api_gateway_method.get_campaign_data.id,
      aws_api_gateway_integration.set_lambda_integration.id,
      aws_api_gateway_integration.retrieve_lambda_integration.id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

# API Gateway Stage
resource "aws_api_gateway_stage" "campaign_data_stage" {
  deployment_id = aws_api_gateway_deployment.campaign_data_deployment.id
  rest_api_id   = aws_api_gateway_rest_api.campaign_data_api.id
  stage_name    = var.environment

  tags = {
    Name        = "Serve Message API Stage"
    Environment = var.environment
  }
}

# API Key for RETRIEVE endpoint
resource "aws_api_gateway_api_key" "retrieve_api_key" {
  name = "serve-message-retrieve-key-${var.environment}"
  description = "API Key for Serve Message RETRIEVE operations (${var.environment})"

  tags = {
    Name        = "Serve Message RETRIEVE API Key"
    Environment = var.environment
  }
}

# Usage Plan
resource "aws_api_gateway_usage_plan" "campaign_data_usage_plan" {
  name         = "serve-message-usage-plan-${var.environment}"
  description  = "Usage plan for Serve Message API (${var.environment})"

  api_stages {
    api_id = aws_api_gateway_rest_api.campaign_data_api.id
    stage  = aws_api_gateway_stage.campaign_data_stage.stage_name
  }

  quota_settings {
    limit  = 10000  # 10,000 requests per month
    period = "MONTH"
  }

  throttle_settings {
    rate_limit  = 100   # 100 requests per second
    burst_limit = 200   # 200 concurrent requests
  }

  tags = {
    Name        = "Serve Message Usage Plan"
    Environment = var.environment
  }
}

# Usage Plan Key (link API key to usage plan)
resource "aws_api_gateway_usage_plan_key" "campaign_data_usage_plan_key" {
  key_id        = aws_api_gateway_api_key.retrieve_api_key.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.campaign_data_usage_plan.id
}