#!/usr/bin/env python3
"""
AWS Lambda (Function URL) port of backend/app/main.py.

Same endpoints, request headers, response shapes, limits and error messages
as the Railway FastAPI service, with three infrastructure substitutions:

  * Job state lives in DynamoDB instead of process memory (survives restarts).
  * The browser uploads the video straight to S3 using a pre-signed POST that
    the preflight call returns (Lambda request bodies are capped at 6 MB).
    POST /jobs then registers the uploaded object as a queued job.
  * Video processing runs in an ECS Fargate Spot task that is started on
    demand (see ../worker/worker.py) instead of an in-process thread.

The same function also handles two non-HTTP events:
  {"action": "drive_save", "job_id": ...}  async "Save to Google Drive"
  {"action": "reconcile"}                 1-minute schedule: requeue jobs from
                                          dead workers, expire reservations,
                                          make sure a worker runs if needed.
"""
import base64
import json
import logging
import os
import re
import time
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "eu-west-2")
TABLE_NAME = os.environ["JOBS_TABLE"]
BUCKET = os.environ["MEDIA_BUCKET"]
CLUSTER = os.environ["WORKER_CLUSTER"]
TASK_FAMILY = os.environ["WORKER_TASK_FAMILY"]
SUBNETS = [s for s in os.environ.get("WORKER_SUBNETS", "").split(",") if s]
SECURITY_GROUP = os.environ.get("WORKER_SECURITY_GROUP", "")
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/slc-video-aws").rstrip("/")
FUNCTION_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")

# ── Limits (same names/defaults as the Railway service) ──────────────────
MAX_UPLOAD_MB = max(1, int(os.environ.get("MAX_UPLOAD_MB", "500")))
MAX_QUEUED_JOBS = max(1, int(os.environ.get("MAX_QUEUED_JOBS", "20")))
MAX_JOBS_PER_SESSION = max(1, int(os.environ.get("MAX_JOBS_PER_SESSION", "5")))
UPLOAD_RESERVATION_TTL_SECONDS = max(60, int(os.environ.get("UPLOAD_RESERVATION_TTL_SECONDS", "3600")))
WORKERS = max(1, int(os.environ.get("WORKERS", "1")))
OUTPUT_RETENTION_DAYS = max(1, int(os.environ.get("OUTPUT_RETENTION_DAYS", "3")))
STALE_HEARTBEAT_SECONDS = max(60, int(os.environ.get("STALE_HEARTBEAT_SECONDS", "180")))
MAX_ATTEMPTS = max(1, int(os.environ.get("MAX_ATTEMPTS", "3")))
ALLOW_ON_DEMAND_FALLBACK = os.environ.get("ALLOW_ON_DEMAND_FALLBACK", "true").lower() == "true"
ALLOW_PRODUCTION_DRIVE_FOLDER = os.environ.get("ALLOW_PRODUCTION_DRIVE_FOLDER", "false").lower() == "true"
PRODUCTION_DRIVE_FOLDER_ID = "1cY7v7956TyrJbGPGno4QQJ5Zpj6bXDjZ"
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")

