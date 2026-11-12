resource "aws_ecr_repository" "api" {
  name = "${var.name}/api"
  # CC-201: commit tags (the rollback targets) can never be overwritten; only "latest" moves.
  # Fully IMMUTABLE needs the server's "latest" fallback removed, which replaces the server (see CC-200).
  image_tag_mutability = "IMMUTABLE_WITH_EXCLUSION"
  image_tag_mutability_exclusion_filter {
    filter      = "latest"
    filter_type = "WILDCARD"
  }

  image_scanning_configuration {
    scan_on_push = true # known-vulnerability scan of every pushed image
  }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 15 newest images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 15 }
      action       = { type = "expire" }
    }]
  })
}
