# SLC Video Merger — Frontend (Vercel)

Minimal Next.js UI: a form (course name, unit number, video file), a
progress log polled from the backend every 2 seconds, and a download link
once the job finishes.

## Deploy to Vercel

1. Push this `frontend/` folder to a GitHub repo (or push the whole
   project and set the Vercel project's **root directory** to `frontend/`).
2. In Vercel: **Add New → Project** → import the repo. Framework preset
   auto-detects as Next.js — no build config changes needed.
3. Before the first deploy (or right after, then redeploy), go to
   **Project Settings → Environment Variables** and add:
   - `NEXT_PUBLIC_API_URL` = your Railway backend URL, e.g.
     `https://your-service-production.up.railway.app` (no trailing slash)
4. Deploy. Vercel gives you a URL like `https://your-app.vercel.app`.
5. Go back to Railway and set `FRONTEND_ORIGIN` to that exact Vercel URL
   so CORS allows the browser to call the API.

## Local development

```bash
cp .env.local.example .env.local   # then edit NEXT_PUBLIC_API_URL
npm install
npm run dev
```

Runs on http://localhost:3000 and expects the backend reachable at the
URL in `.env.local` (defaults to http://localhost:8000 if unset).