ddb = boto3.resource("dynamodb", region_name=REGION)
table = ddb.Table(TABLE_NAME)
ddb_client = boto3.client("dynamodb", region_name=REGION)
s3 = boto3.client(
    "s3", region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
ecs = boto3.client("ecs", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)


class HTTPError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _now() -> Decimal:
    return Decimal(str(round(time.time(), 3)))


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _response(status: int, body=None, headers: dict | None = None) -> dict:
    out = {"statusCode": status, "headers": {"Content-Type": "application/json", **(headers or {})}}
    out["body"] = "" if body is None else json.dumps(body, default=_json_default)
    return out


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(type(o).__name__)


def _require_session_id(value: str | None) -> str:
    session_id = (value or "").strip()
    if not _SESSION_RE.fullmatch(session_id):
        raise HTTPError(400, "A valid browser session id is required. Refresh the page and try again.")
    return session_id


# ── shared queue view ─────────────────────────────────────────────────────

def _active_items() -> list[dict]:
    """Everything that counts against queue limits: queued + processing jobs
    and unexpired upload reservations. Served by a small sparse index."""
    items, kwargs = [], {}
    while True:
        resp = table.query(
            IndexName="active",
            KeyConditionExpression=Key("active").eq("1"),
            **kwargs,
        )
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    cutoff = time.time() - UPLOAD_RESERVATION_TTL_SECONDS
    return [
        i for i in items
        if not (i.get("qstate") == "reserved" and _num(i.get("created_at")) < cutoff)
    ]


def _queue_summary(active: list[dict], session_id: str | None = None) -> dict:
    queued = [i for i in active if i.get("qstate") == "queued"]
    queued.sort(key=lambda i: (_num(i.get("created_at")), i["pk"]))
    positions = {i["pk"][4:]: n + 1 for n, i in enumerate(queued)}
    summary = {
        "queued": len(queued),
        "processing": sum(1 for i in active if i.get("qstate") == "processing"),
        "uploading": sum(1 for i in active if i.get("qstate") == "reserved"),
        "positions": positions,
    }
    if session_id is not None:
        summary["session_active"] = sum(1 for i in active if i.get("sid") == session_id)
    return summary


# ── job helpers ───────────────────────────────────────────────────────────

def _get_job(job_id: str) -> dict | None:
    if not _ID_RE.fullmatch(job_id or ""):
        return None
    return table.get_item(Key={"pk": f"JOB#{job_id}"}, ConsistentRead=True).get("Item")


def _owned_job(job_id: str, session_id: str) -> dict:
    job = _get_job(job_id)
    if not job or job.get("sid") != session_id:
        # 404 rather than 403 so one session cannot probe another's job ids.
        raise HTTPError(404, "Job not found")
    return job


def _public_job(job: dict, queue_position: int | None = None) -> dict:
    status = job["status"]
    return {
        "job_id": job["job_id"],
        "status": status,
        "queue_position": queue_position if status == "queued" else None,
        "progress": list(job.get("progress", []))[-20:],
        "error": job.get("error"),
        "result_filename": job.get("result_filename"),
        "original_filename": job.get("original_filename"),
        "course_name": job.get("course_name"),
        "unit_number": job.get("unit_number"),
        "chapter_number": job.get("chapter_number", ""),
        "awarding_body": job.get("awarding_body", ""),
        "created_at": job.get("created_at"),
        "drive_status": job.get("drive_status", "idle"),
        "drive_error": job.get("drive_error"),
        "drive_link": job.get("drive_link"),
    }


# ── worker launching ──────────────────────────────────────────────────────

def _acquire_launch_lock(seconds: int = 45) -> bool:
    """Stops two simultaneous requests both starting a worker."""
    now = int(time.time())
    try:
        table.put_item(
            Item={"pk": "LOCK#launch", "lock_until": now + seconds, "ttl": now + 3600},
            ConditionExpression="attribute_not_exists(pk) OR lock_until < :now",
            ExpressionAttributeValues={":now": now},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _run_worker_tasks(count: int) -> int:
    base = {
        "cluster": CLUSTER,
        "taskDefinition": TASK_FAMILY,
        "count": count,
        "startedBy": "slc-video-api",
        "propagateTags": "TASK_DEFINITION",
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": SUBNETS,
                "securityGroups": [SECURITY_GROUP],
                "assignPublicIp": "ENABLED",
            }
        },
    }
    started = 0
    try:
        resp = ecs.run_task(capacityProviderStrategy=[{"capacityProvider": "FARGATE_SPOT", "weight": 1}], **base)
        started = len(resp.get("tasks", []))
        if resp.get("failures"):
            log.warning("Fargate Spot launch failures: %s", resp["failures"])
    except ClientError:
        log.exception("Fargate Spot launch failed")
    if started < count and ALLOW_ON_DEMAND_FALLBACK:
        base["count"] = count - started
        resp = ecs.run_task(capacityProviderStrategy=[{"capacityProvider": "FARGATE", "weight": 1}], **base)
        started += len(resp.get("tasks", []))
        if resp.get("failures"):
            log.error("Fargate on-demand launch failures: %s", resp["failures"])
    return started


