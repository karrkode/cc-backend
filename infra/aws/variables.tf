variable "name" {
  description = "Prefix for resource names."
  type        = string
  default     = "culture-crew"
}

variable "region" {
  type    = string
  default = "us-east-2"
}

# ---- domains ------------------------------------------------------------------
# Staging: GoDaddy hands just aws.chiculturecrew.com to Route 53 (one NS record), so the
# live site and email are untouched. At cutover these become the bare domain.

variable "zone_name" {
  description = "The Route 53 hosted zone Terraform creates and manages."
  type        = string
  default     = "aws.chiculturecrew.com"
}

variable "site_domain" {
  description = "Where the page is served."
  type        = string
  default     = "aws.chiculturecrew.com"
}

variable "site_aliases" {
  description = "Other names for the page, e.g. www.chiculturecrew.com after cutover."
  type        = list(string)
  default     = []
}

variable "api_domain" {
  type    = string
  default = "api.aws.chiculturecrew.com"
}

# ---- sizes (free-tier eligible defaults) ----------------------------------------

variable "instance_type" {
  description = "Graviton (arm64), matching images built on an Apple Silicon Mac."
  type        = string
  default     = "t4g.micro"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_backup_days" {
  type    = number
  default = 7
}

variable "db_deletion_protection" {
  description = "Set to false (and apply) before `terraform destroy` can remove the database."
  type        = bool
  default     = true
}

# ---- operations -----------------------------------------------------------------

variable "alert_email" {
  description = "Gets budget, health and failed-sync alerts. Confirm the SNS email AWS sends."
  type        = string
}

variable "monthly_budget_usd" {
  type    = number
  default = 15
}

variable "sync_schedule" {
  description = "When the Google Sheet sync runs (EventBridge cron, Chicago time)."
  type        = string
  default     = "cron(0 3 * * ? *)"
}

variable "api_image_tag" {
  description = "Which image tag the server runs."
  type        = string
  default     = "latest"
}

# ---- CI/CD (GitHub Actions deploys through OIDC, no stored AWS keys) -------------

variable "github_backend_repo" {
  type    = string
  default = "karrkode/cc-backend"
}

variable "github_frontend_repo" {
  type    = string
  default = "karrkode/cultures-crew-app"
}

variable "github_deploy_refs" {
  description = "Branches allowed to deploy."
  type        = list(string)
  default     = ["refs/heads/main"]
}
