output "a_record_name" {
  description = "A record name for the custom domain"
  value       = aws_route53_record.ai_goodparty_a.name
}

output "a_record_fqdn" {
  description = "Fully qualified domain name for the A record"
  value       = aws_route53_record.ai_goodparty_a.fqdn
}

output "aaaa_record_name" {
  description = "AAAA record name for the custom domain"
  value       = aws_route53_record.ai_goodparty_aaaa.name
}

output "aaaa_record_fqdn" {
  description = "Fully qualified domain name for the AAAA record"
  value       = aws_route53_record.ai_goodparty_aaaa.fqdn
}