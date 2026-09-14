# SLC Video Merger — Backend (Railway)

FastAPI service that wraps the video pipeline (ffmpeg + OpenCV + pytesseract
watermark removal, Pillow intro/outro overlays). Ported from the original
Streamlit app's core functions — the processing logic itself is unchanged;
only the interface changed from a Streamlit UI to HTTP job endpoints.

## Endpoints
- `POST /jobs/preflight` — lightweight queue/disk-capacity check using file name/size. Returns a short-lived upload reservation token before the browser sends the large video body.
- `DELETE /jobs/preflight/{token}` — releases an unused reservation (the frontend calls this after a cancelled/failed transfer).
- `POST /jobs` — multipart form (`course_name`, `unit_number`, `chapter_number`, `video`) → `{job_id}`. Requires `X-Session-Id` and the preflight `X-Upload-Reservation` token.
- `GET /jobs` — lists only jobs owned by the requesting browser session and returns team-wide queue/capacity metadata.
- `GET /jobs/{id}` — returns one owned job, including `queue_position` while it is waiting.
- `GET /jobs/{id}/file` — downloads/previews the finished MP4 once `status == "done"`; access is scoped to the owning browser session.
- `POST /jobs/{id}/drive` — one-click "Save to Google Drive" for an owned finished job. Runs in the background; poll `GET /jobs/{id}` for `drive_status`.
- `DELETE /jobs/{id}` — removes only an owned finished/failed job.
- `GET /healthz` — liveness check plus worker, queue, disk-reservation and upload-limit information.

## Save to Google Drive

Videos are saved with this layout, using a Google Cloud **service account**
so the whole flow is one click — no per-user Google sign-in:

```
<your Drive root folder>/
  <Course Name>/                                   (created once, reused after)
    <Course Name> - <Unit Number> - <Chapter Number>.mp4
    <Course Name> - <Unit Number> - <Chapter Number>.mp4
  <Another Course>/
    ...
```

If a course folder already exists it's reused, so every unit/chapter for
that course lands in the same place instead of creating duplicates.

**One-time setup:**
1. In [Google Cloud Console](https://console.cloud.google.com/), create (or
   reuse) a project and enable the **Google Drive API**.
2. Create a **Service Account** (IAM & Admin → Service Accounts), then
   create a JSON key for it and download it.
3. Open your target Drive folder — e.g.
   `https://drive.google.com/drive/folders/1cY7v7956TyrJbGPGno4QQJ5Zpj6bXDjZ`
   — and **Share** it with the service account's email address (looks like
   `something@your-project.iam.gserviceaccount.com`), giving it **Editor**
   access. This step is required — a service account can only see/write to
   folders explicitly shared with it.
4. On Railway, add these variables to the backend service:
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — paste the **entire contents** of the
     downloaded key JSON file as a single-line value.
   - `GOOGLE_DRIVE_ROOT_FOLDER_ID` — the folder ID from the URL in step 3
     (defaults to `1cY7v7956TyrJbGPGno4QQJ5Zpj6bXDjZ`, i.e. the folder you
     shared, so you only need to set this if you want a different root).
5. Redeploy. The "Save to Google Drive" button on each finished job will
   now work.

## Job queue

Jobs run through a small bounded worker pool (`WORKERS` env var, default 1)
instead of one processing thread per upload. Uploaded videos are streamed to
`/tmp/slc-merger-jobs/<job-id>/` in small chunks and the queue stores only the
input file path — never the complete video bytes in Python RAM. Extra jobs sit
with `status: "queued"` until a worker frees up. The UI shows a global queue
position (for example, `Queued — position 3`) without exposing other users'
job details.

For multi-user safety, the backend reserves a queue slot before each upload,
enforces `MAX_QUEUED_JOBS` globally and `MAX_JOBS_PER_SESSION` per browser,
and performs a conservative free-disk/headroom check before accepting the
request body. Jobs are tagged with the browser's opaque `X-Session-Id`, so
listing, Drive-save and delete operations cannot accidentally act on another
browser's job. This is lightweight team isolation, not full user authentication.

