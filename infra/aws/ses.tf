# Email the site sends (address verification and password-reset links) goes through Amazon SES.
#
# SES is in us-east-1: it was first set up there in the console, where production access was
# requested. The API server in us-east-2 calls it across regions, which works the same.
# Microsoft 365 mail is untouched: SES signs with its own DKIM keys (three new CNAMEs beside
# Microsoft's selector1/selector2) and uses mail.<domain> as its envelope (MAIL FROM) domain,
# so the root MX and SPF records stay as they are and DMARC passes through aligned DKIM.

locals {
  ses_region    = "us-east-1"
  ses_mail_from = "mail.${var.zone_name}"
}

# The domain and the info@ address were created in the SES console on 2026-10-17; these import
# blocks bring them under Terraform on the next apply instead of creating duplicates.
import {
  provider = aws.us_east_1
  to       = aws_sesv2_email_identity.domain
  id       = var.zone_name
}

import {
  provider = aws.us_east_1
  to       = aws_sesv2_email_identity_mail_from_attributes.domain
  id       = var.zone_name
}

import {
  provider = aws.us_east_1
  to       = aws_sesv2_email_identity.alert_inbox
  id       = var.alert_email
}

import {
  provider = aws.us_east_1
  to       = aws_sesv2_configuration_set.default
  id       = "my-first-configuration-set"
}

# Applied to every email from these identities. Keeps the console's name; its job is reputation
# metrics (bounce and complaint rates in CloudWatch, which AWS also watches).
resource "aws_sesv2_configuration_set" "default" {
  provider               = aws.us_east_1
  configuration_set_name = "my-first-configuration-set"

  reputation_options {
    reputation_metrics_enabled = true
  }
  sending_options {
    sending_enabled = true
  }
}

# Sending identity for anything @<domain>, e.g. no-reply@. Easy DKIM, 2048-bit keys.
resource "aws_sesv2_email_identity" "domain" {
  provider               = aws.us_east_1
  email_identity         = var.zone_name
  configuration_set_name = aws_sesv2_configuration_set.default.configuration_set_name
}

# A verified inbox: while SES is still in its sandbox it can only send to verified addresses,
# so this is where test emails go.
resource "aws_sesv2_email_identity" "alert_inbox" {
  provider               = aws.us_east_1
  email_identity         = var.alert_email
  configuration_set_name = aws_sesv2_configuration_set.default.configuration_set_name
}

resource "aws_route53_record" "ses_dkim" {
  count   = 3 # SES Easy DKIM always issues three tokens
  zone_id = aws_route53_zone.main.zone_id
  name    = "${aws_sesv2_email_identity.domain.dkim_signing_attributes[0].tokens[count.index]}._domainkey.${var.zone_name}"
  type    = "CNAME"
  ttl     = 1800
  records = ["${aws_sesv2_email_identity.domain.dkim_signing_attributes[0].tokens[count.index]}.dkim.amazonses.com"]
}

# Bounces go to mail.<domain>, not to the root (Microsoft 365). If its MX record ever can't be
# found, SES falls back to its own amazonses.com envelope and still sends; DKIM keeps DMARC passing.
resource "aws_sesv2_email_identity_mail_from_attributes" "domain" {
  provider               = aws.us_east_1
  email_identity         = aws_sesv2_email_identity.domain.email_identity
  mail_from_domain       = local.ses_mail_from
  behavior_on_mx_failure = "USE_DEFAULT_VALUE"
}

resource "aws_route53_record" "ses_mail_from_mx" {
  zone_id = aws_route53_zone.main.zone_id
  name    = local.ses_mail_from
  type    = "MX"
  ttl     = 1800
  records = ["10 feedback-smtp.${local.ses_region}.amazonses.com"]
}

resource "aws_route53_record" "ses_mail_from_spf" {
  zone_id = aws_route53_zone.main.zone_id
  name    = local.ses_mail_from
  type    = "TXT"
  ttl     = 1800
  records = ["v=spf1 include:amazonses.com ~all"]
}

# The only thing the API container can do with AWS: send account email from @<domain>.
# The API server's role assumes it and passes on hour-long credentials (server/mail-creds.sh),
# so the container never sees the server's own role and no long-lived key exists.
resource "aws_iam_role" "email_sender" {
  name                 = "${var.name}-email-sender"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { AWS = aws_iam_role.api.arn }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "email_sender" {
  name = "send-account-email"
  role = aws_iam_role.email_sender.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid = "SendAccountEmail", Effect = "Allow", Action = "ses:SendEmail"
      # The sending identity and the configuration set every email from it uses. While SES is in
      # its sandbox it also checks the recipient's identity, so the verified inbox is listed too;
      # once production access is granted that check stops and nothing here needs to change.
      Resource = [
        aws_sesv2_email_identity.domain.arn,
        aws_sesv2_configuration_set.default.arn,
        aws_sesv2_email_identity.alert_inbox.arn,
      ]
    }]
  })
}
