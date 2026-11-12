# CC-200 (folds in CC-201's ECR leftover): every image tag in ECR is immutable and "latest" is gone. A new server
# (first boot, or a replacement) finds which image to run in this parameter, which CI updates after each successful
# deploy. Terraform doesn't create the parameter, so applying never rewinds it to an older tag; CI creates it on the
# first deploy after this lands (or the product owner seeds it once, see the CC-200 PR).

locals {
  image_tag_param_name = "/${var.name}/api-image-tag"
  image_tag_param_arn  = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.me.account_id}:parameter${local.image_tag_param_name}"
}

# The server reads it when it has no tag of its own yet (deploy.sh).
resource "aws_iam_role_policy" "api_image_tag" {
  name = "read-image-tag"
  role = aws_iam_role.api.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Sid = "ReadImageTag", Effect = "Allow", Action = "ssm:GetParameter", Resource = local.image_tag_param_arn }]
  })
}

# CI records the tag it just deployed. Only this one parameter.
resource "aws_iam_role_policy" "deploy_backend_image_tag" {
  name = "record-image-tag"
  role = aws_iam_role.deploy_backend.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Sid = "RecordImageTag", Effect = "Allow", Action = "ssm:PutParameter", Resource = local.image_tag_param_arn }]
  })
}
