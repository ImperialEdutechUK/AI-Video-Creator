#!/usr/bin/env python3
"""
FastAPI wrapper around pipeline.py.

Endpoints:
  POST /jobs             upload a video + course/unit -> starts a background job, returns job id
  GET  /jobs/{id}        poll status: pending | processing | done | failed  (+ progress log)
  GET  /jobs/{id}/file   download the finished mp4
  GET  /healthz          liveness check for Railway

Jobs are kept in memory. That's fine for a single-instance minimal deploy;
if you scale to multiple Railway instances later, swap JOBS for Redis/DB.
"""
import os
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import pipeline

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

# job_id -> {status, progress: [str], error, result_path, result_filename}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# Where finished files + temp workdirs live. Railway gives you an ephemeral
# filesystem, which is fine here since files are only needed until the user
# downloads them.
WORK_ROOT = Path(tempfile.gettempdir()) / "slc-merger-jobs"
WORK_ROOT.mkdir(parents=True, exist_ok=True)


@app.on_event("startup")
def _startup():
    pipeline.ensure_assets()


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _run_job(job_id: str, course_name: str, unit_number: str, video_bytes: bytes):
    job_dir = WORK_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def progress_cb(msg: str):
        with JOBS_LOCK:
            JOBS[job_id]["progress"].append(msg)

    with JOBS_LOCK:
        JOBS[job_id]["status"] = "processing"

    # process_video writes raw upload + every intermediate ffmpeg output
    # (norm.mp4, intro.mp4, etc.) directly into the dir it's given. Use a
    # scratch subfolder so we can wipe everything except the final file.
    scratch = job_dir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    try:
        data, filename = pipeline.process_video(
            course_name, unit_number, video_bytes, scratch, progress_cb=progress_cb
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
        # Drop the raw upload + every intermediate ffmpeg file; only the
        # final mp4 (already copied to job_dir above) needs to stick around
        # for the download endpoint.
        shutil.rmtree(scratch, ignore_errors=True)


@app.post("/jobs")
async def create_job(
    course_name: str = Form(...),
    unit_number: str = Form(...),
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
            "status": "pending",
            "progress": [],
            "error": None,
            "result_path": None,
            "result_filename": None,
        }

    thread = threading.Thread(
        target=_run_job, args=(job_id, course_name, unit_number, video_bytes), daemon=True
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return {
            "job_id": job_id,
            "status": job["status"],
            "progress": job["progress"][-20:],  # last 20 lines is plenty for a UI
            "error": job["error"],
            "result_filename": job["result_filename"],
        }


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