def ensure_worker(active: list[dict] | None = None) -> None:
    """Start worker tasks for queued work. Uploads in progress (reservations)
    count as demand too, so a worker boots while the video is still uploading
    instead of after it arrives (Fargate start-up is ~50 s)."""
    if active is None:
        active = _active_items()
    queued = sum(1 for i in active if i.get("qstate") == "queued")
    uploading = sum(1 for i in active if i.get("qstate") == "reserved")
    if queued + uploading == 0:
        return
    processing = sum(1 for i in active if i.get("qstate") == "processing")
    wanted = min(WORKERS, queued + processing + uploading)
    running = ecs.list_tasks(cluster=CLUSTER, family=TASK_FAMILY, desiredStatus="RUNNING").get("taskArns", [])
    missing = wanted - len(running)
    if missing <= 0 or not _acquire_launch_lock():
        return
    started = _run_worker_tasks(missing)
    log.info("Started %s worker task(s) (queued=%s processing=%s running=%s)", started, queued, processing, len(running))


# ── routes ────────────────────────────────────────────────────────────────

def healthz(_req) -> dict:
    active = _active_items()
    summary = _queue_summary(active)
    try:
        drive_mode, drive_folder = _drive_module().auth_mode(), _drive_root_folder_id()
    except Exception as e:  # a Drive problem must not take the health check down
        drive_mode, drive_folder = f"error: {e.__class__.__name__}", ""
    return _response(200, {
        "ok": True,
        "platform": "aws",
        "workers": WORKERS,
        "queue_depth": summary["queued"],
        "processing": summary["processing"],
        "uploading": summary["uploading"],
        "max_queued_jobs": MAX_QUEUED_JOBS,
        "max_jobs_per_session": MAX_JOBS_PER_SESSION,
        "max_upload_mb": MAX_UPLOAD_MB,
        "upload_reservation_ttl_seconds": UPLOAD_RESERVATION_TTL_SECONDS,
        "output_retention_days": OUTPUT_RETENTION_DAYS,
        "drive_auth_mode": drive_mode,
        "drive_root_folder_id": drive_folder,
    })


def preflight_upload(req) -> dict:
    session_id = _require_session_id(req["headers"].get("x-session-id"))
    q = req["query"]
    try:
        file_size = int(q.get("file_size", ""))
    except ValueError:
        raise HTTPError(422, "file_size must be a positive integer")
    filename = (q.get("filename") or "").strip()
    if file_size < 1 or not filename:
        raise HTTPError(422, "file_size and filename are required")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPError(415, "Unsupported video type. Use MP4, MOV, WebM, MKV or AVI.")
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    if file_size > max_bytes:
        raise HTTPError(413, f"File too large ({file_size / 1048576:.0f} MB). Limit is {MAX_UPLOAD_MB} MB.")

    active = _active_items()
    summary = _queue_summary(active, session_id)
    if summary["queued"] + summary["uploading"] >= MAX_QUEUED_JOBS:
        raise HTTPError(
            429,
            f"The team queue is full ({MAX_QUEUED_JOBS} waiting uploads). Please try again after a queued video starts processing.",
        )
    if summary["session_active"] >= MAX_JOBS_PER_SESSION:
        raise HTTPError(
            429,
            f"This browser already has {summary['session_active']} active video job(s). The per-browser limit is {MAX_JOBS_PER_SESSION}.",
        )

    token = uuid.uuid4().hex
    upload_key = f"uploads/{token}/source{suffix}"
    now = time.time()
    reservation = {
        "pk": f"RES#{token}",
        "kind": "reservation",
        "sid": session_id,
        "active": "1",
        "qstate": "reserved",
        "created_at": _now(),
        "declared_size": file_size,
        "filename": filename[:255],
        "upload_key": upload_key,
        "ttl": int(now) + UPLOAD_RESERVATION_TTL_SECONDS + 86400,
    }
    table.put_item(Item=reservation)
    try:
        # Pre-warm: boot a worker now so it is ready when the upload finishes.
        ensure_worker(active + [reservation])
    except Exception:
        log.exception("Could not pre-warm a worker")
    # The size condition is enforced by S3 itself, so the 500 MB limit still
    # holds even though the bytes no longer pass through our code.
    post = s3.generate_presigned_post(
        Bucket=BUCKET,
        Key=upload_key,
        Conditions=[["content-length-range", 1, max_bytes]],
        ExpiresIn=UPLOAD_RESERVATION_TTL_SECONDS,
    )
    return _response(200, {
        "reservation_token": token,
        "expires_in_seconds": UPLOAD_RESERVATION_TTL_SECONDS,
        "upload": {"url": post["url"], "fields": post["fields"]},
    })


