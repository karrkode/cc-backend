# CC-250: event photos. A private bucket that only CloudFront reads, served on the site's own domain at
# /event-images/* (frontend.tf), so the page needs no new host, certificate or CORS. Keys under event-images/ are
# the WebPs the site shows; originals/ keeps what each came from, never routed, so photos can be re-made later.
# Its own bucket rather than a folder in the site bucket: the site deploy syncs that bucket with --delete.

resource "aws_s3_bucket" "event_images" {
  bucket = "chiculturecrew-event-images"
}

resource "aws_s3_bucket_public_access_block" "event_images" {
  bucket                  = aws_s3_bucket.event_images.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "event_images" {
  bucket = aws_s3_bucket.event_images.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "event_images" {
  bucket = aws_s3_bucket.event_images.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

# A replaced photo leaves its old object behind; versioning keeps a deleted one recoverable for a while.
resource "aws_s3_bucket_versioning" "event_images" {
  bucket = aws_s3_bucket.event_images.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "event_images" {
  bucket = aws_s3_bucket.event_images.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
  depends_on = [aws_s3_bucket_versioning.event_images]
}

resource "aws_s3_bucket_policy" "event_images" {
  bucket = aws_s3_bucket.event_images.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "CloudFrontReadsServedPhotos", Effect = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.event_images.arn}/event-images/*"
        Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.site.arn } }
      },
      {
        Sid       = "TlsOnly", Effect = "Deny", Principal = "*", Action = "s3:*"
        Resource  = [aws_s3_bucket.event_images.arn, "${aws_s3_bucket.event_images.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.event_images]
}

# The API container (the email-sender role's short-lived credentials, as in photos.tf) stores photos for event
# sourcing. No delete: the product owner removes production data.
resource "aws_iam_role_policy" "container_event_images" {
  name = "event-images"
  role = aws_iam_role.email_sender.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "StoreEventPhotos", Effect = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject"]
      Resource = ["${aws_s3_bucket.event_images.arn}/event-images/*", "${aws_s3_bucket.event_images.arn}/originals/*"]
    }]
  })
}
