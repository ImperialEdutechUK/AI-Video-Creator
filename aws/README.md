# SLC Video Merger — AWS deployment

A separate, low-cost AWS deployment of the SLC Video Merger. It runs **alongside** the
existing Railway/Vercel deployment and shares nothing with it:

- `backend/` and `frontend/` are untouched. Railway keeps deploying `main` exactly as before.
- Everything for AWS lives in this `aws/` folder, on the `aws-migration` branch.
- It uses its own AWS stacks (`slc-video-aws-*`), its own VPC, buckets and table, and a
  dedicated IAM deploy role.

Video processing uses the **unmodified** `backend/app/pipeline.py`, so output is identical
to Railway. The worker image copies `backend/app` as-is.

## Architecture

```
Browser ──► CloudFront ──► S3 (static Next.js export)            aws/frontend
   │
   ├─► Lambda Function URL  (API: same routes as Railway)         aws/api/handler.py
   │      ├─ DynamoDB on-demand   job state (survives restarts)
   │      ├─ pre-signed S3 POST   browser uploads straight to S3 (500 MB limit enforced by S3)
   │      ├─ ecs:RunTask          starts a worker only when a job is queued
   │      ├─ async self-invoke    "Save to Google Drive" (unmodified backend/app/drive.py)
   │      └─ every 1 min          reconciler: restarts interrupted jobs, expires reservations
   │
   └─► ECS Fargate Spot worker (8 vCPU / 16 GB, up to 2 at once)                    aws/worker/worker.py
          starts when an upload begins → claims queued jobs → pipeline.process_video → result to S3 → exits after 2 idle min
```

What it deliberately avoids, because each would cost money every month: NAT gateway,
load balancer, API Gateway, always-on servers, Secrets Manager and Elastic IPs.

## Behaviour compared with Railway

| | Railway | AWS |
|---|---|---|
| Endpoints, headers, limits, error messages | — | same |
| Per-browser job isolation, queue position, team queue cap | — | same |
| Upload path | multipart POST to the backend | pre-signed POST straight to S3, then `POST /jobs` (JSON) |
| Jobs after a restart or deploy | lost | kept (DynamoDB) |
| Worker interrupted mid-job | job lost | job requeued automatically (max 3 attempts) |
| First job after an idle period | starts immediately | worker boots (~50 s) while the video uploads, so little or no wait |
| Processing time (real 8-min lesson) | — | ~50–90 s on 8 vCPU (2 vCPU took ~3–5 min) |
| Videos processed at once | 1 (`WORKERS=1`) | up to 2 (`MaxWorkers`) |
| Finished videos not removed | kept until restart | deleted after 3 days (`OutputRetentionDays`) |
| Drive folder | production | **test folder only** until cutover (enforced by the API) |

## One-time setup

Requirements: AWS CLI v2, Docker, Node 18+, Python 3.12, Git Bash (on Windows).

### 1. Create the project's own deploy identity (done 2026-11-06)

```bash
bash aws/scripts/setup-deployer.sh <iam-capable-profile>
```

This uses an existing IAM-capable profile **once** (it was run with `openNoteBookLmUser`)
to create:

- IAM user `slc-video-aws-deploy-user`. Its only permission is assuming the role below.
  Its access key is written straight to the local AWS credentials file and is never printed.
- IAM role `slc-video-aws-deployer`, with permissions scoped to `slc-video-aws-*` resources
  (`aws/infra/deployer-policy.json`).
- Local CLI profiles `slc-video-aws-deploy-user` and `slc-video-aws-deployer`.

All later steps use only the `slc-video-aws-deployer` profile.

### 2. Deploy

```bash
bash aws/scripts/deploy.sh
# optional: BUDGET_EMAIL=you@example.org bash aws/scripts/deploy.sh
```

The script prints the website URL, API URL and health URL.

### 3. Google Drive settings (run this yourself; the values stay hidden)

Windows (Command Prompt or PowerShell):

```
powershell -ExecutionPolicy Bypass -File aws\scripts\set-drive-config.ps1
```

Git Bash / macOS / Linux: `bash aws/scripts/set-drive-config.sh`. Don't use the `bash`
command from a Windows Command Prompt: that's WSL, which has no AWS CLI or profiles.

Enter the same `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET` and `GDRIVE_REFRESH_TOKEN` as on
Railway, then the URL of a **separate test folder**. While
`ALLOW_PRODUCTION_DRIVE_FOLDER` is `false`, the API refuses to write to the production
folder. The Drive code only ever creates folders and uploads files; it never deletes anything.

### 4. Test checklist

1. Open `<API URL>/healthz`. It should show `"ok": true`, `"drive_auth_mode": "oauth"` and the test folder ID.
2. Open the website and upload one short real NotebookLM video.
3. Watch the job go Queued → Processing (the first one takes about 1–2 min to start) → Done.
4. Click View and Download, and compare the result with the same video processed on Railway.
5. Click Save to Google Drive and check the file appears in the **test** folder.
6. Upload 3 videos at once and check queue positions 1/2/3.

