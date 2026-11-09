# ---- Jira tickets from member reports (CC-241) -------------------------------------------
# An admin can turn a member's report into a ticket in the CC Jira project. The API calls Jira Cloud with an
# Atlassian account email and a scoped API token (read and write Jira work items), kept together as
# "email:token" in this parameter. The value is set outside Terraform, so it never lands in state or the repo:
#   aws ssm put-parameter --name /culture-crew/jira-api-credentials --type SecureString --overwrite --value 'email:token'
# Until then it holds "unset" and the Issues page says Jira isn't connected.
resource "aws_ssm_parameter" "jira_api_credentials" {
  name        = "/${var.name}/jira-api-credentials"
  description = "Jira Cloud email:API token for making tickets from member reports. Set by hand; Terraform never changes it."
  type        = "SecureString"
  value       = "unset"

  lifecycle {
    ignore_changes = [value]
  }
}

# The container works with the email-sender role's short-lived credentials (see discovery.tf), so that role may read
# this one parameter. The API reads it when it needs it; it's never in the container's environment.
resource "aws_iam_role_policy" "container_jira_credentials" {
  name = "read-jira-credentials"
  role = aws_iam_role.email_sender.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Sid = "ReadJiraCredentials", Effect = "Allow", Action = "ssm:GetParameter", Resource = aws_ssm_parameter.jira_api_credentials.arn }]
  })
}
