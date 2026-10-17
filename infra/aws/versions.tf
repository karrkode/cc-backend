terraform {
  required_version = ">= 1.11" # write-only attributes keep secrets out of state

  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.7" }
  }

  # State lives in the bucket made by infra/bootstrap. Its name includes the account id,
  # so it is passed at init time:
  #   terraform init -backend-config="bucket=culture-crew-tfstate-<account id>"
  backend "s3" {
    key          = "culture-crew/aws.tfstate"
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true # S3-native state locking; no DynamoDB table needed
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "culture-crew", ManagedBy = "terraform" } }
}

# CloudFront only accepts TLS certificates from us-east-1, whatever region the rest is in.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
  default_tags { tags = { Project = "culture-crew", ManagedBy = "terraform" } }
}

data "aws_caller_identity" "me" {}
data "aws_region" "current" {}
