# The page: a private S3 bucket that only CloudFront can read (Origin Access Control),
# served over HTTPS with an ACM certificate. Deploying the page is an upload plus a
# CloudFront invalidation; see the README.

resource "aws_s3_bucket" "site" {
  bucket = "${var.name}-site-${data.aws_caller_identity.me.account_id}"
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "site" {
  bucket = aws_s3_bucket.site.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "site" {
  bucket = aws_s3_bucket.site.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${var.name}-site"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "CloudFrontReadsOnly"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.site.arn}/*"
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.site.arn } }
    }]
  })
}

# ---- certificate (must be in us-east-1 for CloudFront) -------------------------------

resource "aws_acm_certificate" "site" {
  provider                  = aws.us_east_1
  domain_name               = var.site_domain
  subject_alternative_names = var.site_aliases
  validation_method         = "DNS"
  lifecycle { create_before_destroy = true }
}

resource "aws_route53_record" "site_cert_validation" {
  for_each = {
    for o in aws_acm_certificate.site.domain_validation_options : o.domain_name => o
  }
  zone_id         = aws_route53_zone.main.zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

# Waits until ACM sees the records, which needs the GoDaddy NS delegation in place.
resource "aws_acm_certificate_validation" "site" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.site.arn
  validation_record_fqdns = [for r in aws_route53_record.site_cert_validation : r.fqdn]
}

# ---- CloudFront ---------------------------------------------------------------------

# Redirects www. (and any other alias) to the canonical site_domain. Runs at the edge on
# every request, in well under a millisecond; requests for the canonical host pass through.
resource "aws_cloudfront_function" "canonical_host" {
  name    = "${var.name}-canonical-host"
  runtime = "cloudfront-js-2.0"
  comment = "Redirect aliases such as www. to ${var.site_domain}"
  publish = true
  code    = templatefile("${path.module}/www-redirect.js.tftpl", { canonical_host = var.site_domain })
}

locals {
  # AWS managed policies
  cache_policy_caching_optimized   = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  response_policy_security_headers = "67f7725c-6f97-4210-82d7-5512b31e9d03" # HSTS, nosniff, frame-options...
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  comment             = "${var.name} page"
  aliases             = concat([var.site_domain], var.site_aliases)
  default_root_object = "index.html"
  price_class         = "PriceClass_100" # North America and Europe edges only; cheapest
  http_version        = "http2and3"

  origin {
    origin_id                = "site-bucket"
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id           = "site-bucket"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = local.cache_policy_caching_optimized
    response_headers_policy_id = local.response_policy_security_headers

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.canonical_host.arn
    }
  }

  # Any path that isn't on the site gets the friendly 404 page. The private bucket answers
  # a missing file with 403 (it won't confirm what does or doesn't exist), so map both.
  dynamic "custom_error_response" {
    for_each = [403, 404]
    content {
      error_code            = custom_error_response.value
      response_code         = 404
      response_page_path    = "/404.html"
      error_caching_min_ttl = 60
    }
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.site.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}
