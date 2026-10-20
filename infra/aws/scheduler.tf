# EventBridge Scheduler's role: it may ask SSM Run Command to run shell commands on the API server.
# The schedules that use it are in discovery.tf (hourly discovery, the weekly recap). The nightly Google
# Sheet sync that used to run here was retired in October 2026 (CC-72). The policy keeps its old name
# so it isn't replaced.

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