def cancel_preflight(req, token: str) -> dict:
    session_id = _require_session_id(req["headers"].get("x-session-id"))
    if not _ID_RE.fullmatch(token or ""):
        raise HTTPError(404, "Upload reservation not found")
    item = table.get_item(Key={"pk": f"RES#{token}"}, ConsistentRead=True).get("Item")
    if not item or item.get("sid") != session_id:
        raise HTTPError(404, "Upload reservation not found")
    table.delete_item(Key={"pk": f"RES#{token}"})
    try:
        s3.delete_object(Bucket=BUCKET, Key=item["upload_key"])
    except ClientError:
        pass
    return _response(200, {"cancelled": True})


def create_job(req) -> dict:
    session_id = _require_session_id(req["headers"].get("x-session-id"))
    body = req["json"]
    course_name = body.get("course_name")
    unit_number = body.get("unit_number")
    if not isinstance(course_name, str) or not isinstance(unit_number, str):
        raise HTTPError(422, "course_name and unit_number are required")
    chapter_number = str(body.get("chapter_number") or "")
    awarding_body = str(body.get("awarding_body") or "")
    filename = str(body.get("filename") or "")
    if not filename:
        raise HTTPError(400, "No file uploaded")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPError(415, "Unsupported video type. Use MP4, MOV, WebM, MKV or AVI.")

    token = (req["headers"].get("x-upload-reservation") or "").strip()
    if not token:
        raise HTTPError(428, "Upload preflight is required. Refresh the page and retry the upload.")
    if not _ID_RE.fullmatch(token):
        raise HTTPError(409, "Upload reservation is missing, expired or already used. Please retry the upload.")
    res = table.get_item(Key={"pk": f"RES#{token}"}, ConsistentRead=True).get("Item")
    expired = res is not None and _num(res.get("created_at")) < time.time() - UPLOAD_RESERVATION_TTL_SECONDS
    if not res or res.get("sid") != session_id or expired:
        raise HTTPError(409, "Upload reservation is missing, expired or already used. Please retry the upload.")
    if Path(filename).suffix.lower() != Path(str(res.get("filename", ""))).suffix.lower():
        raise HTTPError(409, "The selected file changed after upload preflight. Please retry.")

    upload_key = res["upload_key"]
    try:
        head = s3.head_object(Bucket=BUCKET, Key=upload_key)
    except ClientError:
        raise HTTPError(409, "The uploaded video was not found. Please retry the upload.")
    total_bytes = int(head.get("ContentLength", 0))
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    if total_bytes == 0:
        raise HTTPError(400, "Uploaded video is empty")
    if total_bytes > max_bytes:
        raise HTTPError(413, f"File too large ({total_bytes / 1048576:.0f} MB read). Limit is {MAX_UPLOAD_MB} MB.")
    declared = int(res.get("declared_size", 0) or 0)
    if declared and declared != total_bytes:
        raise HTTPError(409, "The selected file changed after upload preflight. Please retry.")

    job_id = uuid.uuid4().hex[:12]
    job = {
        "pk": f"JOB#{job_id}",
        "kind": "job",
        "job_id": job_id,
        "sid": session_id,
        "status": "queued",
        "active": "1",
        "qstate": "queued",
        "progress": [],
        "course_name": course_name,
        "unit_number": unit_number,
        "chapter_number": chapter_number,
        "awarding_body": awarding_body,
        "original_filename": filename[:255],
        "created_at": _now(),
        "upload_size_bytes": total_bytes,
        "upload_key": upload_key,
        "drive_status": "idle",  # idle | saving | saved | failed
    }
    from boto3.dynamodb.types import TypeSerializer
    ser = TypeSerializer()
    try:
        ddb_client.transact_write_items(TransactItems=[
            {"Delete": {
                "TableName": TABLE_NAME,
                "Key": {"pk": {"S": f"RES#{token}"}},
                "ConditionExpression": "sid = :sid",
                "ExpressionAttributeValues": {":sid": {"S": session_id}},
            }},
            {"Put": {
                "TableName": TABLE_NAME,
                "Item": {k: ser.serialize(v) for k, v in job.items()},
                "ConditionExpression": "attribute_not_exists(pk)",
            }},
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            raise HTTPError(409, "Upload reservation is missing, expired or already used. Please retry the upload.")
        raise

    try:
        ensure_worker()
    except Exception:
        # The 1-minute reconciler will start the worker if this fails.
        log.exception("Could not start a worker right away")
    return _response(200, {"job_id": job_id})


def list_jobs(req) -> dict:
    session_id = _require_session_id(req["headers"].get("x-session-id"))
    active = _active_items()
    summary = _queue_summary(active, session_id)
    items, kwargs = [], {}
    while True:
        resp = table.query(
            IndexName="by_session",
            KeyConditionExpression=Key("sid").eq(session_id),
            ScanIndexForward=False,
            **kwargs,
        )
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    jobs = [
        _public_job(i, summary["positions"].get(i["job_id"]))
        for i in items if i.get("kind") == "job"
    ]
    jobs.sort(key=lambda j: _num(j["created_at"]), reverse=True)
    return _response(200, {
        "jobs": jobs,
        "queue": {
            "queued": summary["queued"],
            "processing": summary["processing"],
            "uploading": summary["uploading"],
            "capacity": MAX_QUEUED_JOBS,
            "session_active": summary["session_active"],
            "session_limit": MAX_JOBS_PER_SESSION,
        },
    })


def get_job(req, job_id: str) -> dict:
    session_id = _require_session_id(req["headers"].get("x-session-id"))
    job = _owned_job(job_id, session_id)
    position = None
    if job["status"] == "queued":
        position = _queue_summary(_active_items())["positions"].get(job_id)
    return _response(200, _public_job(job, position))


def get_job_file(req, job_id: str) -> dict:
    session_id = _require_session_id(req["headers"].get("x-session-id") or req["query"].get("session_id"))
    job = _owned_job(job_id, session_id)
    if job["status"] != "done" or not job.get("result_key"):
        raise HTTPError(409, f"Job is not finished (status: {job['status']})")
    filename = job.get("result_filename") or "video.mp4"
    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET,
            "Key": job["result_key"],
            "ResponseContentType": "video/mp4",
            "ResponseContentDisposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
        },
        ExpiresIn=3600,
    )
    # Redirect so both the Download link and the <video> preview read the
    # file directly from S3 instead of streaming it through Lambda.
    return {"statusCode": 302, "headers": {"Location": url, "Cache-Control": "no-store"}, "body": ""}


