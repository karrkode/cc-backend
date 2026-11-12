# ---- account monitoring (CC-192) ------------------------------------------------------------
# CC-192: who did what in the AWS account is kept (CloudTrail), threats are flagged (GuardDuty),
# unintended outside access is listed (Access Analyzer), and a root sign-in or any IAM change emails
# the alerts topic (monitoring.tf). The SES complaint and bounce alarms already exist (ses.tf, CC-44).

locals {
  account_id = data.aws_caller_identity.me.account_id
  # Written out, not taken from the trail resource, so the bucket policy doesn't depend on the trail it serves.
  trail_name = var.name
  trail_arn  = "arn:aws:cloudtrail:${var.region}:${data.aws_caller_identity.me.account_id}:trail/${var.name}"
}

# ---- CloudTrail: private, encrypted, TLS-only log bucket -----------------------------------

resource "aws_s3_bucket" "cloudtrail" {
  bucket = "${var.name}-cloudtrail-${local.account_id}"
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  bucket                  = aws_s3_bucket.cloudtrail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

# CC-192: a year of history. No cheaper storage class: many small files cost more to move than they save.
resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id
  rule {
    id     = "expire-after-a-year"
    status = "Enabled"
    filter {}
    expiration { days = 365 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

resource "aws_s3_bucket_policy" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AWSCloudTrailAclCheck", Effect = "Allow", Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl", Resource = aws_s3_bucket.cloudtrail.arn
        Condition = { StringEquals = { "aws:SourceArn" = local.trail_arn } }
      },
      {
        Sid       = "AWSCloudTrailWrite", Effect = "Allow", Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject", Resource = "${aws_s3_bucket.cloudtrail.arn}/AWSLogs/${local.account_id}/*"
        Condition = { StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control", "aws:SourceArn" = local.trail_arn } }
      },
      {
        Sid       = "TlsOnly", Effect = "Deny", Principal = "*", Action = "s3:*"
        Resource  = [aws_s3_bucket.cloudtrail.arn, "${aws_s3_bucket.cloudtrail.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.cloudtrail]
}

# Management events (read and write) from every region, with signed digests so tampering shows.
resource "aws_cloudtrail" "main" {
  name                          = local.trail_name
  s3_bucket_name                = aws_s3_bucket.cloudtrail.id
  is_multi_region_trail         = true
  include_global_service_events = true
  enable_log_file_validation    = true
  depends_on                    = [aws_s3_bucket_policy.cloudtrail]
}

# ---- GuardDuty: threat findings to the alerts topic ----------------------------------------

resource "aws_guardduty_detector" "main" {
  enable = true
}

resource "aws_cloudwatch_event_rule" "guardduty_finding" {
  name        = "${var.name}-guardduty-finding"
  description = "GuardDuty found something of medium severity or higher"
  event_pattern = jsonencode({
    source        = ["aws.guardduty"]
    "detail-type" = ["GuardDuty Finding"]
    detail        = { severity = [{ numeric = [">=", 4] }] }
  })
}

resource "aws_cloudwatch_event_target" "guardduty_finding" {
  rule = aws_cloudwatch_event_rule.guardduty_finding.name
  arn  = aws_sns_topic.alerts.arn
  # CC-192: a one-line email instead of the raw event JSON.
  input_transformer {
    input_paths = {
      title  = "$.detail.title"
      sev    = "$.detail.severity"
      type   = "$.detail.type"
      region = "$.detail.region"
    }
    input_template = "\"GuardDuty (severity <sev>) in <region>: <title> [<type>]\""
  }
}

# ---- IAM Access Analyzer (external access; free) -------------------------------------------

resource "aws_accessanalyzer_analyzer" "account" {
  analyzer_name = "${var.name}-account"
  type          = "ACCOUNT"
}

# ---- alerts on a root sign-in and on IAM changes -------------------------------------------
# IAM events only reach EventBridge in us-east-1, and a root console sign-in is recorded in one of
# us-east-1, us-east-2 or us-west-2. A rule in each of the other two forwards them to the default bus in
# our own region (us-east-2 needs none), where the rules below pick out what to email.

resource "aws_iam_role" "events_forwarder" {
  name = "${var.name}-events-forwarder"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Principal = { Service = "events.amazonaws.com" }, Action = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "events_forwarder" {
  name = "put-events-to-home-bus"
  role = aws_iam_role.events_forwarder.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow", Action = "events:PutEvents"
      Resource = "arn:aws:events:${var.region}:${local.account_id}:event-bus/default"
    }]
  })
}

resource "aws_cloudwatch_event_rule" "global_activity" {
  provider    = aws.us_east_1
  name        = "${var.name}-global-activity"
  description = "Forward IAM and console sign-in events to the home region"
  event_pattern = jsonencode({
    source        = ["aws.iam", "aws.signin"]
    "detail-type" = ["AWS API Call via CloudTrail", "AWS Console Sign In via CloudTrail"]
  })
}

resource "aws_cloudwatch_event_target" "global_activity" {
  provider = aws.us_east_1
  rule     = aws_cloudwatch_event_rule.global_activity.name
  arn      = "arn:aws:events:${var.region}:${local.account_id}:event-bus/default"
  role_arn = aws_iam_role.events_forwarder.arn
}

# CC-192: a root sign-in can be recorded in us-west-2, so forward sign-ins from there too.
resource "aws_cloudwatch_event_rule" "signin_west" {
  region      = "us-west-2"
  name        = "${var.name}-signin-west"
  description = "Forward console sign-in events to the home region"
  event_pattern = jsonencode({
    source        = ["aws.signin"]
    "detail-type" = ["AWS Console Sign In via CloudTrail"]
  })
}

resource "aws_cloudwatch_event_target" "signin_west" {
  region   = "us-west-2"
  rule     = aws_cloudwatch_event_rule.signin_west.name
  arn      = "arn:aws:events:${var.region}:${local.account_id}:event-bus/default"
  role_arn = aws_iam_role.events_forwarder.arn
}

resource "aws_cloudwatch_event_rule" "root_sign_in" {
  name        = "${var.name}-root-sign-in"
  description = "Someone signed in to the console as the root user"
  event_pattern = jsonencode({
    source        = ["aws.signin"]
    "detail-type" = ["AWS Console Sign In via CloudTrail"]
    detail        = { userIdentity = { type = ["Root"] } }
  })
}

resource "aws_cloudwatch_event_target" "root_sign_in" {
  rule = aws_cloudwatch_event_rule.root_sign_in.name
  arn  = aws_sns_topic.alerts.arn
  # CC-192: a one-line email instead of the raw event JSON.
  input_transformer {
    input_paths = {
      ip     = "$.detail.sourceIPAddress"
      time   = "$.time"
      result = "$.detail.responseElements.ConsoleLogin"
    }
    input_template = "\"Root console sign-in (<result>) from <ip> at <time>\""
  }
}

# Only changes reach EventBridge from CloudTrail; read-only calls (Get, List) are not delivered.
resource "aws_cloudwatch_event_rule" "iam_change" {
  name        = "${var.name}-iam-change"
  description = "An IAM user, role, policy or key was created, changed or deleted"
  event_pattern = jsonencode({
    source        = ["aws.iam"]
    "detail-type" = ["AWS API Call via CloudTrail"]
  })
}

resource "aws_cloudwatch_event_target" "iam_change" {
  rule = aws_cloudwatch_event_rule.iam_change.name
  arn  = aws_sns_topic.alerts.arn
  # CC-192: a one-line email instead of the raw event JSON.
  input_transformer {
    input_paths = {
      event = "$.detail.eventName"
      who   = "$.detail.userIdentity.arn"
      ip    = "$.detail.sourceIPAddress"
      time  = "$.time"
    }
    input_template = "\"IAM change <event> by <who> from <ip> at <time>\""
  }
}
