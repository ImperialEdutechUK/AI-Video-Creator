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

## Sheet automation (Excel log → NotebookLM → Drive)

Every 30 minutes, with nobody involved, the automation does the manual steps for each new row of the
"Chapterwise AI video log" tab:

1. **Reads the workbook** (Microsoft Graph) and takes rows that have a NotebookLM link in G, unit and
   chapter in D/E, and **empty H and I**. It writes `auto` in H straight away so people can see the row is taken.
   Rows with someone's name in H are never touched. Cell colours are ignored.
2. **Cleans the course name.** The awarding body is removed from the start (`Qualifi` + `Qualifi Level 03 …`
   → `Level 03 …`). Unit 14 / chapter 4 become `Unit 14` / `Chapter 4`, exactly as typed in the web app.
3. **Downloads the video** in a temporary cloud browser (Fargate Spot, headless Chromium) signed in as the
   content team's Google account. It opens the link, goes to the notebook (direct video links have no
   download option), finds the Studio item titled `U14C4`, and downloads it. If there is no match, or more
   than one *video* matches, nothing is guessed: the row becomes NO.
4. **Processes it** with the normal video worker (same pipeline as the web app) and **saves to Drive**
   (same `drive.py`, same folders and names). If that chapter is already in the course folder, it is not
   uploaded again.
5. **Checks Drive.** It re-reads the file by ID and checks the folder, the name, the size and the MD5
   against what was uploaded. Only then does it write **I = `Yes`**. Any failure writes **I = `NO`**.
   NO rows are retried automatically on later runs (3 attempts in all). To make the bot try again from
   scratch, clear the I cell.
6. **Cleans up.** The video's S3 copies are deleted as soon as it is verified in Drive. Leftovers expire on
   their own (uploads: 1 day; outputs: `OutputRetentionDays`; failure screenshots: 7 days).

Emails go to `AlertEmail`: "sign in again" alerts (at most every 12 h) and a daily summary at 16:00 UTC
with the rows marked Yes/NO and the reasons. No email is sent on days with no activity.
Cost: the 30-minute check is a few seconds of Lambda. The browser task (2 vCPU Spot) runs only when rows
are waiting. Expect roughly $1–3/month.

**Safety switches** (both default to test mode):
- `AllowProductionSheet=false`: the automation only writes to a workbook whose file name contains **TEST**.
- `AllowProductionDriveFolder=false` (existing): Drive saves can only go to the test folder.
- `AutomationEnabled=false`: nothing runs on a schedule until you turn it on.

### Setup (once)

0. **Deploy role:** give it the new SNS permission. Re-run
   `bash aws/scripts/setup-deployer.sh <iam-capable-profile>` (it only updates the role's policy).
1. **Entra app registration.** Someone allowed to register apps in the Microsoft 365 tenant does this:
   Entra admin centre → App registrations → New registration → name `SLC Video Automation`,
   *Accounts in this organizational directory only*, no redirect URI. Then:
   - Authentication → *Allow public client flows* = **Yes**.
   - API permissions → Microsoft Graph → Delegated: `Files.ReadWrite.All`, `offline_access`,
     `User.Read` (none of these normally needs admin consent; if the tenant blocks user consent, an admin
     clicks *Grant admin consent*).
   - Note the **Application (client) ID** and **Directory (tenant) ID**.
2. **Test copy of the log.** In OneDrive: *Save a copy* of the workbook named e.g.
   `TEST - Chapterwise AI video log`, and copy its sharing link. Remove most old rows, or use the start
   date below, so testing only processes a few videos.
3. **Separate test Drive folder.** Create a new empty folder, e.g. `Automation TEST`, in the Drive
   account the app uploads with, then point the deployment at it:
   `powershell -ExecutionPolicy Bypass -File aws\scripts\set-drive-config.ps1` (or
   `bash aws/scripts/set-drive-config.sh folder`).
4. **Deploy** (Docker Desktop must be running). The automation stays switched off:
   `ALERT_EMAIL=<team address> bash aws/scripts/deploy.sh`
   The alert address receives an "AWS Notification - Subscription Confirmation" email. Click *Confirm*.
5. **Connect Excel:** `py -3.12 aws\scripts\automation_microsoft_signin.py`. Enter the IDs from step 1,
   the TEST copy's link and an optional start date, then sign in at microsoft.com/devicelogin with an
   account that can edit the copy. It then lists, read-only, the rows it would take and their cleaned
   course names. Check them.
6. **Connect Google:** `py -3.12 aws\scripts\automation_google_signin.py`. Chrome opens with an empty
   profile; sign in to the content team account and wait for NotebookLM to list the notebooks, then press
   Enter. **Don't click "Sign out"** in that window afterwards; just close it.
   Then try the real download steps on this PC with a row you know:
   `py -3.12 aws\scripts\automation_google_signin.py --skip-signin --probe <link from G> --unit 14 --chapter 4`

### Test

```
py -3.12 aws\scripts\automation_admin.py dry-run   # what it WOULD take; writes nothing
AUTOMATION_ENABLED=true SKIP_IMAGE=1 IMAGE_TAG=<worker tag> bash aws/scripts/deploy.sh
py -3.12 aws\scripts\automation_admin.py run-now   # don't wait for the 30-minute schedule
py -3.12 aws\scripts\automation_admin.py status    # every row's progress and failure reason
```

Check that H shows `auto` within a minute, I shows `Yes` after processing (about 3–6 min per video), and the
files are in the **test** Drive folder as `<Awarding Body> - <Course>/Unit X | Chapter Y.mp4`. Also try
the failure paths: a wrong unit number (→ NO with "No Studio item titled …"), and a
row with a person's name in H (→ untouched).

### Cutover to the real log (only after the test passes)

1. `py -3.12 aws\scripts\automation_microsoft_signin.py` again. This time enter the **real** workbook link
   and a **start date** (e.g. today), so old rows with empty H/I aren't all processed at once.
2. Do the Drive cutover above (production folder + `ALLOW_PRODUCTION_DRIVE_FOLDER=true`).
3. `ALLOW_PRODUCTION_SHEET=true SKIP_IMAGE=1 IMAGE_TAG=<tag> bash aws/scripts/deploy.sh`

**Switch off at any time:** `AUTOMATION_ENABLED=false SKIP_IMAGE=1 IMAGE_TAG=<tag> bash aws/scripts/deploy.sh`.
Rows already in H=`auto` with empty I can then be finished by hand: put your name in H.

**When Google or Microsoft ask for a new sign-in** (the email says which), re-run step 5 or 6. Waiting rows
continue on the next run, and nothing is lost.

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
