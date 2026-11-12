# CC-201: account-wide S3 Block Public Access. No bucket in the account can be made public by a
# public ACL or bucket policy, whatever its own settings say. Every project bucket already blocks
# public access itself (and CloudFront reaches the site and event images through OAC, a service
# principal, which S3 does not treat as public), so this only guards against mistakes and old buckets.
resource "aws_s3_account_public_access_block" "this" {
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
