# Railway upload stability fixes

This revision is focused on preventing the backend crashes that surfaced as
"Network error during upload" or `502 Bad Gateway` in the browser.

## Changes made

- Uploads are streamed to disk in small chunks instead of using an unbounded
  `await video.read()` that loads the whole video into Python RAM.
- The in-memory job queue stores an input file path, not the video's bytes.
- The processing pipeline accepts an on-disk input path directly.
- The finished MP4 stays on disk; it is moved into the job output directory
  instead of being read completely into RAM and written out again.
- Browser upload concurrency is reduced from 3 files at once to 1.
- FFmpeg-heavy preparation concurrency defaults to 1 via
  `PIPELINE_PREP_WORKERS` (configurable up to 3 on larger Railway plans).
- Large intermediate files are deleted as soon as the next processing stage
  has consumed them, reducing peak temporary-disk usage.
- The source upload is deleted after processing, including failed jobs.
- Orphaned temporary job directories are cleared when the backend starts.
- Railway deployment config now uses `/healthz` as a deployment healthcheck.
- `/healthz` now reports queue depth, worker count, upload settings and free
  work-disk space.
- Production frontend no longer silently falls back to `localhost:8000` when
  `NEXT_PUBLIC_API_URL` is missing.
- Upload errors now distinguish size/type/server-unavailable cases and give a
  clearer message when the backend cannot be reached.

## Recommended Railway variables

```text
WORKERS=1
PIPELINE_PREP_WORKERS=1
UPLOAD_CHUNK_MB=1
MAX_UPLOAD_MB=500
MAX_QUEUED_JOBS=20
MAX_JOBS_PER_SESSION=5
MIN_FREE_DISK_MB=1024
DISK_HEADROOM_FACTOR=2.5
UPLOAD_RESERVATION_TTL_SECONDS=3600
FRONTEND_ORIGIN=https://<your-vercel-domain>
```

`WORKERS` or `PIPELINE_PREP_WORKERS` should only be increased after checking
Railway CPU and memory metrics while processing real videos.

## Important remaining limitation

Job metadata is still stored in memory. A Railway restart/redeploy therefore
clears the visible queue. This revision prevents abandoned temp files from
accumulating after such a restart, but making jobs survive restarts requires a
persistent store such as Redis/Postgres and persistent video storage (volume or
object storage).

## Multi-user protections

- Every browser gets a persistent opaque `X-Session-Id`; `/jobs` returns only
  that browser's jobs.
- File access, Google Drive save and delete operations are owner-scoped, so one
  browser cannot accidentally operate on another browser's job.
- Queue positions are calculated globally but only the requesting browser's
  job details are returned.
- `MAX_QUEUED_JOBS` caps the global waiting queue and upload reservations close
  the simultaneous-request race.
- `MAX_JOBS_PER_SESSION` prevents a single browser from monopolising the queue.
- The frontend calls `/jobs/preflight` before sending a large video. Queue/full
  and low-disk conditions are therefore rejected before the multipart transfer.
  The reservation is consumed by `POST /jobs`, cancelled on transfer failure,
  or expires after `UPLOAD_RESERVATION_TTL_SECONDS`. Free space is also checked
  while the server copies the spooled upload into its job workspace.
- `/healthz` exposes queue, upload-reservation and disk-headroom information for
  Railway monitoring.

This is designed for a trusted internal team. The browser session id is not a
replacement for login/SSO if the service is made public.
