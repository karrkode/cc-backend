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
# Production: Route 53 hosts the whole chiculturecrew.com zone, email records included
# (dns_mail.tf). The site was first staged on aws.chiculturecrew.com, delegated from GoDaddy.

variable "zone_name" {
  description = "The Route 53 hosted zone Terraform creates and manages."
  type        = string
  default     = "chiculturecrew.com"
}

variable "site_domain" {
  description = "Where the page is served; the canonical address everything else redirects to."
  type        = string
  default     = "chiculturecrew.com"
}

variable "site_aliases" {
  description = "Other names for the page; they redirect to site_domain."
  type        = list(string)
  default     = ["www.chiculturecrew.com"]
}

variable "api_domain" {
  type    = string
  default = "api.chiculturecrew.com"
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
  default = "karrkode/cc-site"
}

variable "github_deploy_environment" {
  description = "GitHub environment deploy jobs run in; restrict it to main in each repo's settings."
  type        = string
  default     = "production"
}
