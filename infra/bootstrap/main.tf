# One-time setup: the S3 bucket that holds the main stack's Terraform state.
# This small stack keeps its own state locally (terraform.tfstate here, gitignored);
# it is only ever applied once.
#
#   cd infra/bootstrap && terraform init && terraform apply

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "region" {
  type    = string
  default = "us-east-2"
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "culture-crew", ManagedBy = "terraform-bootstrap" } }
}

data "aws_caller_identity" "me" {}

resource "aws_s3_bucket" "state" {
  # Bucket names are global, so the account id keeps this one unique.
  bucket = "culture-crew-tfstate-${data.aws_caller_identity.me.account_id}"
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" } # every state change can be rolled back
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}
