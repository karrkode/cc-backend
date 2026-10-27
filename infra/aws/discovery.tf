# ---- event discovery (CC-70) ------------------------------------------------------------
# Every hour from 7 AM to 10 PM Chicago time the server runs `scripts.discover`, which checks only
# the event sources that are due (most hours: none) and puts what it finds in a holding list for
# review. The Anthropic API key it needs is read from Parameter Store on the host and handed to the
# job on standard input, so it's never on a command line, in a file or in the container's environment.

# The key's value is set outside Terraform, so it never lands in state:
#   aws ssm put-parameter --name /culture-crew/anthropic-api-key --type SecureString --overwrite --value 'sk-ant-…'
# Until then it holds "unset", and discovery checks only what it can read without the model.
resource "aws_ssm_parameter" "anthropic_api_key" {
  name        = "/${var.name}/anthropic-api-key"
  description = "Anthropic API key for event discovery (Claude Haiku 5.5). Set by hand; Terraform never changes it."
  type        = "SecureString"
  value       = "unset"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_cloudwatch_log_group" "discovery" {
  name              = "/${var.name}/discovery" # output of each hourly discovery run
  retention_in_days = 90
}

resource "aws_scheduler_schedule" "discovery" {
  name                         = "${var.name}-discovery"
  description                  = "Check event sources that are due"
  schedule_expression          = "cron(15 7-22 * * ? *)"
  schedule_expression_timezone = "America/Chicago"

  flexible_time_window { mode = "OFF" }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ssm:sendCommand"
    role_arn = aws_iam_role.scheduler.arn
    input = jsonencode({
      DocumentName   = "AWS-RunShellScript"
      InstanceIds    = [aws_instance.api.id]
      Comment        = "Event discovery: check sources that are due"
      TimeoutSeconds = 600
      Parameters = {
        commands = [join(" ", [
          "bash -c 'set -euo pipefail; . /etc/culture-crew/config;",
          "aws ssm get-parameter --region \"$REGION\" --name ${aws_ssm_parameter.anthropic_api_key.name} --with-decryption --query Parameter.Value --output text",
          "| docker exec -i api python -m scripts.discover --key-stdin'",
        ])]
        executionTimeout = ["1800"]
      }
      CloudWatchOutputConfig = {
        CloudWatchOutputEnabled = true
        CloudWatchLogGroupName  = aws_cloudwatch_log_group.discovery.name
      }
    })
    retry_policy { maximum_retry_attempts = 0 } # the next hour tries again
  }
}

# The weekly recap (CC-77): Tuesdays at 10 AM Chicago time, an email to info@ about what discovery
# found, what's waiting and which sources couldn't be read. If it can't be sent, the command fails
# and the "command failed" alert fires (monitoring.tf), so a missing recap never goes unnoticed.
resource "aws_scheduler_schedule" "discovery_recap" {
  name                         = "${var.name}-discovery-recap"
  description                  = "Email the weekly event-discovery recap"
  schedule_expression          = "cron(0 10 ? * TUE *)"
  schedule_expression_timezone = "America/Chicago"

  flexible_time_window { mode = "OFF" }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ssm:sendCommand"
    role_arn = aws_iam_role.scheduler.arn
    input = jsonencode({
      DocumentName   = "AWS-RunShellScript"
      InstanceIds    = [aws_instance.api.id]
      Comment        = "Event discovery: weekly recap email"
      TimeoutSeconds = 600
      Parameters     = { commands = ["docker exec api python -m scripts.recap"], executionTimeout = ["300"] }
      CloudWatchOutputConfig = {
        CloudWatchOutputEnabled = true
        CloudWatchLogGroupName  = aws_cloudwatch_log_group.discovery.name
      }
    })
    retry_policy { maximum_retry_attempts = 0 }
  }
}

# CC-124: the API reads a member's link for the Suggest form, with Claude when the page has no event data. The
# container works with the email-sender role's short-lived credentials (ses.tf, server/mail-creds.sh), so that role
# may read this one parameter. A separate policy, so the role (and the server) stay as they are. The key is never in
# the container's environment: the API reads it when it needs it.
resource "aws_iam_role_policy" "container_anthropic_key" {
  name = "read-anthropic-key"
  role = aws_iam_role.email_sender.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Sid = "ReadAnthropicKey", Effect = "Allow", Action = "ssm:GetParameter", Resource = aws_ssm_parameter.anthropic_api_key.arn }]
  })
}
