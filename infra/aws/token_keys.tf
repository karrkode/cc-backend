# ---- CC-204: one key per purpose, and the nightly token clean-up ------------------------------------------------
# Photo links and visitor hashing used to be derived from the sign-in key (jwt-secret). Each now has its own
# parameter, so rotating one doesn't affect the others. The API reads them while it runs (app/keys.py), like the
# Jira and Anthropic keys, so the server's startup setup is untouched. Until a parameter holds a real value
# the app falls back to the old derivation, so applying this (or not) never breaks links.

# Generated as an ephemeral value and written through a write-only attribute, so it never lands in state.
# Bump value_wo_version to rotate it: photo links issued in the last 15 minutes stop working once.
ephemeral "random_password" "photo_link_key" {
  length  = 64
  special = false
}

resource "aws_ssm_parameter" "photo_link_key" {
  name             = "/${var.name}/photo-link-key"
  description      = "Signs the short-lived photo links."
  type             = "SecureString"
  value_wo         = ephemeral.random_password.photo_link_key.result
  value_wo_version = 1
}

# Set by hand so returning-visitor counts continue: copy the current jwt-secret value into it (see infra/README.md).
# A fresh random value would restart everyone's "first seen" date. Until set it holds "unset" and the app derives the key.
resource "aws_ssm_parameter" "visitor_hash_key" {
  name        = "/${var.name}/visitor-hash-key"
  description = "Hashes visitors for the traffic stats. Set by hand; Terraform never changes it."
  type        = "SecureString"
  value       = "unset"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_iam_role_policy" "container_token_keys" {
  name = "read-purpose-keys"
  role = aws_iam_role.email_sender.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadPurposeKeys", Effect = "Allow", Action = "ssm:GetParameter"
      Resource = [aws_ssm_parameter.photo_link_key.arn, aws_ssm_parameter.visitor_hash_key.arn]
    }]
  })
}

# Nightly at 3:30 AM Chicago time: delete expired sessions and expired or used-reset email links. If it fails, the command
# fails and the "command failed" alert fires (monitoring.tf). Apply after the deploy that adds the script.
resource "aws_scheduler_schedule" "token_cleanup" {
  name                         = "${var.name}-token-cleanup"
  description                  = "Delete expired sessions and expired or used-reset email links"
  schedule_expression          = "cron(30 3 * * ? *)"
  schedule_expression_timezone = "America/Chicago"

  flexible_time_window { mode = "OFF" }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ssm:sendCommand"
    role_arn = aws_iam_role.scheduler.arn
    input = jsonencode({
      DocumentName   = "AWS-RunShellScript"
      InstanceIds    = [aws_instance.api.id]
      Comment        = "Nightly token clean-up"
      TimeoutSeconds = 600
      Parameters     = { commands = ["docker exec api python -m scripts.token_cleanup"], executionTimeout = ["300"] }
      CloudWatchOutputConfig = {
        CloudWatchOutputEnabled = true
        CloudWatchLogGroupName  = aws_cloudwatch_log_group.discovery.name
      }
    })
    retry_policy { maximum_retry_attempts = 0 }
  }
}