The pipeline also defaults to `PIPELINE_PREP_WORKERS=1`, so normalisation,
intro generation and outro generation do not start several FFmpeg-heavy tasks
at the same time on a small Railway instance. Larger plans can raise this to
2 or 3 after checking CPU/RAM headroom.

## Deploy to Railway

1. Push this `backend/` folder to a GitHub repo (or push the whole project
   and set the Railway service's **root directory** to `backend/`).
2. In Railway: **New Project → Deploy from GitHub repo** → pick the repo.
3. Railway detects the `Dockerfile` and builds automatically (ffmpeg,
   tesseract-ocr, and libgl1 are installed inside the image — no extra
   Railway buildpack config needed).
4. Under **Variables**, add:
   - `FRONTEND_ORIGIN` = your Vercel URL, e.g. `https://your-app.vercel.app`
     (comma-separate multiple origins if needed; defaults to `*` if unset)
   - `MAX_UPLOAD_MB` = `500` (optional; uploads are rejected while streaming
     once they exceed this size)
   - `UPLOAD_CHUNK_MB` = `1` (optional; amount read into Python memory per
     upload chunk)
   - `WORKERS` = `1` (recommended for smaller Railway plans; controls how many
     complete videos process at once)
   - `PIPELINE_PREP_WORKERS` = `1` (recommended; controls FFmpeg-heavy prep
     concurrency inside one video job; only raise to 2-3 with spare CPU/RAM)
   - `MAX_QUEUED_JOBS` = `20` (recommended starting point; global waiting-upload cap)
   - `MAX_JOBS_PER_SESSION` = `5` (active uploading/queued/processing jobs per browser)
   - `MIN_FREE_DISK_MB` = `1024` (keep at least 1 GB unused as a storage safety reserve)
   - `DISK_HEADROOM_FACTOR` = `2.5` (conservative source + FFmpeg scratch/output allowance)
   - `UPLOAD_RESERVATION_TTL_SECONDS` = `3600` (stale preflight reservation expiry; 1 hour allows slow large uploads)
5. Railway auto-assigns a public URL like
   `https://your-service-production.up.railway.app`. Copy it — the frontend
   needs it as `NEXT_PUBLIC_API_URL`.
6. Railway is configured by `railway.json` to use `/healthz` during deploy.
   Test it manually as well: `curl https://your-service-production.up.railway.app/healthz`
   should return `{"ok": true, ...}` including worker, queue, upload and free
   work-disk information.

## Notes / limitations of this minimal build
- **No Canva integration yet** — that was in the original Streamlit app
  but is out of scope for this pass. Google Drive save is now built in
  (see above).
- **No password gate** — the original app's optional `APP_PASSWORD` check
  isn't ported yet. Add auth (e.g. an API key header checked in `main.py`,
  or Railway's private networking) before exposing this publicly with
  real content.
- **In-memory job store** — jobs live in the process's memory, so they're
  lost on redeploy/restart, and this won't scale past a single Railway
  instance. Fine for personal/small-team use; swap `JOBS` in `main.py` for
  Redis or a database if you need more durability or horizontal scaling.
- **Browser-session isolation, not login authentication** — each browser gets
  an opaque local session id and sees/controls only its own jobs. This prevents
  accidental cross-user actions for an internal team, but there are still no
  named user accounts, SSO, roles, or password gate. Add real authentication
  before exposing the service publicly.
- **Large uploads** — upload bodies are streamed to disk instead of loaded
  into RAM. The source upload is deleted as soon as processing finishes, and
  large pipeline intermediates are deleted as soon as their next stage has
  consumed them. Very long videos still take real wall-clock time to process.
- **Restart behaviour** — job metadata is still in memory. A Railway restart
  therefore clears the queue, and startup removes orphaned `/tmp` job files so
  repeated crashes cannot slowly fill ephemeral storage. Add Redis/DB plus a
  persistent volume/object store if jobs must survive restarts.
- **Speed** — the pipeline already uses `libx264 -preset ultrafast` and
  skips re-encoding in `concat` wherever it safely can, so there wasn't
  free encode-speed left on the table. The `WORKERS` env var (see above)
  is the main lever now: raising it only helps if your Railway plan
  actually has spare CPU cores to give it.
