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
