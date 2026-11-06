#!/usr/bin/env bash
# Build and deploy the whole AWS stack using ONLY the dedicated deployer
# profile created by setup-deployer.sh.
#
#   bash aws/scripts/deploy.sh
#
# Optional environment variables:
#   SKIP_IMAGE=1                      reuse IMAGE_TAG instead of rebuilding the worker
#   IMAGE_TAG=<tag>                   worker image tag to deploy (with SKIP_IMAGE=1)
#   SKIP_FRONTEND=1                   do not rebuild/upload the website
#   BUDGET_EMAIL=you@example.com      enable the monthly cost alert
#   ALLOW_PRODUCTION_DRIVE_FOLDER=true  ONLY at cutover (default: keep current value)
#   WORKER_CPU / WORKER_MEMORY        worker size (default 8192 / 16384 = 8 vCPU / 16 GB)
#   PIPELINE_PREP_WORKERS / MAX_WORKERS  (default 3 / 2)
# (CloudFormation keeps a stack's previous parameter values unless they are
#  passed, so the worker settings are always passed explicitly here.)
set -euo pipefail
export MSYS_NO_PATHCONV=1

PROJECT="${PROJECT:-slc-video-aws}"
REGION="${REGION:-eu-west-2}"
PROFILE="${PROFILE:-$PROJECT-deployer}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if command -v py >/dev/null 2>&1; then PY=(py -3.12); else PY=(python3); fi
aws_() { aws --profile "$PROFILE" --region "$REGION" "$@"; }
output() { aws_ cloudformation describe-stacks --stack-name "$1" --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text; }
step() { echo; echo "==> $*"; }
# Value a parameter has on the deployed stack ("" if the stack doesn't exist yet).
current_param() { aws_ cloudformation describe-stacks --stack-name "$PROJECT" --query "Stacks[0].Parameters[?ParameterKey=='$1'].ParameterValue" --output text 2>/dev/null | tr -d '\r' | grep -v '^None$' || true; }

# Settings keep their current deployed value unless explicitly given, so a
# routine redeploy can never re-lock the production Drive folder after
# cutover or silently remove the budget alert.
BUDGET_EMAIL="${BUDGET_EMAIL-$(current_param BudgetEmail)}"
ALLOW_PROD_DRIVE="${ALLOW_PRODUCTION_DRIVE_FOLDER:-$(current_param AllowProductionDriveFolder)}"
ALLOW_PROD_DRIVE="${ALLOW_PROD_DRIVE:-false}"
DOMAIN_NAME="${DOMAIN_NAME-$(current_param DomainName)}"
HOSTED_ZONE_ID="${HOSTED_ZONE_ID-$(current_param HostedZoneId)}"

step "Deploying as: $(aws_ sts get-caller-identity --query Arn --output text)"

# ECS needs its service-linked role once per account (first ECS use).
# (The deployer role cannot read roles outside the project, so just try to
# create it and treat "already taken" as success.)
if SLR_OUT="$(aws_ iam create-service-linked-role --aws-service-name ecs.amazonaws.com 2>&1)"; then
  step "Created the ECS service-linked role (first ECS use in this account)"
  sleep 15
elif [[ "$SLR_OUT" != *"has been taken"* ]]; then
  echo "$SLR_OUT" >&2
  exit 1
fi

# A first deploy that failed leaves an empty ROLLBACK_COMPLETE stack, which
# cannot be updated - remove it so the deploy can be retried.
STATUS="$(aws_ cloudformation describe-stacks --stack-name "$PROJECT" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
if [[ "$STATUS" == "ROLLBACK_COMPLETE" ]]; then
  step "Removing empty failed stack $PROJECT"
  aws_ cloudformation delete-stack --stack-name "$PROJECT"
  aws_ cloudformation wait stack-delete-complete --stack-name "$PROJECT"
fi

step "Bootstrap stack (ECR repository + artifacts bucket)"
aws_ cloudformation deploy --stack-name "$PROJECT-bootstrap" \
  --template-file aws/infra/bootstrap.yaml \
  --parameter-overrides ProjectName="$PROJECT" \
  --tags Project="$PROJECT" --no-fail-on-empty-changeset