def save_to_drive(req, job_id: str) -> dict:
    session_id = _require_session_id(req["headers"].get("x-session-id"))
    job = _owned_job(job_id, session_id)
    if job["status"] != "done" or not job.get("result_key"):
        raise HTTPError(409, f"Job is not finished (status: {job['status']})")
    try:
        table.update_item(
            Key={"pk": f"JOB#{job_id}"},
            UpdateExpression="SET drive_status = :saving REMOVE drive_error",
            ConditionExpression="drive_status <> :saving",
            ExpressionAttributeValues={":saving": "saving"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPError(409, "Already saving to Drive")
        raise
    lambda_client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps({"action": "drive_save", "job_id": job_id}).encode(),
    )
    return _response(200, {"drive_status": "saving"})


def delete_job(req, job_id: str) -> dict:
    """Remove only a finished/failed job owned by the requesting session."""
    session_id = _require_session_id(req["headers"].get("x-session-id"))
    job = _owned_job(job_id, session_id)
    if job["status"] in ("queued", "processing"):
        raise HTTPError(409, "Can't remove a job that hasn't finished yet")
    table.delete_item(Key={"pk": f"JOB#{job_id}"})
    for key in (job.get("result_key"), job.get("upload_key")):
        if key:
            try:
                s3.delete_object(Bucket=BUCKET, Key=key)
            except ClientError:
                pass
    return _response(200, {"deleted": True})


# ── Google Drive (reuses backend/app/drive.py unchanged) ────────────────

_drive = None
_drive_settings_loaded_at = 0.0
DRIVE_SETTINGS_TTL_SECONDS = 300


def _drive_module():
    """Load Drive credentials from SSM Parameter Store into the environment
    variables drive.py already reads, then import it. Imported lazily so the
    Google libraries never slow down ordinary API polling. Settings are
    re-read every few minutes, so changing the folder (e.g. at cutover)
    takes effect without a redeploy."""
    global _drive, _drive_settings_loaded_at
    if _drive is not None and time.time() - _drive_settings_loaded_at < DRIVE_SETTINGS_TTL_SECONDS:
        return _drive
    names = {
        f"{SSM_PREFIX}/gdrive/client_id": "GDRIVE_CLIENT_ID",
        f"{SSM_PREFIX}/gdrive/client_secret": "GDRIVE_CLIENT_SECRET",
        f"{SSM_PREFIX}/gdrive/refresh_token": "GDRIVE_REFRESH_TOKEN",
        f"{SSM_PREFIX}/gdrive/folder_url": "GDRIVE_FOLDER_URL",
    }
    before = {env: os.environ.get(env, "") for env in names.values()}
    try:
        ssm = boto3.client("ssm", region_name=REGION)
        resp = ssm.get_parameters(Names=list(names), WithDecryption=True)
        for p in resp.get("Parameters", []):
            os.environ[names[p["Name"]]] = p["Value"].strip()
        _drive_settings_loaded_at = time.time()
    except ClientError:
        log.exception("Could not read Drive settings from SSM")
    import drive  # noqa: E402  (copied from backend/app/drive.py at build time)
    # Never fall back to the hard-coded production folder: the folder must be
    # configured explicitly in SSM for this deployment.
    drive.DEFAULT_ROOT_FOLDER_ID = ""
    if _drive is not None and any(os.environ.get(env, "") != old for env, old in before.items()):
        drive.reset_service_cache()  # credentials/folder changed: drop cached client + folder ids
    _drive = drive
    return _drive


def _drive_root_folder_id() -> str:
    return _drive_module()._root_folder_id()


def run_drive_save(job_id: str) -> None:
    job = _get_job(job_id)
    if not job or job.get("drive_status") != "saving":
        return
    local = Path("/tmp") / f"drive-{job_id}.mp4"
    try:
        drive = _drive_module()
        folder_id = drive._root_folder_id()
        if not folder_id:
            raise drive.DriveNotConfigured(
                f"Google Drive folder isn't configured. Set the {SSM_PREFIX}/gdrive/folder_url parameter."
            )
        if folder_id == PRODUCTION_DRIVE_FOLDER_ID and not ALLOW_PRODUCTION_DRIVE_FOLDER:
            raise RuntimeError(
                "This AWS deployment is in test mode and is not allowed to write to the production "
                "Drive folder. Point gdrive/folder_url at the test folder, or set "
                "AllowProductionDriveFolder=true at cutover."
            )
        s3.download_file(BUCKET, job["result_key"], str(local))
        result = drive.upload_video(
            job["course_name"], job["unit_number"], job.get("chapter_number", ""), str(local),
            awarding_body=job.get("awarding_body", ""),
        )
        table.update_item(
            Key={"pk": f"JOB#{job_id}"},
            UpdateExpression="SET drive_status = :s, drive_link = :l REMOVE drive_error",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":s": "saved", ":l": result.get("web_view_link")},
        )
    except Exception as e:
        log.exception("Drive save failed for job %s", job_id)
        msg = str(e).strip() or e.__class__.__name__
        msg = re.sub(r"/tmp/[\w./-]+", "<tmp>", msg)[:300]
        try:
            table.update_item(
                Key={"pk": f"JOB#{job_id}"},
                UpdateExpression="SET drive_status = :f, drive_error = :e",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeValues={":f": "failed", ":e": msg},
            )
        except ClientError:
            pass
    finally:
        local.unlink(missing_ok=True)


