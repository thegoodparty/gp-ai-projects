# ALB Outputs
output "alb_dns_name" {
  description = "DNS name of the Application Load Balancer"
  value       = module.alb.alb_dns_name
}

output "alb_zone_id" {
  description = "Zone ID of the Application Load Balancer"
  value       = module.alb.alb_zone_id
}

output "alb_arn" {
  description = "ARN of the Application Load Balancer"
  value       = module.alb.alb_arn
}

output "target_group_arn" {
  description = "ARN of the Lambda target group"
  value       = module.alb.target_group_arn
}

output "https_listener_arn" {
  description = "ARN of the HTTPS listener for adding additional rules"
  value       = module.alb.https_listener_arn
}

output "custom_domain_url" {
  description = "Custom domain URL"
  value       = "https://${var.custom_domain_name}"
}

output "serve_messages_api_url" {
  description = "Full URL for the serve messages API endpoint"
  value       = "https://${var.custom_domain_name}/serve/messages"
}

# Route53 Outputs
output "custom_domain_fqdn" {
  description = "Fully qualified domain name for the custom domain"
  value       = module.route53.a_record_fqdn
}