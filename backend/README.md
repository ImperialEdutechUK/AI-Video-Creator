# SLC Video Merger — Backend (Railway)

FastAPI service that wraps the video pipeline (ffmpeg + OpenCV + pytesseract
watermark removal, Pillow intro/outro overlays). Ported from the original
Streamlit app's core functions — the processing logic itself is unchanged;
only the interface changed from a Streamlit UI to HTTP job endpoints.

## Endpoints
- `POST /jobs` — multipart form (`course_name`, `unit_number`, `video`) → `{job_id}`
- `GET /jobs/{id}` — `{status, progress[], error, result_filename}`
- `GET /jobs/{id}/file` — downloads the finished MP4 once `status == "done"`
- `GET /healthz` — liveness check

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
5. Railway auto-assigns a public URL like
   `https://your-service-production.up.railway.app`. Copy it — the frontend
   needs it as `NEXT_PUBLIC_API_URL`.
6. Test it: `curl https://your-service-production.up.railway.app/healthz`
   should return `{"ok": true}`.

## Notes / limitations of this minimal build
- **No Google Drive upload and no Canva integration yet** — those were in
  the original Streamlit app but are out of scope for this first pass.
  They can be added back as additional endpoints once the core flow is
  confirmed working end-to-end.
- **No password gate** — the original app's optional `APP_PASSWORD` check
  isn't ported yet. Add auth (e.g. an API key header checked in `main.py`,
  or Railway's private networking) before exposing this publicly with
  real content.
- **In-memory job store** — jobs live in the process's memory, so they're
  lost on redeploy/restart, and this won't scale past a single Railway
  instance. Fine for personal/small-team use; swap `JOBS` in `main.py` for
  Redis or a database if you need more durability or horizontal scaling.
- **Large uploads** — Railway doesn't impose Vercel-style request timeouts,
  but very long videos will still take real wall-clock time to process
  (ffmpeg re-encoding isn't instant). The frontend polls every 2s so the
  user sees live progress either way.
