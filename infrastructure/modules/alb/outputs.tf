output "alb_dns_name" {
  description = "DNS name of the load balancer"
  value       = aws_lb.serve_messages_alb.dns_name
}

output "alb_zone_id" {
  description = "Zone ID of the load balancer for Route53 alias records"
  value       = aws_lb.serve_messages_alb.zone_id
}

output "alb_arn" {
  description = "ARN of the load balancer"
  value       = aws_lb.serve_messages_alb.arn
}

output "target_group_arn" {
  description = "ARN of the Lambda target group"
  value       = aws_lb_target_group.serve_message.arn
}

output "security_group_id" {
  description = "ID of the ALB security group"
  value       = aws_security_group.alb_sg.id
}

output "https_listener_arn" {
  description = "ARN of the HTTPS listener for adding additional rules"
  value       = aws_lb_listener.https.arn
}

output "http_listener_arn" {
  description = "ARN of the HTTP listener"
  value       = aws_lb_listener.http.arn
}