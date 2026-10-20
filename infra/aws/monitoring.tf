resource "aws_cloudwatch_log_group" "api" {
  name              = "/${var.name}/api" # API and Caddy container logs
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "sync" {
  name              = "/${var.name}/sheet-sync" # output of the retired nightly sheet sync (CC-72), kept as history
  retention_in_days = 90
}

# ---- alerts: one topic, emailed to you --------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email # AWS emails a confirmation link; nothing arrives until it's clicked
}

resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "CloudWatchAndEventBridge", Effect = "Allow"
        Principal = { Service = ["cloudwatch.amazonaws.com", "events.amazonaws.com"] }
        Action    = "sns:Publish", Resource = aws_sns_topic.alerts.arn
        Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.me.account_id } }
      },
      {
        Sid    = "Budgets", Effect = "Allow", Principal = { Service = "budgets.amazonaws.com" }
        Action = "sns:Publish", Resource = aws_sns_topic.alerts.arn
      },
    ]
  })
}

# The underlying AWS host failed: move the instance to healthy hardware, and tell me.
resource "aws_cloudwatch_metric_alarm" "api_host" {
  alarm_name          = "${var.name}-api-host-failed"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_System"
  dimensions          = { InstanceId = aws_instance.api.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = ["arn:aws:automate:${var.region}:ec2:recover", aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "api_instance" {
  alarm_name          = "${var.name}-api-instance-unhealthy"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_Instance"
  dimensions          = { InstanceId = aws_instance.api.id }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "db_storage" {
  alarm_name          = "${var.name}-db-storage-low"
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic           = "Minimum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 2 * 1024 * 1024 * 1024 # 2 GiB
  comparison_operator = "LessThanThreshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# Any Run Command on the server that fails: discovery, the weekly recap, or a deploy.
resource "aws_cloudwatch_event_rule" "command_failed" {
  name        = "${var.name}-command-failed"
  description = "A sync or deploy command on the API server failed"
  event_pattern = jsonencode({
    source        = ["aws.ssm"]
    "detail-type" = ["EC2 Command Invocation Status-change Notification"]
    detail = {
      "instance-id" = [aws_instance.api.id]
      status        = ["Failed", "TimedOut", "Cancelled"]
    }
  })
}

resource "aws_cloudwatch_event_target" "command_failed" {
  rule = aws_cloudwatch_event_rule.command_failed.name
  arn  = aws_sns_topic.alerts.arn
}

# ---- spending ------------------------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
