output "api_gateway_url" {
  description = "Base URL of the API Gateway"
  value       = "https://${aws_api_gateway_rest_api.campaign_data_api.id}.execute-api.${data.aws_region.current.name}.amazonaws.com/${var.environment}"
}

output "retrieve_api_key_id" {
  description = "ID of the RETRIEVE API key"
  value       = aws_api_gateway_api_key.retrieve_api_key.id
}

output "retrieve_api_key_value" {
  description = "Value of the RETRIEVE API key"
  value       = aws_api_gateway_api_key.retrieve_api_key.value
  sensitive   = true
}

output "api_gateway_id" {
  description = "ID of the API Gateway"
  value       = aws_api_gateway_rest_api.campaign_data_api.id
}

output "api_gateway_domain_name" {
  description = "Domain name of the API Gateway"
  value       = "${aws_api_gateway_rest_api.campaign_data_api.id}.execute-api.${data.aws_region.current.name}.amazonaws.com"
}

data "aws_region" "current" {}