# ── reconciler (EventBridge, every minute) ──────────────────────────────

def reconcile() -> dict:
    now = time.time()
    active = []
    items, kwargs = [], {}
    while True:
        resp = table.query(IndexName="active", KeyConditionExpression=Key("active").eq("1"), **kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    requeued = failed = expired = 0
    for i in items:
        pk = i["pk"]
        if i.get("qstate") == "reserved":
            if _num(i.get("created_at")) < now - UPLOAD_RESERVATION_TTL_SECONDS:
                table.delete_item(Key={"pk": pk})
                expired += 1
                continue
        elif i.get("qstate") == "processing":
            job = table.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item") or {}
            if job.get("status") == "processing" and _num(job.get("heartbeat")) < now - STALE_HEARTBEAT_SECONDS:
                attempts = int(job.get("attempts", 1))
                try:
                    if attempts >= MAX_ATTEMPTS:
                        table.update_item(
                            Key={"pk": pk},
                            UpdateExpression="SET #s = :f, #e = :err, #ttl = :ttl REMOVE active, qstate, worker_id",
                            ConditionExpression="#s = :p AND heartbeat = :hb",
                            ExpressionAttributeNames={"#s": "status", "#e": "error", "#ttl": "ttl"},
                            ExpressionAttributeValues={
                                ":f": "failed", ":p": "processing", ":hb": job.get("heartbeat"),
                                ":err": "The video worker stopped repeatedly while processing this video. Please retry the upload.",
                                ":ttl": int(now) + OUTPUT_RETENTION_DAYS * 86400,
                            },
                        )
                        failed += 1
                        continue
                    table.update_item(
                        Key={"pk": pk},
                        UpdateExpression=(
                            "SET #s = :q, qstate = :q, progress = list_append(if_not_exists(progress, :empty), :note) "
                            "REMOVE worker_id"
                        ),
                        ConditionExpression="#s = :p AND heartbeat = :hb",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={
                            ":q": "queued", ":p": "processing", ":hb": job.get("heartbeat"), ":empty": [],
                            ":note": ["The video worker stopped unexpectedly — retrying automatically."],
                        },
                    )
                    i["qstate"] = "queued"
                    requeued += 1
                except ClientError as e:
                    if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                        raise
        active.append(i)

    ensure_worker([i for i in active if i.get("qstate") in ("queued", "processing")])
    if requeued or failed or expired:
        log.info("Reconcile: requeued=%s failed=%s expired_reservations=%s", requeued, failed, expired)
    return {"requeued": requeued, "failed": failed, "expired_reservations": expired}


# ── entry point ───────────────────────────────────────────────────────────

_ROUTES = [
    ("GET", re.compile(r"^/healthz/?$"), healthz),
    ("POST", re.compile(r"^/jobs/preflight/?$"), preflight_upload),
    ("DELETE", re.compile(r"^/jobs/preflight/([^/]+)/?$"), cancel_preflight),
    ("POST", re.compile(r"^/jobs/?$"), create_job),
    ("GET", re.compile(r"^/jobs/?$"), list_jobs),
    ("GET", re.compile(r"^/jobs/([^/]+)/file/?$"), get_job_file),
    ("POST", re.compile(r"^/jobs/([^/]+)/drive/?$"), save_to_drive),
    ("GET", re.compile(r"^/jobs/([^/]+)/?$"), get_job),
    ("DELETE", re.compile(r"^/jobs/([^/]+)/?$"), delete_job),
]


def _parse_request(event: dict) -> dict:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = event.get("body") or ""
    if raw and event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    parsed = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
    return {
        "method": event.get("requestContext", {}).get("http", {}).get("method", "GET").upper(),
        "path": event.get("rawPath") or "/",
        "headers": headers,
        "query": event.get("queryStringParameters") or {},
        "json": parsed if isinstance(parsed, dict) else {},
    }


def handler(event, context):
    action = event.get("action") if isinstance(event, dict) else None
    if action == "drive_save":
        run_drive_save(str(event.get("job_id", "")))
        return {"ok": True}
    if action == "reconcile":
        return reconcile()

    req = _parse_request(event)
    path_matched = False
    try:
        for method, pattern, fn in _ROUTES:
            m = pattern.match(req["path"])
            if not m:
                continue
            path_matched = True
            if method != req["method"]:
                continue
            return fn(req, *m.groups())
        if path_matched:
            raise HTTPError(405, "Method Not Allowed")
        raise HTTPError(404, "Not Found")
    except HTTPError as e:
        return _response(e.status, {"detail": e.detail})
    except Exception:
        log.exception("Unhandled error for %s %s", req["method"], req["path"])
        return _response(500, {"detail": "Internal Server Error"})
