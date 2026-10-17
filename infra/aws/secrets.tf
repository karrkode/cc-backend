# The key that signs login tokens. It's generated as an *ephemeral* value and written to
# Parameter Store through a *write-only* attribute, so it never lands in Terraform state.
# Bump jwt_secret_version (and apply) to rotate it; everyone is signed out when it changes.

ephemeral "random_password" "jwt" {
  length  = 64
  special = false
}

resource "aws_ssm_parameter" "jwt_secret" {
  name             = "/${var.name}/jwt-secret"
  type             = "SecureString"
  value_wo         = ephemeral.random_password.jwt.result
  value_wo_version = 1
}
