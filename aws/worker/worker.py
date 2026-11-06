#!/usr/bin/env python3
"""
ECS Fargate (Spot) video worker for the AWS deployment.

Replaces the Railway service's in-process worker thread. The processing
itself is the unmodified backend/app/pipeline.py; this file only moves job
state from memory into DynamoDB and video files from local disk into S3.

Lifecycle:
  * The API Lambda starts a task when a job is queued and no worker is running.
  * The task claims the oldest queued job (conditional DynamoDB update, so two
    workers can never claim the same job), downloads the source from S3, runs
    pipeline.process_video, uploads the result to S3 and marks the job done.
  * When no job has been waiting for IDLE_EXIT_SECONDS, the task exits so
    nothing is billed while the team is not uploading.
  * On SIGTERM (Fargate Spot interruption / stop) the current job is put back
    in the queue and a replacement worker is started by the API's reconciler.
"""
import logging
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app import pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("slc-video-worker")

REGION = os.environ.get("AWS_REGION", "eu-west-2")
TABLE_NAME = os.environ["JOBS_TABLE"]
BUCKET = os.environ["MEDIA_BUCKET"]
IDLE_EXIT_SECONDS = max(10, int(os.environ.get("IDLE_EXIT_SECONDS", "120")))
POLL_SECONDS = max(1, int(os.environ.get("POLL_SECONDS", "4")))
HEARTBEAT_SECONDS = max(10, int(os.environ.get("HEARTBEAT_SECONDS", "30")))
OUTPUT_RETENTION_DAYS = max(1, int(os.environ.get("OUTPUT_RETENTION_DAYS", "3")))
# Keep waiting (instead of exiting) while an upload that started within this
# many seconds is still in progress - the worker is pre-warmed at preflight.
UPLOAD_WAIT_SECONDS = max(60, int(os.environ.get("UPLOAD_WAIT_SECONDS", "600")))
PROGRESS_FLUSH_SECONDS = 2.0
PROGRESS_KEEP = 20  # the API only ever returned the last 20 lines

WORKER_ID = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
WORK_ROOT = Path(tempfile.gettempdir()) / "slc-merger-jobs"

ddb = boto3.resource("dynamodb", region_name=REGION)
table = ddb.Table(TABLE_NAME)
s3 = boto3.client("s3", region_name=REGION)

_current_job_id: str | None = None
_stopping = False


def _now() -> Decimal:
    return Decimal(str(round(time.time(), 3)))


# ── job claiming ──────────────────────────────────────────────────────────

_uploads_in_progress = False


def _queued_candidates() -> list[str]:
    """Oldest-first queued job ids from the sparse `active` index. Also notes
    whether a recent upload is still in progress, so the worker waits for it
    instead of exiting just before the video arrives."""
    global _uploads_in_progress
    resp = table.query(
        IndexName="active",
        KeyConditionExpression=Key("active").eq("1"),
        ScanIndexForward=True,
    )
    all_items = resp.get("Items", [])
    cutoff = time.time() - UPLOAD_WAIT_SECONDS
    _uploads_in_progress = any(
        i.get("qstate") == "reserved" and float(i.get("created_at", 0)) > cutoff for i in all_items
    )
    items = [i for i in all_items if i.get("qstate") == "queued"]
    items.sort(key=lambda i: (float(i.get("created_at", 0)), i["pk"]))
    return [i["pk"] for i in items]


