# Culture Crew on AWS

Terraform for the whole stack, in **us-east-2 (Ohio)**, serving
**https://chiculturecrew.com** (www. redirects there) and **https://api.chiculturecrew.com**.
Route 53 hosts the whole `chiculturecrew.com` zone, including the Microsoft 365 email records;
the domain itself is still registered at GoDaddy.

```
Browser ─► Route 53 ─┬─► CloudFront (HTTPS, ACM cert from us-east-1, www. → bare domain) ─► S3 (private)
                     └─► Elastic IP ─► EC2 t4g.micro, public subnet
                                        Caddy (HTTPS, Let's Encrypt) ─► API container (from ECR)
                                                                          │
                                         private subnets ◄────────────────┘ port 5432 only from the API's SG
                                         RDS Postgres 16, db.t4g.micro, encrypted, no public address

API container ─► S3 ("I've been" photos, private) · Rekognition (checks each photo first) · SES (email)
EventBridge Scheduler (hourly discovery, Tuesday recap) ─► SSM Run Command ─► scripts on the server
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
| `aws/frontend.tf`, `aws/www-redirect.js.tftpl` | S3 bucket, CloudFront, ACM certificate, 404 page, www. redirect |
| `aws/dns.tf` | the Route 53 zone, site and API records |
| `aws/dns_mail.tf` | email and Microsoft 365 records, copied from GoDaddy; change only when the mail provider asks |
| `aws/scheduler.tf` | the scheduler's role for running commands on the server |
| `aws/ses.tf` | SES: the sending domain, its DKIM records, the email-sender role the API container uses, bounce and complaint alerts |
| `aws/photos.tf` | the private, encrypted bucket for "I've been" photos, and the container's permission to store them and check them with Rekognition (CC-76) |
| `aws/discovery.tf` | event discovery: the API key's slot, the hourly checks, the Tuesday recap email |
| `aws/monitoring.tf` | log groups, alarms, failed-command alerts, monthly budget |
| `aws/github.tf` | OIDC deploy roles for both repos |

Rough cost while free-tier eligible: about $1–2/month out of pocket (Route 53 zone $0.50,
a Parameter Store/Secrets Manager secret $0.40; the server, database and public IPv4
address are covered). After the free tier: about $25/month, mostly RDS, EC2 and the
public IPv4 address ($3.60). Photos add Rekognition's $0.001 a photo (about $10 for 10,000) and a few
cents a month of S3 storage.

## First deployment

**Prerequisites:** `aws sso login --profile culture-crew` works, and
`export AWS_PROFILE=culture-crew` in the terminal you use.

### 1. State bucket (once)

```
cd infra/bootstrap
terraform init && terraform apply
```

### 2. The zone first

```
cd ../aws
cp terraform.tfvars.example terraform.tfvars        # put your email in it
terraform init -backend-config="bucket=culture-crew-tfstate-$(aws sts get-caller-identity --query Account --output text)"
terraform apply -target=aws_route53_zone.main
terraform output name_servers
```

A rebuilt zone gets **new name servers**. Set them at **GoDaddy → chiculturecrew.com →
Nameservers** before the next step; the certificate can't validate until the domain points
at this zone. (The first time round this was done with no downtime; see *How the domain
moved* below.)

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
  --parameters 'commands=["/usr/local/bin/culture-crew-deploy"]'
```

The deploy runs the database migrations. To rebuild with the events, restore the database from
an RDS snapshot. (The Google Sheet sync that used to load them was retired in CC-72; the server
still has a `culture-crew-sync` script from its first boot, which no longer works and isn't scheduled.)
`https://api.chiculturecrew.com/health` should answer `{"status":"ok"}`.

### 5. The page

From the cc-site repo:

```
aws s3 cp public/index.html s3://<site_bucket>/index.html --cache-control no-cache
aws s3 cp public/404.html s3://<site_bucket>/404.html --cache-control max-age=300
aws cloudfront create-invalidation --distribution-id <cloudfront_distribution_id> --paths '/*'
```

Then open https://chiculturecrew.com.

## Day to day

| Task | How |
|---|---|
| Deploy the API | merge to `main`; GitHub Actions builds, pushes and deploys (see the repo README) |
| Deploy by hand | push an image as in step 4, then `aws ssm send-command ... commands=["/usr/local/bin/culture-crew-deploy <tag>"]` |
| Roll back | `culture-crew-deploy <older tag>` (ECR keeps the 15 newest images, each tagged with its commit) |
| Shell on the server | `aws ssm start-session --target <api_instance_id>` |
| API logs | CloudWatch → `/culture-crew/api` (streams `api`, `caddy`) |
| Discovery and recap output | CloudWatch → `/culture-crew/discovery` |
| Send the weekly recap now | on the server: `docker exec api python -m scripts.recap` (`--print` to preview) |
| Look at the database | run the `db_tunnel_command` output, then connect a GUI to `localhost:5433` (user `culture_admin`, password from Secrets Manager) |
| Make an admin | on the server: `docker exec api python -m scripts.make_admin you@example.com` |
| Run event discovery now | on the server: `docker exec api python -m scripts.discover --source <id> --now` (without the API key it can't read changed pages) |
| Set the Anthropic API key for discovery | `aws ssm put-parameter --name /culture-crew/anthropic-api-key --type SecureString --overwrite --value '<key>'` |
| Plan dolphins on an account | on the server: `docker exec api python -m scripts.dolphins you@example.com` (`--off` to remove) |

## How the domain moved (October 2026)

The site was first staged on `aws.chiculturecrew.com`, delegated from GoDaddy with four NS
records, while Netlify kept serving the live site. The cutover then avoided any downtime
for the site or email:

1. **Copy the zone.** The GoDaddy zone export became `dns_mail.tf`. Every record was
   compared against GoDaddy's own answers, and later against Route 53's.
2. **Get certificates while GoDaddy was still authoritative.** ACM gives a domain the same
   validation CNAME every time within an account, so a throwaway certificate request
   revealed the records; they were added at GoDaddy along with `api` → the Elastic IP.
   The real certificate then validated straight away, and Caddy got its certificate too.
3. **Apply, then test the real names against CloudFront** (`curl --resolve`) before any
   visitor could reach them.
4. **Switch the nameservers at GoDaddy.** For up to 48 hours (the `.com` NS TTL) resolvers
   could get either side; both served working answers and identical email records.
5. **Afterwards, GoDaddy only registers the domain.** Its DNS page shows "DNS Provider:
   Amazon Route 53" and no records, so the staging and validation records added there are
   gone and nothing needs cleaning up (checked 2026-10-18). Route 53 is the only copy of
   the DNS, including the email records: `dns_mail.tf` is their source of truth.

## Tearing it down

**This takes the site and the domain's email offline** unless the nameservers go back to
GoDaddy first (`ns51`/`ns52.domaincontrol.com`), with the email records re-created there
(copy them from `dns_mail.tf`; GoDaddy no longer keeps a copy).
Then: set `db_deletion_protection = false` in `terraform.tfvars`, `terraform apply`, and
`terraform destroy`. RDS keeps a final snapshot (`culture-crew-db-final`), which you can
delete in the console.
