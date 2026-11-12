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
  cache_policy_caching_optimized = "658327ea-f89d-4fab-a63d-7e88639e58f6"
}

# CC-193: the Content-Security-Policy. It ships as Content-Security-Policy-Report-Only first; the one-line
# change to csp_enforce = true enforces it, once the live pages show no violations in the browser console.
# Keep these lists equal to the ones in web.nginx.conf (tests/test_csp.py checks it).
locals {
  csp_enforce = false

  # base64 sha256 of app/seo.py THEME_SCRIPT, which is also the body of the inline script in the site's
  # index.html and 404.html (cc-site scripts/test_csp.js pins the same value). tests/test_csp.py checks it.
  csp_theme_hash = "sha256-fKtflTlilgoEdPhIIhjI+ISyYPQ2B0LkNrNMqwKPy3U="

  # The site: S3 pages, scripts and styles from 'self'. style-src-attr allows the inline style="" attributes
  # (a follow-up story removes them); api.github.com is for the unlinked deployments.html.
  csp_site = join("; ", [
    "default-src 'self'",
    "script-src 'self' '${local.csp_theme_hash}'",
    "style-src 'self' https://fonts.googleapis.com",
    "style-src-attr 'unsafe-inline'",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data: blob: https:",
    "connect-src 'self' https://api.chiculturecrew.com https://api.github.com",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ])

  # The pages the API renders (/e/, /chicago/): no script files, and their CSS is inline in the page.
  csp_api_pages = join("; ", [
    "default-src 'none'",
    "script-src '${local.csp_theme_hash}'",
    # 'self': CloudFront may serve the site's 404 page (and its /css/404.css) on these paths
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data:",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'none'",
  ])

  security_header_policies = {
    site      = { csp = local.csp_site, comment = "Site, 404 page and event photos (CC-193)" }
    api_pages = { csp = local.csp_api_pages, comment = "Pages the API renders: /e/, /chicago/ (CC-193)" }
  }
}

# CC-193: replaces the managed SecurityHeadersPolicy, keeping what it sent (HSTS, nosniff, frame options,
# referrer policy, X-XSS-Protection) and adding includeSubDomains, a Permissions-Policy and the CSP.
# override = true: these replace what the origin sends (the managed policy's X-Frame-Options was SAMEORIGIN).
resource "aws_cloudfront_response_headers_policy" "security" {
  for_each = local.security_header_policies

  name    = "${var.name}-${replace(each.key, "_", "-")}-security-headers"
  comment = each.value.comment

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
    }
    content_type_options { override = true }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
    xss_protection {
      mode_block = true
      protection = true
      override   = true
    }

    dynamic "content_security_policy" {
      for_each = local.csp_enforce ? [1] : []
      content {
        content_security_policy = each.value.csp
        override                = true
      }
    }
  }

  custom_headers_config {
    items {
      header   = "Permissions-Policy"
      value    = "camera=(), microphone=(), geolocation=()"
      override = true
    }
    dynamic "items" {
      for_each = local.csp_enforce ? [] : [1]
      content {
        header   = "Content-Security-Policy-Report-Only"
        value    = each.value.csp
        override = true
      }
    }
  }
}

# CC-108: event pages, the sitemap and robots.txt come from the API, which renders them as plain HTML
# for search engines and link previews. Cached for the API's max-age (10 minutes), never longer than
# an hour; no cookies, headers or query strings are passed or cached on.
resource "aws_cloudfront_cache_policy" "api_pages" {
  name        = "${var.name}-api-pages"
  comment     = "Event pages, sitemap, robots.txt (CC-108)"
  min_ttl     = 0
  default_ttl = 600
  max_ttl     = 3600
  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config { query_string_behavior = "none" }
  }
}

locals {
  api_page_paths = ["/e/*", "/chicago", "/chicago/*", "/sitemap.xml", "/robots.txt"] # CC-109: roundups at /chicago/
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

  origin {
    origin_id                = "event-images" # CC-250: event_images.tf
    domain_name              = aws_s3_bucket.event_images.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  origin {
    origin_id   = "api"
    domain_name = var.api_domain
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  dynamic "ordered_cache_behavior" {
    for_each = local.api_page_paths
    content {
      path_pattern               = ordered_cache_behavior.value
      target_origin_id           = "api"
      viewer_protocol_policy     = "redirect-to-https"
      allowed_methods            = ["GET", "HEAD"]
      cached_methods             = ["GET", "HEAD"]
      compress                   = true
      cache_policy_id            = aws_cloudfront_cache_policy.api_pages.id
      response_headers_policy_id = aws_cloudfront_response_headers_policy.security["api_pages"].id

      function_association {
        event_type   = "viewer-request"
        function_arn = aws_cloudfront_function.canonical_host.arn
      }
    }
  }

  # CC-250: event photos. Their keys change with their bytes, so they're cached as long as CloudFront allows.
  ordered_cache_behavior {
    path_pattern               = "/event-images/*"
    target_origin_id           = "event-images"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = false # WebP is already compressed
    cache_policy_id            = local.cache_policy_caching_optimized
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security["site"].id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.canonical_host.arn
    }
  }

  default_cache_behavior {
    target_origin_id           = "site-bucket"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = local.cache_policy_caching_optimized
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security["site"].id

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
