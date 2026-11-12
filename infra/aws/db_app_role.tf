# CC-200: the password for the API's least-privilege database role (culture_app, created by migration 0055).
# Generated as an *ephemeral* value and written through a *write-only* attribute, so it never lands in Terraform
# state (same pattern as secrets.tf). The JSON has the same shape as the RDS master secret, so write-env.sh builds
# both URLs the same way. The deploy step (scripts/migrate.py) sets it on the role as the owner.
# To rotate: bump secret_string_wo_version, apply, then run culture-crew-deploy.

ephemeral "random_password" "db_app" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "db_app" {
  name = "${var.name}/db-app"
}

resource "aws_secretsmanager_secret_version" "db_app" {
  secret_id                = aws_secretsmanager_secret.db_app.id
  secret_string_wo         = jsonencode({ username = "culture_app", password = ephemeral.random_password.db_app.result })
  secret_string_wo_version = 1
}

# The server reads it at boot and on each deploy (write-env.sh). A separate policy, so server.tf's stay as they are.
resource "aws_iam_role_policy" "api_db_app_secret" {
  name = "read-db-app-secret"
  role = aws_iam_role.api.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Sid = "ReadDbAppSecret", Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = aws_secretsmanager_secret.db_app.arn }]
  })
}
