# A record for custom domain pointing to ALB
resource "aws_route53_record" "ai_goodparty_a" {
  zone_id = var.route53_zone_id
  name    = var.custom_domain_name
  type    = "A"

  alias {
    name                   = var.alb_dns_name
    zone_id                = var.alb_zone_id
    evaluate_target_health = true
  }
}

# AAAA record for IPv6 support (ALBs support IPv6)
resource "aws_route53_record" "ai_goodparty_aaaa" {
  zone_id = var.route53_zone_id
  name    = var.custom_domain_name
  type    = "AAAA"

  alias {
    name                   = var.alb_dns_name
    zone_id                = var.alb_zone_id
    evaluate_target_health = true
  }
}