CERT_ARN=""
if [[ -n "$DOMAIN_NAME" ]]; then
  [[ -n "$HOSTED_ZONE_ID" ]] || { echo "HOSTED_ZONE_ID is required with DOMAIN_NAME" >&2; exit 1; }
  step "TLS certificate for $DOMAIN_NAME (us-east-1, DNS-validated, free)"
  aws --profile "$PROFILE" --region us-east-1 cloudformation deploy --stack-name "$PROJECT-certificate" \
    --template-file aws/infra/certificate.yaml \
    --parameter-overrides DomainName="$DOMAIN_NAME" HostedZoneId="$HOSTED_ZONE_ID" \
    --tags Project="$PROJECT" --no-fail-on-empty-changeset
  CERT_ARN="$(aws --profile "$PROFILE" --region us-east-1 cloudformation describe-stacks --stack-name "$PROJECT-certificate" \
    --query "Stacks[0].Outputs[?OutputKey=='CertificateArn'].OutputValue" --output text | tr -d '\r')"
fi

REPO_URI="$(output "$PROJECT-bootstrap" WorkerRepositoryUri)"
ARTIFACTS="$(output "$PROJECT-bootstrap" ArtifactsBucketName)"

if [[ "${SKIP_IMAGE:-0}" == "1" ]]; then
  TAG="${IMAGE_TAG:?Set IMAGE_TAG when SKIP_IMAGE=1}"
  step "Reusing worker image $REPO_URI:$TAG"
else
  TAG="$(git rev-parse --short HEAD)-$(date +%Y%m%d%H%M%S)"
  step "Building worker image $REPO_URI:$TAG"
  docker build --platform linux/amd64 -f aws/worker/Dockerfile -t "$REPO_URI:$TAG" .
  aws_ ecr get-login-password | docker login --username AWS --password-stdin "${REPO_URI%%/*}"
  docker push "$REPO_URI:$TAG"
fi

step "Packaging API Lambda"
rm -rf aws/api/build aws/.build
mkdir -p aws/api/build aws/.build
"${PY[@]}" -m pip install -q -r aws/api/requirements.txt -t aws/api/build \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 \
  --only-binary=:all: --upgrade --no-warn-conflicts
cp aws/api/handler.py aws/api/build/handler.py
cp backend/app/drive.py aws/api/build/drive.py   # unmodified Drive logic
# Keep only the Drive discovery document (saves ~90 MB and cold-start time).
find aws/api/build/googleapiclient/discovery_cache/documents -type f ! -name 'drive.v3.json' -delete
find aws/api/build -name '__pycache__' -type d -prune -exec rm -rf {} +

aws_ cloudformation package \
  --template-file aws/infra/template.yaml \
  --s3-bucket "$ARTIFACTS" --s3-prefix api \
  --output-template-file aws/.build/packaged.yaml >/dev/null

step "Application stack"
aws_ cloudformation deploy --stack-name "$PROJECT" \
  --template-file aws/.build/packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ProjectName="$PROJECT" \
    WorkerImageUri="$REPO_URI:$TAG" \
    AllowProductionDriveFolder="$ALLOW_PROD_DRIVE" \
    BudgetEmail="$BUDGET_EMAIL" \
    WorkerCpu="${WORKER_CPU:-8192}" \
    WorkerMemory="${WORKER_MEMORY:-16384}" \
    PipelinePrepWorkers="${PIPELINE_PREP_WORKERS:-3}" \
    MaxWorkers="${MAX_WORKERS:-2}" \
    DomainName="$DOMAIN_NAME" \
    HostedZoneId="$HOSTED_ZONE_ID" \
    CertificateArn="$CERT_ARN" \
  --tags Project="$PROJECT" --no-fail-on-empty-changeset

API_URL="$(output "$PROJECT" ApiUrl)"
SITE_URL="$(output "$PROJECT" SiteUrl)"
SITE_BUCKET="$(output "$PROJECT" SiteBucketName)"
DIST_ID="$(output "$PROJECT" DistributionId)"

if [[ "${SKIP_FRONTEND:-0}" != "1" ]]; then
  step "Building and uploading the website"
  (cd aws/frontend && npm ci --no-audit --no-fund && NEXT_PUBLIC_API_URL="${API_URL%/}" npx next build)
  # Hashed assets can be cached forever; HTML must always be revalidated.
  aws_ s3 sync aws/frontend/out "s3://$SITE_BUCKET" --delete \
    --exclude "*.html" --exclude "*.txt" \
    --cache-control "public,max-age=31536000,immutable"
  aws_ s3 sync aws/frontend/out "s3://$SITE_BUCKET" \
    --exclude "*" --include "*.html" --include "*.txt" \
    --cache-control "no-cache"
  aws_ cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" >/dev/null
fi

step "Done"
echo "Website : $SITE_URL"
echo "API     : $API_URL"
echo "Health  : ${API_URL%/}/healthz"
echo "Worker image tag: $TAG"
