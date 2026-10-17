# Culture Crew on AWS

Terraform for the whole stack, in **us-east-2 (Ohio)**:

```
Browser ─► Route 53 ─┬─► CloudFront (HTTPS, ACM cert from us-east-1) ─► S3 bucket (private; the page)
                     └─► Elastic IP ─► EC2 t4g.micro, public subnet
                                        Caddy (HTTPS, Let's Encrypt) ─► API container (from ECR)
                                                                          │
                                         private subnets ◄────────────────┘ port 5432 only from the API's SG
                                         RDS Postgres 16, db.t4g.micro, encrypted, no public address

EventBridge Scheduler (03:00 Chicago) ─► SSM Run Command ─► culture-crew-sync on the server
Secrets Manager (DB password, managed by RDS) + Parameter Store (JWT key) ─► read by the server at start
CloudWatch logs + alarms ─► SNS ─► email · AWS Budget ─► email
GitHub Actions ─► OIDC roles (no stored keys) ─► push image / upload page
```

| File | What it makes |
|---|---|
| `bootstrap/` | the S3 bucket for Terraform state (applied once) |
| `aws/network.tf` | VPC, public + private subnets in 2 AZs, security groups (no NAT gateway) |
| `aws/database.tf` | RDS Postgres; RDS generates and stores the password |
| `aws/secrets.tf` | JWT signing key, written write-only so it never enters Terraform state |
| `aws/server.tf`, `aws/server/` | the EC2 instance, its IAM role, and its boot and deploy scripts |
| `aws/frontend.tf` | S3 bucket, CloudFront, ACM certificate |
| `aws/dns.tf` | the Route 53 zone and records |
| `aws/scheduler.tf` | the nightly Google Sheet sync |
| `aws/monitoring.tf` | log groups, alarms, failed-command alerts, monthly budget |
| `aws/github.tf` | OIDC deploy roles for both repos |

Rough cost while free-tier eligible: about $1–2/month out of pocket (Route 53 zone $0.50,
a Parameter Store/Secrets Manager secret $0.40; the server, database and public IPv4
address are covered). After the free tier: about $25/month, mostly RDS, EC2 and the
public IPv4 address ($3.60).

## First deployment

**Prerequisites:** `aws sso login --profile culture-crew` works, and
`export AWS_PROFILE=culture-crew` in the terminal you use.

### 1. State bucket (once)

```
cd infra/bootstrap
terraform init && terraform apply
```

### 2. The zone first, so GoDaddy can delegate to it

```
cd ../aws
cp terraform.tfvars.example terraform.tfvars        # put your email in it
terraform init -backend-config="bucket=culture-crew-tfstate-$(aws sts get-caller-identity --query Account --output text)"
terraform apply -target=aws_route53_zone.main
terraform output name_servers
```

At **GoDaddy → DNS → Add record**, add four **NS** records, host **`aws`**, one per name
server above. This touches nothing else in the zone: the live site and email are unaffected.
Check it with `dig +short NS aws.chiculturecrew.com` (can take a few minutes).

### 3. Everything else

```
terraform apply
```

About 15 minutes; RDS and CloudFront are the slow parts. Confirm the SNS email AWS sends you.

### 4. First API image

The server boots before any image exists, so push one and deploy it:

```
cd ../..   # the cc-backend repo root
REPO=$(terraform -chdir=infra/aws output -raw ecr_repository_url)
aws ecr get-login-password | docker login --username AWS --password-stdin ${REPO%%/*}
docker build -t "${REPO}:latest" -t "${REPO}:$(git rev-parse --short HEAD)" .
docker push --all-tags "${REPO}"

aws ssm send-command --document-name AWS-RunShellScript \
  --targets Key=InstanceIds,Values=$(terraform -chdir=infra/aws output -raw api_instance_id) \
  --parameters 'commands=["/usr/local/bin/culture-crew-deploy","/usr/local/bin/culture-crew-sync"]'
```

The deploy runs the database migrations; the sync loads every event from the sheet.
`https://api.aws.chiculturecrew.com/health` should answer `{"status":"ok"}`.

### 5. The page

From the cc-site repo:

```
aws s3 cp public/index.html s3://<site_bucket>/index.html --cache-control no-cache
aws cloudfront create-invalidation --distribution-id <cloudfront_distribution_id> --paths '/*'
```

Then open https://aws.chiculturecrew.com.

## Day to day

| Task | How |
|---|---|
| Shell on the server | `aws ssm start-session --target <api_instance_id>` |
| API logs | CloudWatch → `/culture-crew/api` (streams `api`, `caddy`) |
| Sync results | CloudWatch → `/culture-crew/sheet-sync`, or `GET /admin/syncs` |
| Run the sync now | `aws ssm send-command ... commands=["/usr/local/bin/culture-crew-sync"]` |
| Look at the database | run the `db_tunnel_command` output, then connect a GUI to `localhost:5433` (user `culture_admin`, password from Secrets Manager) |
| Make an admin | on the server: `docker exec api python -m scripts.make_admin you@example.com` |

## Tearing it down

Set `db_deletion_protection = false` in `terraform.tfvars`, `terraform apply`, then
`terraform destroy`. RDS keeps a final snapshot (`culture-crew-db-final`), which you can
delete in the console. Remove the `aws` NS records at GoDaddy afterwards.
