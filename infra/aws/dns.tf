# The hosted zone. While staging, GoDaddy delegates just this subdomain here with an NS
# record; the `name_servers` output lists the values to enter at GoDaddy.
resource "aws_route53_zone" "main" {
  name = var.zone_name
}

resource "aws_route53_record" "api" {
  zone_id = aws_route53_zone.main.zone_id
  name    = var.api_domain
  type    = "A"
  ttl     = 300
  records = [aws_eip.api.public_ip]
}

resource "aws_route53_record" "site" {
  for_each = toset(concat([var.site_domain], var.site_aliases))
  zone_id  = aws_route53_zone.main.zone_id
  name     = each.value
  type     = "A"
  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = aws_cloudfront_distribution.site.hosted_zone_id
    evaluate_target_health = false
  }
}
