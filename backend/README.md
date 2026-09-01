# SLC Video Merger — Backend (Railway)

FastAPI service that wraps the video pipeline (ffmpeg + OpenCV + pytesseract
watermark removal, Pillow intro/outro overlays). Ported from the original
Streamlit app's core functions — the processing logic itself is unchanged;
only the interface changed from a Streamlit UI to HTTP job endpoints.

## Endpoints
- `POST /jobs` — multipart form (`course_name`, `unit_number`, `chapter_number`, `video`) → `{job_id}`. `chapter_number` is optional.
- `GET /jobs` — lists every job (newest first) — powers the frontend's shared queue view
- `GET /jobs/{id}` — `{status, progress[], error, result_filename, drive_status, drive_link, drive_error}`
- `GET /jobs/{id}/file` — downloads the finished MP4 once `status == "done"`
- `POST /jobs/{id}/drive` — one-click "Save to Google Drive". Only works once the job's `status == "done"`. Runs in the background; poll `GET /jobs/{id}` for `drive_status` (`idle` → `saving` → `saved`/`failed`).
- `GET /healthz` — liveness check, also reports `workers` and `queue_depth`

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
instead of one thread per upload. Submit as many videos as you want — extra
ones sit with `status: "queued"` until a worker frees up, rather than all
starting immediately and competing for the same CPU. That contention is
usually what makes concurrent video processing *slower*, not faster, so the
queue is also the fix for "make it faster" as much as it is for "let me
queue more videos."

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
   - `MAX_UPLOAD_MB` = `500` (optional, matches the original app's cap)
   - `WORKERS` = how many videos process at once (default `1`). Video
     encoding is CPU-bound, so this should match the number of CPU cores
     your Railway plan actually gives you — setting it higher than that
     makes concurrent jobs slower via contention, not faster. Check your
     plan's vCPU count before raising this above 1.
5. Railway auto-assigns a public URL like
   `https://your-service-production.up.railway.app`. Copy it — the frontend
   needs it as `NEXT_PUBLIC_API_URL`.
6. Test it: `curl https://your-service-production.up.railway.app/healthz`
   should return `{"ok": true}`.

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
- **Queue is shared, not per-user** — `GET /jobs` returns every job on the
  server, not scoped to whoever's asking. Fine for a small trusted team,
  not fine if you expose this publicly without adding auth/scoping first.
- **Large uploads** — Railway doesn't impose Vercel-style request timeouts,
  but very long videos will still take real wall-clock time to process
  (ffmpeg re-encoding isn't instant). The frontend polls every 2s so the
  user sees live progress either way.
- **Speed** — the pipeline already uses `libx264 -preset ultrafast` and
  skips re-encoding in `concat` wherever it safely can, so there wasn't
  free encode-speed left on the table. The `WORKERS` env var (see above)
  is the main lever now: raising it only helps if your Railway plan
  actually has spare CPU cores to give it.
