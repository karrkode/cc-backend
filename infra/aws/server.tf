# The API server: one Graviton instance running the API container behind Caddy.
# No SSH key and no port 22; shell access is through SSM Session Manager.

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_iam_role" "api" {
  name = "${var.name}-api-server"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "api_ssm" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" # Session Manager + Run Command
}

# Exactly what the server needs, scoped to this project's resources.
resource "aws_iam_role_policy" "api" {
  name = "culture-crew-runtime"
  role = aws_iam_role.api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "EcrLogin", Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Sid      = "EcrPull", Effect = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = aws_ecr_repository.api.arn
      },
      {
        Sid      = "ReadDbSecret", Effect = "Allow", Action = "secretsmanager:GetSecretValue"
        Resource = aws_db_instance.main.master_user_secret[0].secret_arn
      },
      { Sid = "ReadJwtKey", Effect = "Allow", Action = "ssm:GetParameter", Resource = aws_ssm_parameter.jwt_secret.arn },
      # Account emails: the host assumes the send-only role and hands its short-lived
      # credentials to the API container (server/mail-creds.sh; the role is in ses.tf).
      { Sid = "AssumeEmailSender", Effect = "Allow", Action = "sts:AssumeRole", Resource = aws_iam_role.email_sender.arn },
      {
        Sid      = "WriteLogs", Effect = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = ["${aws_cloudwatch_log_group.api.arn}:*", "${aws_cloudwatch_log_group.sync.arn}:*"]
      },
    ]
  })
}

resource "aws_iam_instance_profile" "api" {
  name = "${var.name}-api-server"
  role = aws_iam_role.api.name
}

locals {
  cors_origins = join(",", [for d in concat([var.site_domain], var.site_aliases) : "https://${d}"])
}

resource "aws_instance" "api" {
  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public[0].id
  vpc_security_group_ids      = [aws_security_group.api.id]
  iam_instance_profile        = aws_iam_instance_profile.api.name
  associate_public_ip_address = true # needed to reach the internet at boot, before the Elastic IP attaches

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1          # containers can't reach the instance's credentials
  }

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/server/user_data.sh.tftpl", {
    region        = var.region
    image_repo    = aws_ecr_repository.api.repository_url
    image_tag     = var.api_image_tag
    db_secret_arn = aws_db_instance.main.master_user_secret[0].secret_arn
    db_host       = aws_db_instance.main.address
    db_name       = aws_db_instance.main.db_name
    jwt_param     = aws_ssm_parameter.jwt_secret.name
    cors_origins  = local.cors_origins
    log_group     = aws_cloudwatch_log_group.api.name
    api_domain    = var.api_domain
    acme_email    = var.alert_email
    deploy_sh     = file("${path.module}/server/deploy.sh")
    write_env_sh  = file("${path.module}/server/write-env.sh")
    sync_sh       = file("${path.module}/server/sync.sh")
    mail_creds_sh = file("${path.module}/server/mail-creds.sh")
    site_url      = "https://${var.site_domain}"
    mail_role_arn = aws_iam_role.email_sender.arn
  })
  # Changing the boot script replaces the server (it's disposable; data is in RDS, the
  # image in ECR). Caddy then fetches a new certificate, so avoid many replacements a week.
  user_data_replace_on_change = true

  lifecycle {
    ignore_changes = [ami] # don't replace the server every time AWS publishes a new AMI
  }

  tags = { Name = "${var.name}-api" }
}

# A fixed public IP, so DNS doesn't change when the server is replaced.
resource "aws_eip" "api" {
  domain   = "vpc"
  instance = aws_instance.api.id
  tags     = { Name = "${var.name}-api" }
}