def _claim(pk: str) -> dict | None:
    try:
        resp = table.update_item(
            Key={"pk": pk},
            UpdateExpression=(
                "SET #s = :p, qstate = :p, worker_id = :w, heartbeat = :now, "
                "started_at = :now, attempts = if_not_exists(attempts, :zero) + :one"
            ),
            ConditionExpression="#s = :q",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":p": "processing", ":q": "queued", ":w": WORKER_ID,
                ":now": _now(), ":zero": 0, ":one": 1,
            },
            ReturnValues="ALL_NEW",
        )
        return resp["Attributes"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None  # another worker claimed it first
        raise


def claim_next() -> dict | None:
    for pk in _queued_candidates():
        job = _claim(pk)
        if job:
            return job
    return None


# ── job updates (all guarded so a requeued job is never overwritten) ─────

def _owned_update(pk: str, expr: str, values: dict, names: dict | None = None) -> bool:
    names = dict(names or {})
    names["#s"] = "status"
    values = dict(values)
    values[":p"] = "processing"
    values[":w"] = WORKER_ID
    try:
        table.update_item(
            Key={"pk": pk},
            UpdateExpression=expr,
            ConditionExpression="#s = :p AND worker_id = :w",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.warning("Job %s is no longer owned by this worker; skipping update", pk)
            return False
        raise


class ProgressWriter:
    """Mirrors the Railway progress list, throttled to one write per ~2s."""

    def __init__(self, pk: str, existing: list[str]):
        self.pk = pk
        self.lines = list(existing or [])
        self.dirty = False
        self.last_flush = 0.0
        self.lock = threading.Lock()

    def __call__(self, msg: str):
        with self.lock:
            self.lines.append(str(msg))
            self.lines = self.lines[-PROGRESS_KEEP:]
            self.dirty = True
        if time.time() - self.last_flush >= PROGRESS_FLUSH_SECONDS:
            self.flush()

    def flush(self):
        with self.lock:
            if not self.dirty:
                return
            lines = list(self.lines)
            self.dirty = False
            self.last_flush = time.time()
        _owned_update(self.pk, "SET progress = :l, heartbeat = :now", {":l": lines, ":now": _now()})


class Heartbeat(threading.Thread):
    """Long FFmpeg steps can run for minutes without a progress message, so a
    separate heartbeat tells the reconciler this worker is still alive."""

    def __init__(self, pk: str):
        super().__init__(daemon=True)
        self.pk = pk
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.wait(HEARTBEAT_SECONDS):
            try:
                _owned_update(self.pk, "SET heartbeat = :now", {":now": _now()})
            except Exception:
                log.exception("Heartbeat update failed")

    def stop(self):
        self.stop_event.set()


def _requeue_current(reason: str):
    """Put the in-flight job back in the queue (used on SIGTERM)."""
    if not _current_job_id:
        return
    pk = f"JOB#{_current_job_id}"
    try:
        _owned_update(
            pk,
            "SET #s = :q, qstate = :q, progress = list_append(if_not_exists(progress, :empty), :note) "
            "REMOVE worker_id",
            {":q": "queued", ":empty": [], ":note": [reason]},
        )
        log.info("Requeued job %s: %s", _current_job_id, reason)
    except Exception:
        log.exception("Could not requeue job %s", _current_job_id)


def _on_sigterm(signum, frame):
    global _stopping
    _stopping = True
    log.warning("SIGTERM received (Spot interruption or task stop)")
    if _current_job_id:
        _requeue_current("Worker was interrupted — the video is back in the queue and will restart automatically.")
        # Raising here unwinds out of subprocess.run(), which kills FFmpeg.
        raise SystemExit(0)


# ── processing ────────────────────────────────────────────────────────────

def process(job: dict):
    global _current_job_id
    job_id = job["job_id"]
    pk = job["pk"]
    _current_job_id = job_id
    log.info("Processing job %s (attempt %s)", job_id, job.get("attempts"))

    upload_key = job["upload_key"]
    suffix = Path(upload_key).suffix.lower() or ".mp4"
    job_dir = WORK_ROOT / job_id
    scratch = job_dir / "scratch"
    input_path = job_dir / f"input{suffix}"
    progress = ProgressWriter(pk, job.get("progress", []))
    heartbeat = Heartbeat(pk)
    heartbeat.start()

    try:
        scratch.mkdir(parents=True, exist_ok=True)
        s3.download_file(BUCKET, upload_key, str(input_path))

        final_path, filename = pipeline.process_video(
            job["course_name"], job["unit_number"], input_path, scratch,
            progress_cb=progress,
            chapter_number=job.get("chapter_number", ""),
            awarding_body=job.get("awarding_body", ""),
        )
        result_key = f"outputs/{job_id}/{filename}"
        s3.upload_file(
            str(final_path), BUCKET, result_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
        progress.flush()
        _owned_update(
            pk,
            "SET #s = :d, result_key = :k, result_filename = :f, finished_at = :now, #ttl = :ttl "
            "REMOVE active, qstate, worker_id",
            {
                ":d": "done", ":k": result_key, ":f": filename, ":now": _now(),
                ":ttl": int(time.time()) + OUTPUT_RETENTION_DAYS * 86400,
            },
            {"#ttl": "ttl"},
        )
        log.info("Job %s done -> s3://%s/%s", job_id, BUCKET, result_key)
    except SystemExit:
        raise
    except Exception as e:
        log.exception("Video processing failed for job %s", job_id)
        try:
            progress.flush()
        except Exception:
            pass
        _owned_update(
            pk,
            "SET #s = :f, #e = :err, finished_at = :now, #ttl = :ttl REMOVE active, qstate, worker_id",
            {
                ":f": "failed", ":err": pipeline._sanitise_error(e), ":now": _now(),
                ":ttl": int(time.time()) + OUTPUT_RETENTION_DAYS * 86400,
            },
            {"#e": "error", "#ttl": "ttl"},
        )
    finally:
        heartbeat.stop()
        if not _stopping:
            # Same as Railway: the source upload is deleted after processing,
            # including failed jobs. (On interruption it is kept for the retry.)
            try:
                s3.delete_object(Bucket=BUCKET, Key=upload_key)
            except Exception:
                log.exception("Could not delete source upload %s", upload_key)
        shutil.rmtree(job_dir, ignore_errors=True)
        _current_job_id = None


def main():
    signal.signal(signal.SIGTERM, _on_sigterm)
    pipeline.ensure_assets()
    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    log.info(
        "Worker %s started: table=%s bucket=%s idle_exit=%ss prep_workers=%s",
        WORKER_ID, TABLE_NAME, BUCKET, IDLE_EXIT_SECONDS, pipeline.PIPELINE_PREP_WORKERS,
    )

    idle_since = time.time()
    while not _stopping:
        job = claim_next()
        if job:
            process(job)
            idle_since = time.time()
            continue
        if _uploads_in_progress:
            idle_since = time.time()  # a video is on its way - don't shut down
        elif time.time() - idle_since >= IDLE_EXIT_SECONDS:
            log.info("No queued jobs for %ss — exiting to stop billing", IDLE_EXIT_SECONDS)
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    sys.exit(0)
