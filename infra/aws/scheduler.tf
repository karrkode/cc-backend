# The nightly Google Sheet sync: EventBridge Scheduler asks SSM Run Command to run
# culture-crew-sync on the server. Output goes to CloudWatch; failures alert (monitoring.tf).

resource "aws_iam_role" "scheduler" {
  name = "${var.name}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Principal = { Service = "scheduler.amazonaws.com" }, Action = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.me.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "run-sheet-sync"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Action = "ssm:SendCommand"
      Resource = [
        "arn:aws:ssm:${var.region}::document/AWS-RunShellScript",
        aws_instance.api.arn,
      ]
    }]
  })
}

resource "aws_scheduler_schedule" "sheet_sync" {
  name                         = "${var.name}-sheet-sync"
  description                  = "Bring events in line with the Google Sheet"
  schedule_expression          = var.sync_schedule
  schedule_expression_timezone = "America/Chicago"

  flexible_time_window { mode = "OFF" }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ssm:sendCommand"
    role_arn = aws_iam_role.scheduler.arn
    input = jsonencode({
      DocumentName   = "AWS-RunShellScript"
      InstanceIds    = [aws_instance.api.id]
      Comment        = "Nightly Google Sheet sync"
      TimeoutSeconds = 600
      Parameters     = { commands = ["/usr/local/bin/culture-crew-sync"], executionTimeout = ["600"] }
      CloudWatchOutputConfig = {
        CloudWatchOutputEnabled = true
        CloudWatchLogGroupName  = aws_cloudwatch_log_group.sync.name
      }
    })
    retry_policy { maximum_retry_attempts = 2 }
  }
}
