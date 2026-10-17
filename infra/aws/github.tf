# GitHub Actions deploys without any stored AWS keys: each workflow run gets short-lived
# credentials through OIDC.
#
# Deploy jobs run in a GitHub *environment* ("production"), which is what shows deploys in
# Jira. A job in an environment gets the subject "repo:<owner>/<repo>:environment:<name>"
# (not ":ref:refs/heads/main"), so the roles trust exactly that. The environment itself is
# limited to the main branch in each repo's GitHub settings, so other branches can't use it.

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  github_trust = {
    for repo in [var.github_backend_repo, var.github_frontend_repo] : repo => jsonencode({
      Version = "2012-10-17"
      Statement = [{
        Effect    = "Allow"
        Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
        Action    = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
            "token.actions.githubusercontent.com:sub" = "repo:${repo}:environment:${var.github_deploy_environment}"
          }
        }
      }]
    })
  }
}

# cc-backend: push an image, then tell the server to deploy it.
resource "aws_iam_role" "deploy_backend" {
  name               = "${var.name}-deploy-backend"
  assume_role_policy = local.github_trust[var.github_backend_repo]
}

resource "aws_iam_role_policy" "deploy_backend" {
  name = "push-and-deploy"
  role = aws_iam_role.deploy_backend.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Effect = "Allow", Resource = aws_ecr_repository.api.arn
        Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage"]
      },
      {
        Effect   = "Allow", Action = "ssm:SendCommand"
        Resource = ["arn:aws:ssm:${var.region}::document/AWS-RunShellScript", aws_instance.api.arn]
      },
      { Effect = "Allow", Action = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"], Resource = "*" },
    ]
  })
}

# cc-site: upload the page and clear CloudFront's cache.
resource "aws_iam_role" "deploy_frontend" {
  name               = "${var.name}-deploy-frontend"
  assume_role_policy = local.github_trust[var.github_frontend_repo]
}

resource "aws_iam_role_policy" "deploy_frontend" {
  name = "upload-and-invalidate"
  role = aws_iam_role.deploy_frontend.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.site.arn },
      { Effect = "Allow", Action = ["s3:PutObject", "s3:DeleteObject"], Resource = "${aws_s3_bucket.site.arn}/*" },
      { Effect = "Allow", Action = "cloudfront:CreateInvalidation", Resource = aws_cloudfront_distribution.site.arn },
    ]
  })
}
