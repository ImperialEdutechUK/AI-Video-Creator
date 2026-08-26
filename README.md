# SLC Video Merger — Vercel + Railway split

This is the original single-file Streamlit app (`streamlit_app.py`) split
into two independently deployable pieces:

- **`backend/`** — FastAPI service on **Railway**. Owns all the real work:
  ffmpeg, OpenCV, pytesseract watermark detection/removal, Pillow overlay
  rendering. Same processing logic as the original app, exposed as HTTP
  job endpoints instead of driven by Streamlit's UI/session state.
- **`frontend/`** — Next.js app on **Vercel**. Upload form, live progress
  polling, download link. Talks to the backend over HTTPS.

## Deploy order (matters, because of the URL dependency)

1. **Deploy the backend first** — see `backend/README.md`. Grab its public
   Railway URL when done.
2. **Deploy the frontend** — see `frontend/README.md` — setting
   `NEXT_PUBLIC_API_URL` to that Railway URL.
3. **Go back to Railway** and set `FRONTEND_ORIGIN` to the Vercel URL you
   just got, so the backend's CORS allows requests from your frontend.
4. Reload the Vercel URL and run a test upload.

## What this minimal build does NOT include yet

Carried over from the original app but intentionally left out of this
first pass, to get the core upload → process → download flow working and
verified before adding more surface area:

- Google Drive upload (OAuth refresh-token flow)
- Canva Connect API integration
- The optional password gate (`APP_PASSWORD`)
- A persistent job queue (jobs currently live in the backend's memory and
  are lost on redeploy)

Each of these can be added as a follow-up — the ffmpeg/OpenCV pipeline
itself (`backend/app/pipeline.py`) is untouched from the original app, so
none of that logic needs to change to add them back.

## Verified working

The full pipeline was run end-to-end against this exact code before
handoff: upload → background job → intro/outro overlay → watermark
detection & removal → transition → concat → a valid, playable 1920x1080
H.264/AAC MP4 downloaded via the API.
