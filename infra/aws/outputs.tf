output "name_servers" {
  description = "Add these at GoDaddy as NS records for the zone (staging: host \"aws\")."
  value       = aws_route53_zone.main.name_servers
}

output "site_url" {
  value = "https://${var.site_domain}"
}

output "api_url" {
  value = "https://${var.api_domain}"
}

output "site_bucket" {
  value = aws_s3_bucket.site.bucket
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.site.id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "api_instance_id" {
  value = aws_instance.api.id
}

output "api_public_ip" {
  value = aws_eip.api.public_ip
}

output "db_endpoint" {
  value = aws_db_instance.main.address
}

output "db_tunnel_command" {
  description = "Forwards localhost:5433 to the database through the server (SSM), for a GUI client."
  value = join(" ", [
    "aws ssm start-session --region ${var.region} --target ${aws_instance.api.id}",
    "--document-name AWS-StartPortForwardingSessionToRemoteHost",
    "--parameters '{\"host\":[\"${aws_db_instance.main.address}\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"5433\"]}'",
  ])
}

output "deploy_backend_role_arn" {
  value = aws_iam_role.deploy_backend.arn
}

output "deploy_frontend_role_arn" {
  value = aws_iam_role.deploy_frontend.arn
}
