#!/usr/bin/env bash
# Store the Google Drive settings for the AWS deployment in SSM Parameter
# Store (SecureString, free tier). Run this YOURSELF in a terminal - the
# values are typed in hidden and are never printed or written to disk.
#
#   bash aws/scripts/set-drive-config.sh            # all four values
#   bash aws/scripts/set-drive-config.sh folder     # only change the folder
#
# Use the same GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET / GDRIVE_REFRESH_TOKEN
# values as the Railway service. For testing, the folder MUST be a separate
# test folder - the API refuses the production folder until cutover.
set -euo pipefail
export MSYS_NO_PATHCONV=1

PROJECT="${PROJECT:-slc-video-aws}"
REGION="${REGION:-eu-west-2}"
PROFILE="${PROFILE:-$PROJECT-deployer}"
PREFIX="/$PROJECT/gdrive"

put() {
  local name="$1" value="$2"
  aws ssm put-parameter --profile "$PROFILE" --region "$REGION" \
    --name "$PREFIX/$name" --type SecureString --overwrite \
    --value "$value" >/dev/null
  echo "  saved $PREFIX/$name"
}

ask_secret() {
  local prompt="$1" value=""
  read -r -s -p "$prompt: " value; echo >&2
  [[ -n "$value" ]] || { echo "Empty value - aborting." >&2; exit 1; }
  printf '%s' "$value"
}

if [[ "${1:-all}" != "folder" ]]; then
  put client_id     "$(ask_secret 'GDRIVE_CLIENT_ID')"
  put client_secret "$(ask_secret 'GDRIVE_CLIENT_SECRET')"
  put refresh_token "$(ask_secret 'GDRIVE_REFRESH_TOKEN')"
fi

read -r -p "Drive TEST folder URL (or ID): " FOLDER
[[ -n "$FOLDER" ]] || { echo "Empty value - aborting." >&2; exit 1; }
put folder_url "$FOLDER"

echo "Done. The API Lambda reads these on its next cold start (redeploy or wait ~15 min)."
