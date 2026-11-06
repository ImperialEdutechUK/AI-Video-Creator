# Store the Google Drive settings for the AWS deployment in SSM Parameter
# Store (SecureString). Windows/PowerShell version of set-drive-config.sh.
# Secrets are typed in hidden and are never printed or written to disk.
#
#   powershell -ExecutionPolicy Bypass -File aws\scripts\set-drive-config.ps1
#   powershell -ExecutionPolicy Bypass -File aws\scripts\set-drive-config.ps1 folder   # only change the folder
param([string]$Mode = "all")
$ErrorActionPreference = "Stop"

$Project = "slc-video-aws"
$Region  = "eu-west-2"
$AwsProfile = "$Project-deployer"
$Prefix  = "/$Project/gdrive"

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Write-Error "AWS CLI not found on PATH."
}

function Put-Param([string]$Name, [string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "Empty value for $Name - aborting." }
    & aws ssm put-parameter --profile $AwsProfile --region $Region `
        --name "$Prefix/$Name" --type SecureString --overwrite --value $Value.Trim() | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Saving $Prefix/$Name failed." }
    Write-Host "  saved $Prefix/$Name"
}

function Read-Secret([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

if ($Mode -ne "folder") {
    Put-Param "client_id"     (Read-Secret "GDRIVE_CLIENT_ID")
    Put-Param "client_secret" (Read-Secret "GDRIVE_CLIENT_SECRET")
    Put-Param "refresh_token" (Read-Secret "GDRIVE_REFRESH_TOKEN")
}
Put-Param "folder_url" (Read-Host -Prompt "Drive folder URL (or ID) - use the TEST folder while testing")

Write-Host "Done. Tell Claude so it can restart the API to load the new settings."
