#!/usr/bin/env python3
"""
FastAPI wrapper around pipeline.py.

Endpoints:
  POST /jobs             upload a video + course/unit -> enqueues a job, returns job id
  GET  /jobs              list all jobs (newest first) — powers the frontend's queue view
  GET  /jobs/{id}         poll one job's status/progress
  GET  /jobs/{id}/file    download the finished mp4
  GET  /healthz           liveness check for Railway + Drive config state

Jobs run through a small bounded worker pool instead of one thread per
upload. This matters for both correctness of the "queue" (jobs beyond the
worker count sit in status="queued" until a worker frees up) and for speed:
video encoding is CPU-bound, so running more jobs at once than you have
CPU cores just makes every one of them slower via contention. Tune with
the WORKERS env var to match your Railway plan's CPU allocation.

Jobs are kept in memory. That's fine for a single-instance minimal deploy;
if you scale to multiple Railway instances later, swap JOBS for Redis/DB.
"""
import os
import queue
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import drive, pipeline

app = FastAPI(title="SLC Video Merger API")

# ── CORS ──────────────────────────────────────────────────────────────────
# Set FRONTEND_ORIGIN on Railway to your Vercel URL, e.g.
# https://your-app.vercel.app  (comma-separate if you have more than one)
_origins_env = os.environ.get("FRONTEND_ORIGIN", "*")
ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",")] if _origins_env != "*" else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))

# How many videos process at once. Video encoding is CPU-bound, so this
# should roughly match the number of CPU cores your Railway plan gives you
# — set higher than that and jobs slow each other down instead of speeding
# up. Default 1 is the safe choice for Railway's smaller plans, but it also
# means every queued video processes strictly one-at-a-time no matter how
# many CPUs the plan actually has — if jobs are backing up in "queued" and
# your Railway plan has more than 1 vCPU, raise WORKERS (e.g. to 2 or 3) in
# the service's environment variables to process that many videos at once.
WORKERS = max(1, int(os.environ.get("WORKERS", "1")))

# job_id -> {status, progress, error, result_path, result_filename,
#            course_name, unit_number, chapter_number, original_filename,
#            created_at, drive_status, drive_error, drive_link}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_QUEUE: "queue.Queue[str]" = queue.Queue()

WORK_ROOT = Path(tempfile.gettempdir()) / "slc-merger-jobs"
WORK_ROOT.mkdir(parents=True, exist_ok=True)


def _worker_loop():
    while True:
        job_id = JOB_QUEUE.get()
        try:
            _run_job(job_id)
        finally:
            JOB_QUEUE.task_done()


def _run_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS[job_id]
        job["status"] = "processing"
        course_name, unit_number, chapter_number, awarding_body, video_bytes = (
            job["course_name"], job["unit_number"], job.get("chapter_number", ""),
            job.get("awarding_body", ""), job.pop("_video_bytes"),
        )

    def progress_cb(msg: str):
        with JOBS_LOCK:
            JOBS[job_id]["progress"].append(msg)

    job_dir = WORK_ROOT / job_id
    scratch = job_dir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    try:
        data, filename = pipeline.process_video(
            course_name, unit_number, video_bytes, scratch, progress_cb=progress_cb,
            chapter_number=chapter_number, awarding_body=awarding_body,
        )
        out_path = job_dir / filename
        out_path.write_bytes(data)
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["result_path"] = str(out_path)
            JOBS[job_id]["result_filename"] = filename
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = pipeline._sanitise_error(e)
    finally:
        # Only the final mp4 (already copied to job_dir above) needs to
        # stick around for the download endpoint.
        shutil.rmtree(scratch, ignore_errors=True)


@app.on_event("startup")
def _startup():
    pipeline.ensure_assets()
    for _ in range(WORKERS):
        threading.Thread(target=_worker_loop, daemon=True).start()


@app.get("/healthz")
def healthz():
    """Liveness check, plus the Drive configuration state.

    drive_auth_mode is the field to look at when "Save to Google Drive"
    fails: "oauth" means uploads go through a real Google account (correct
    for a personal My Drive folder), "service_account" means they do not
    and will hit storageQuotaExceeded outside a Shared Drive. A single
    missing GDRIVE_* variable is enough to silently drop to the fallback,
    so this is checked here rather than guessed at."""
    return {
        "ok": True,
        "workers": WORKERS,
        "queue_depth": JOB_QUEUE.qsize(),
        "drive_auth_mode": drive.auth_mode(),
        "drive_root_folder_id": drive._root_folder_id(),
    }


