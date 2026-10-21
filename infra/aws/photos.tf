# ---- "I've been" photos (CC-76) ------------------------------------------------------------
# Members' photos from events they've been to. Private: no public access, encrypted, TLS only.
# The API checks each one with Amazon Rekognition before storing it, and shows photos only through
# links it signs itself (app/photos.py), so nothing outside the API ever reads this bucket.
# The name is fixed because the API's setting (photo_bucket in app/config.py) defaults to it; changing
# the server's environment would replace the server.

resource "aws_s3_bucket" "photos" {
  bucket = "chiculturecrew-went-photos"
}

resource "aws_s3_bucket_public_access_block" "photos" {
  bucket                  = aws_s3_bucket.photos.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "photos" {
  bucket = aws_s3_bucket.photos.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "photos" {
  bucket = aws_s3_bucket.photos.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

# No versioning: deleting a photo (its mark, its event or its account) really deletes it.
resource "aws_s3_bucket_versioning" "photos" {
  bucket = aws_s3_bucket.photos.id
  versioning_configuration { status = "Disabled" }
}

resource "aws_s3_bucket_policy" "photos_tls_only" {
  bucket = aws_s3_bucket.photos.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "TlsOnly", Effect = "Deny", Principal = "*", Action = "s3:*"
      Resource  = [aws_s3_bucket.photos.arn, "${aws_s3_bucket.photos.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.photos]
}

# The API container works with the short-lived credentials of the email-sender role (ses.tf;
# server/mail-creds.sh refreshes them). It also gets the photos: read, write and delete them in this
# bucket, and ask Rekognition about one. A separate policy, so the role (and the server) stay as they are.
resource "aws_iam_role_policy" "container_photos" {
  name = "went-photos"
  role = aws_iam_role.email_sender.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "StorePhotos", Effect = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.photos.arn}/photos/*"
      },
      { Sid = "ListPhotos", Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.photos.arn },
      # Images are sent as bytes, so Rekognition never reads S3. It has no resource-level permissions.
      { Sid = "CheckPhotos", Effect = "Allow", Action = "rekognition:DetectModerationLabels", Resource = "*" },
    ]
  })
}
