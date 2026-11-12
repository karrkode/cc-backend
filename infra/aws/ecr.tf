resource "aws_ecr_repository" "api" {
  name = "${var.name}/api"
  # CC-201/CC-200: no tag can ever be overwritten. There is no "latest"; the server finds the current tag in
  # Parameter Store (image_tag.tf).
  image_tag_mutability = "IMMUTABLE"

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