@app.post("/jobs")
async def create_job(
    course_name: str = Form(...),
    unit_number: str = Form(...),
    chapter_number: str = Form(""),
    awarding_body: str = Form(""),
    video: UploadFile = File(...),
):
    if not video.filename:
        raise HTTPException(400, "No file uploaded")

    video_bytes = await video.read()
    size_mb = len(video_bytes) / 1048576
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(413, f"File too large ({size_mb:.0f} MB). Limit is {MAX_UPLOAD_MB} MB.")

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "progress": [],
            "error": None,
            "result_path": None,
            "result_filename": None,
            "course_name": course_name,
            "unit_number": unit_number,
            "chapter_number": chapter_number,
            "awarding_body": awarding_body,
            "original_filename": video.filename,
            "created_at": time.time(),
            "_video_bytes": video_bytes,
            # Google Drive save state — separate from the processing
            # status above, since saving to Drive is a second, optional,
            # user-triggered step that only makes sense once done.
            "drive_status": "idle",   # idle | saving | saved | failed
            "drive_error": None,
            "drive_link": None,
        }

    JOB_QUEUE.put(job_id)
    return {"job_id": job_id}


def _public_job(job_id: str, job: dict) -> dict:
    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job["progress"][-20:],  # last 20 lines is plenty for a UI
        "error": job["error"],
        "result_filename": job["result_filename"],
        "original_filename": job["original_filename"],
        "course_name": job["course_name"],
        "unit_number": job["unit_number"],
        "chapter_number": job.get("chapter_number", ""),
        "awarding_body": job.get("awarding_body", ""),
        "created_at": job["created_at"],
        "drive_status": job.get("drive_status", "idle"),
        "drive_error": job.get("drive_error"),
        "drive_link": job.get("drive_link"),
    }


@app.get("/jobs")
def list_jobs():
    with JOBS_LOCK:
        jobs = [_public_job(jid, j) for jid, j in JOBS.items()]
    jobs.sort(key=lambda j: j["created_at"], reverse=True)
    return {"jobs": jobs}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return _public_job(job_id, job)


@app.get("/jobs/{job_id}/file")
def get_job_file(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] != "done" or not job["result_path"]:
            raise HTTPException(409, f"Job is not finished (status: {job['status']})")
        path = job["result_path"]
        filename = job["result_filename"]

    return FileResponse(path, media_type="video/mp4", filename=filename)


def _run_drive_save(job_id: str):
    """Uploads a finished video to Drive in the background. Runs in its
    own thread (started by the endpoint below) so the one-click button
    returns immediately and the frontend polls /jobs/{id} for progress,
    the same way it already polls for processing status."""
    with JOBS_LOCK:
        job = JOBS[job_id]
        course_name = job["course_name"]
        unit_number = job["unit_number"]
        chapter_number = job.get("chapter_number", "")
        result_path = job["result_path"]

    try:
        result = drive.upload_video(course_name, unit_number, chapter_number, result_path)
        with JOBS_LOCK:
            JOBS[job_id]["drive_status"] = "saved"
            JOBS[job_id]["drive_link"] = result["web_view_link"]
            JOBS[job_id]["drive_error"] = None
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["drive_status"] = "failed"
            JOBS[job_id]["drive_error"] = pipeline._sanitise_error(e)


@app.post("/jobs/{job_id}/drive")
def save_to_drive(job_id: str):
    """One-click 'Save to Google Drive'. Uploads the finished video into
    <root folder>/<course name>/, creating the course folder the first
    time and reusing it for every later unit/chapter in that course."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] != "done" or not job["result_path"]:
            raise HTTPException(409, f"Job is not finished (status: {job['status']})")
        if job.get("drive_status") == "saving":
            raise HTTPException(409, "Already saving to Drive")
        job["drive_status"] = "saving"
        job["drive_error"] = None

    threading.Thread(target=_run_drive_save, args=(job_id,), daemon=True).start()
    return {"drive_status": "saving"}
