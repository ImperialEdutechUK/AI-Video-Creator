#!/usr/bin/env bash
# One-time setup of this project's own deploy identity, using an existing
# IAM-capable profile ONCE (plain IAM calls, no CloudFormation needed):
#
#   * IAM user  slc-video-aws-deploy-user  - can do nothing except assume the role
#   * IAM role  slc-video-aws-deployer     - permissions scoped to slc-video-aws-* resources
#   * local CLI profiles:
#       slc-video-aws-deploy-user  (the new user's access key, never printed)
#       slc-video-aws-deployer     (assumes the role; used by deploy.sh)
#
# After this, no other profile is used for this project.
#
#   bash aws/scripts/setup-deployer.sh <iam-capable-profile>
set -euo pipefail
export MSYS_NO_PATHCONV=1

ADMIN_PROFILE="${1:?Usage: setup-deployer.sh <iam-capable-profile>}"
PROJECT="${PROJECT:-slc-video-aws}"
REGION="${REGION:-eu-west-2}"
USER_NAME="$PROJECT-deploy-user"
ROLE_NAME="$PROJECT-deployer"
DOMAIN_NAME="${DOMAIN_NAME:-webtool.imperial.imet.lk}"  # only DNS name the role may change
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
iam() { aws iam --profile "$ADMIN_PROFILE" "$@"; }

ACCOUNT_ID="$(aws sts get-caller-identity --profile "$ADMIN_PROFILE" --query Account --output text)"
echo "Using $(aws sts get-caller-identity --profile "$ADMIN_PROFILE" --query Arn --output text) once to create the project identity"
TMP="aws/.build/setup-tmp"; mkdir -p "$TMP"; trap 'rm -rf "$TMP"' EXIT

# 1. Dedicated user that can only assume the deploy role.
if ! iam get-user --user-name "$USER_NAME" >/dev/null 2>&1; then
  iam create-user --user-name "$USER_NAME" --tags Key=Project,Value="$PROJECT" >/dev/null
  echo "Created user $USER_NAME"
fi
cat > "$TMP/user.json" <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"sts:AssumeRole","Resource":"arn:aws:iam::$ACCOUNT_ID:role/$ROLE_NAME"}]}
JSON
iam put-user-policy --user-name "$USER_NAME" --policy-name assume-deployer-role --policy-document "file://$TMP/user.json"

# 2. Deploy role trusted only by that user.
cat > "$TMP/trust.json" <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::$ACCOUNT_ID:user/$USER_NAME"},"Action":"sts:AssumeRole"}]}
JSON
sed -e "s/\${ACCOUNT_ID}/$ACCOUNT_ID/g" -e "s/\${PROJECT}/$PROJECT/g" -e "s/\${DOMAIN_NAME}/$DOMAIN_NAME/g" aws/infra/deployer-policy.json > "$TMP/role.json"
if iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "file://$TMP/trust.json"
else
  # IAM is eventually consistent: a brand-new user may not be accepted as a principal immediately.
  for i in 1 2 3 4 5 6; do
    iam create-role --role-name "$ROLE_NAME" --max-session-duration 3600 \
      --assume-role-policy-document "file://$TMP/trust.json" \
      --tags Key=Project,Value="$PROJECT" >/dev/null 2>"$TMP/err" && break
    [[ $i == 6 ]] && { cat "$TMP/err"; exit 1; }
    sleep 10
  done
  echo "Created role $ROLE_NAME"
fi
iam put-role-policy --role-name "$ROLE_NAME" --policy-name deploy-"$PROJECT" --policy-document "file://$TMP/role.json"

# 3. Access key for the new user -> local profile (written straight to the
#    AWS CLI credentials file, never echoed). Re-created only if the local
#    copy is missing or no longer matches an active key of this user.
LOCAL_KEY_ID="$(aws configure get aws_access_key_id --profile "$USER_NAME" 2>/dev/null | tr -d '\r' || true)"
IAM_KEY_IDS="$(iam list-access-keys --user-name "$USER_NAME" --query 'AccessKeyMetadata[].AccessKeyId' --output text | tr -d '\r')"
LOCAL_SECRET_SET="$(aws configure get aws_secret_access_key --profile "$USER_NAME" >/dev/null 2>&1 && echo yes || true)"
if [[ -z "$LOCAL_KEY_ID" || -z "$LOCAL_SECRET_SET" || " $IAM_KEY_IDS " != *" $LOCAL_KEY_ID "* ]]; then
  for old in $IAM_KEY_IDS; do  # unusable keys of THIS project user only
    iam delete-access-key --user-name "$USER_NAME" --access-key-id "$old"
  done
  read -r KEY_ID KEY_SECRET < <(iam create-access-key --user-name "$USER_NAME" \
    --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text | tr -d '\r')
  aws configure set aws_access_key_id "$KEY_ID" --profile "$USER_NAME"
  aws configure set aws_secret_access_key "$KEY_SECRET" --profile "$USER_NAME"
  unset KEY_ID KEY_SECRET
  echo "Stored access key for $USER_NAME in the local AWS credentials file"
fi
aws configure set region "$REGION" --profile "$USER_NAME"
aws configure set role_arn "arn:aws:iam::$ACCOUNT_ID:role/$ROLE_NAME" --profile "$ROLE_NAME"
aws configure set source_profile "$USER_NAME" --profile "$ROLE_NAME"
aws configure set region "$REGION" --profile "$ROLE_NAME"

echo "Waiting for the new credentials to become active..."
for i in $(seq 1 12); do
  if ARN="$(aws sts get-caller-identity --profile "$ROLE_NAME" --query Arn --output text 2>/dev/null)"; then
    echo "Ready: profile '$ROLE_NAME' -> $ARN"
    exit 0
  fi
  sleep 10
done
echo "The role could not be assumed yet; retry: aws sts get-caller-identity --profile $ROLE_NAME" >&2
exit 1
