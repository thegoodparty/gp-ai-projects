variable "custom_domain_name" {
  description = "Custom domain name (e.g., ai.goodparty.org)"
  type        = string
}

variable "route53_zone_id" {
  description = "Route53 hosted zone ID for goodparty.org"
  type        = string
}

variable "alb_dns_name" {
  description = "Application Load Balancer DNS name"
  type        = string
}

variable "alb_zone_id" {
  description = "Application Load Balancer hosted zone ID"
  type        = string
}