## Custom domain (webtool.imperial.imet.lk)

The `imperial.imet.lk` Route 53 hosted zone must be in this AWS account. Everything here is free:
the ACM certificate, CloudFront custom domain, and alias records.

```bash
DOMAIN_NAME=webtool.imperial.imet.lk HOSTED_ZONE_ID=<zone id> SKIP_IMAGE=1 IMAGE_TAG=<tag> bash aws/scripts/deploy.sh
```

This creates the certificate in us-east-1 (stack `slc-video-aws-certificate`, validated through
DNS automatically), adds the domain to CloudFront, creates A/AAAA alias records, and allows the
new origin in CORS. Later deploys keep the domain. The deploy role may change only the
`webtool.imperial.imet.lk` record and its `_*.webtool...` validation records.

## Cutover (only after testing succeeds)

The test folder is only for testing. After cutover, videos are saved exactly as on Railway
(same `drive.py`): `<production folder>/<Awarding Body> - <Course>/<Unit> | <Chapter>.mp4`.

1. `bash aws/scripts/set-drive-config.sh folder` and paste the **exact `GDRIVE_FOLDER_URL`
   value from Railway's variables**, so both systems save to the same place.
2. `ALLOW_PRODUCTION_DRIVE_FOLDER=true SKIP_IMAGE=1 IMAGE_TAG=<last tag> bash aws/scripts/deploy.sh`
   (the redeploy also makes the API reload the folder setting).
3. Open `<API URL>/healthz` and check `drive_root_folder_id` is the production folder. Then save
   one video and confirm it lands in the right course folder.
4. Share the new website URL with the team.
5. Keep Railway running until the team has used AWS successfully for a while (it is the
   fallback), then stop the Railway service and the Vercel site.

Later deploys keep the current `AllowProductionDriveFolder` and `BudgetEmail` values unless
you set them explicitly, so a routine redeploy never re-locks production or removes the budget alert.

Rollback at any point: the team goes back to the Railway/Vercel URL, which has kept running throughout.

## Updating

```bash
bash aws/scripts/deploy.sh                                      # everything
SKIP_IMAGE=1 IMAGE_TAG=<tag> bash aws/scripts/deploy.sh         # API/infra/frontend only
```

## Cost (eu-west-2 list prices, Nov 2026)

Measured on Railway: about 7.7 vCPU-minutes and about 46 MB of egress per video.

| Item | Cost |
|---|---|
| Fargate Spot worker (8 vCPU / 16 GB) incl. public IP while running | ≈ $0.004–0.005 per video (benchmark: CPU cost per video is nearly the same at 2, 4 or 8 vCPU) |
| Worker idle time (2 min after each batch) | ≈ $0.005 per batch |
| Lambda API + reconciler, DynamoDB on-demand, S3 requests | ≈ $0–0.60 per month |
| ECR image, temporary S3 storage, CloudFront, logs (7-day retention) | ≈ $0.10–0.40 per month |
| Data transfer out (downloads + Drive uploads) | free up to 100 GB/month (~2,100 videos) |

Typical totals: about $1.30 for a 230-video month, about $6 for 1,150 videos, and about $9
for the busiest month (~1,730 videos), around **$3.30 a month** on average. Spot capacity
is preferred; if it's unavailable the worker falls back to on-demand Fargate (about
$0.011 per video) so jobs still finish. Set `AllowOnDemandFallback=false` to forbid that.

## Teardown

```bash
P="--profile slc-video-aws-deployer --region eu-west-2"
aws s3 rm s3://slc-video-aws-media-<account-id> --recursive $P
aws s3 rm s3://slc-video-aws-site-<account-id> --recursive $P
aws cloudformation delete-stack --stack-name slc-video-aws $P
# after it finishes:
aws ecr delete-repository --repository-name slc-video-aws-worker --force $P
aws s3 rm s3://slc-video-aws-artifacts-<account-id> --recursive $P
aws cloudformation delete-stack --stack-name slc-video-aws-bootstrap $P
# finally, with an IAM-capable profile, remove the deploy identity:
A="--profile <iam-capable-profile>"
aws iam delete-role-policy --role-name slc-video-aws-deployer --policy-name deploy-slc-video-aws $A
aws iam delete-role --role-name slc-video-aws-deployer $A
aws iam list-access-keys --user-name slc-video-aws-deploy-user $A   # delete each key listed:
aws iam delete-access-key --user-name slc-video-aws-deploy-user --access-key-id <id> $A
aws iam delete-user-policy --user-name slc-video-aws-deploy-user --policy-name assume-deployer-role $A
aws iam delete-user --user-name slc-video-aws-deploy-user $A
```
