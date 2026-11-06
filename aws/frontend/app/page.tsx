"use client";

import { useEffect, useRef, useState } from "react";

// AWS deployment: NEXT_PUBLIC_API_URL is set at build time by
// aws/scripts/deploy.sh to the API Lambda Function URL.
// In production we deliberately do NOT fall back to localhost: a missing
// variable would otherwise make every user's browser try to upload
// to their own computer and surface a misleading "network error".
const CONFIGURED_API_URL = (process.env.NEXT_PUBLIC_API_URL || "").replace(/\/+$/, "");
const API_URL = CONFIGURED_API_URL || (process.env.NODE_ENV === "development" ? "http://localhost:8000" : "");

type JobStatus = "queued" | "processing" | "done" | "failed";

type DriveStatus = "idle" | "saving" | "saved" | "failed";

interface Job {
  job_id: string;
  status: JobStatus;
  queue_position: number | null;
  progress: string[];
  error: string | null;
  result_filename: string | null;
  original_filename: string;
  course_name: string;
  unit_number: string;
  chapter_number: string;
  awarding_body: string;
  created_at: number;
  drive_status: DriveStatus;
  drive_error: string | null;
  drive_link: string | null;
}

interface QueueInfo {
  queued: number;
  processing: number;
  uploading: number;
  capacity: number;
  session_active: number;
  session_limit: number;
}

interface StagedItem {
  key: string;
  file: File;
  awardingBody: string;
  courseName: string;
  unitNumber: string;
  chapterNumber: string;
  uploadPct: number | null; // null = not uploading yet
  uploadError: string | null;
}

function extractApiDetail(text: string): string {
  if (!text) return "";
  try {
    const parsed = JSON.parse(text);
    if (typeof parsed?.detail === "string") return parsed.detail;
  } catch {
    // Non-JSON response (for example, an edge/proxy error page).
  }
  return text.length <= 240 ? text : "";
}

function requestFailureMessage(status: number, responseText = ""): string {
  const detail = extractApiDetail(responseText);
  if (status === 413) return detail || "This video is larger than the server upload limit.";
  if (status === 415) return detail || "Unsupported video type. Use MP4, MOV, WebM, MKV or AVI.";
  if (status === 429) return detail || "The team queue or your browser's active-job limit has been reached. Please try again shortly.";
  if (status === 507) return detail || "The video server is temporarily low on storage. Please try again later.";
  if ([502, 503, 504].includes(status)) {
    return "The video server is temporarily unavailable or restarting. Please wait a moment and retry.";
  }
  if (status >= 500) return detail || "The video server could not accept the upload. Please retry.";
  return detail || `Upload failed (${status || "no response"}).`;
}

function uploadFailureMessage(xhr: XMLHttpRequest): string {
  return requestFailureMessage(xhr.status, xhr.responseText || "");
}

// The video goes straight to S3 (pre-signed POST). S3 answers errors in XML,
// so translate the ones a user can actually hit into the usual messages.
function storageFailureMessage(xhr: XMLHttpRequest): string {
  const text = xhr.responseText || "";
  if (text.includes("EntityTooLarge")) return requestFailureMessage(413);
  if (text.includes("AccessDenied") || text.includes("Policy expired")) {
    return "The upload link expired before the transfer finished. Please retry the upload.";
  }
  return requestFailureMessage(xhr.status || 0);
}

interface UploadTarget {
  url: string;
  fields: Record<string, string>;
}

const SESSION_STORAGE_KEY = "slc-video-merger-session-id";

function createBrowserSessionId(): string {
  const browserCrypto = typeof globalThis !== "undefined" ? globalThis.crypto : undefined;
  if (browserCrypto && typeof browserCrypto.randomUUID === "function") {
    return browserCrypto.randomUUID();
  }
  if (browserCrypto && typeof browserCrypto.getRandomValues === "function") {
    const bytes = new Uint8Array(24);
    browserCrypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  return `browser-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

export default function Home() {
  const [awardingBody, setAwardingBody] = useState("");
  const [courseName, setCourseName] = useState("");
  const [staged, setStaged] = useState<StagedItem[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [queueInfo, setQueueInfo] = useState<QueueInfo | null>(null);
  const [sessionId, setSessionId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Create one opaque id per browser profile and keep it across reloads.
  // The backend uses it to isolate each staff member's jobs while still
  // calculating queue position against the team-wide processing queue.
  useEffect(() => {
    let id = window.localStorage.getItem(SESSION_STORAGE_KEY) || "";
    if (!/^[A-Za-z0-9_-]{16,128}$/.test(id)) {
      id = createBrowserSessionId();
      window.localStorage.setItem(SESSION_STORAGE_KEY, id);
    }
    setSessionId(id);
  }, []);

  useEffect(() => {
    if (!sessionId) return;
    fetchJobs();
    pollRef.current = setInterval(fetchJobs, 2000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [sessionId]);

  const removingRef = useRef<Set<string>>(new Set());

  async function fetchJobs() {
    if (!API_URL || !sessionId) return;
    try {
      const res = await fetch(`${API_URL}/jobs`, {
        headers: { "X-Session-Id": sessionId },
      });
      if (!res.ok) return;
      const data = await res.json();
      const incoming: Job[] = data.jobs || [];
      setQueueInfo(data.queue || null);

      // Once this browser's video is safely saved to Drive, remove its local
      // server copy. The ownership header means another browser cannot delete
      // this job even if it somehow learns the job id.
      const toRemove = incoming.filter((j) => j.drive_status === "saved" && !removingRef.current.has(j.job_id));
      for (const job of toRemove) {
        removingRef.current.add(job.job_id);
        fetch(`${API_URL}/jobs/${job.job_id}`, {
          method: "DELETE",
          headers: { "X-Session-Id": sessionId },
        }).catch(() => {
          removingRef.current.delete(job.job_id);
        });
      }

      setJobs(incoming.filter((j) => !removingRef.current.has(j.job_id)));
    } catch {
      // transient network hiccup — next tick retries
    }
  }

  function handleFilesPicked(files: FileList | null) {
    if (!files || files.length === 0) return;
    const startUnit = staged.length + 1;
    const additions: StagedItem[] = Array.from(files).map((file, i) => ({
      key: `${file.name}-${file.size}-${Date.now()}-${i}`,
      file,
      awardingBody,
      courseName,
      unitNumber: `Unit ${startUnit + i}`,
      chapterNumber: "",
      uploadPct: null,
      uploadError: null,
    }));
    setStaged((prev) => [...prev, ...additions]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  function updateStaged(key: string, patch: Partial<StagedItem>) {
    setStaged((prev) => prev.map((s) => (s.key === key ? { ...s, ...patch } : s)));
  }

  function removeStaged(key: string) {
    setStaged((prev) => prev.filter((s) => s.key !== key));
  }

  // Reserve team queue/disk capacity before starting the large request body.
  // This avoids making a user upload hundreds of MB only to discover that
  // Railway has no safe queue slot or working-disk headroom left.
  async function uploadOne(item: StagedItem): Promise<void> {
    if (!API_URL || !sessionId) {
      updateStaged(item.key, {
        uploadPct: null,
        uploadError: !API_URL
          ? "Backend URL is not configured. Set NEXT_PUBLIC_API_URL in Vercel and redeploy."
          : "Your browser session is still initialising. Please retry in a moment.",
      });
      return;
    }

    let reservationToken = "";
    let uploadTarget: UploadTarget | null = null;
    try {
      const params = new URLSearchParams({
        file_size: String(item.file.size),
        filename: item.file.name,
      });
      const preflight = await fetch(`${API_URL}/jobs/preflight?${params.toString()}`, {
        method: "POST",
        headers: { "X-Session-Id": sessionId },
      });
      if (!preflight.ok) {
        updateStaged(item.key, {
          uploadPct: null,
          uploadError: requestFailureMessage(preflight.status, await preflight.text()),
        });
        return;
      }
      const data = await preflight.json();
      reservationToken = data.reservation_token || "";
      uploadTarget = data.upload || null;
      if (!reservationToken || !uploadTarget?.url) {
        updateStaged(item.key, { uploadPct: null, uploadError: "The server did not create an upload reservation. Please retry." });
        return;
      }
    } catch {
      const message = navigator.onLine
        ? "Could not reach the video server for upload preflight. It may be restarting or unavailable. Please retry in a moment."
        : "Your internet connection appears to be offline. Reconnect and retry the upload.";
      updateStaged(item.key, { uploadPct: null, uploadError: message });
      return;
    }

    const releaseReservation = () => {
      if (!reservationToken) return;
      fetch(`${API_URL}/jobs/preflight/${encodeURIComponent(reservationToken)}`, {
        method: "DELETE",
        headers: { "X-Session-Id": sessionId },
      }).catch(() => {});
      reservationToken = "";
    };

    // Step 1: send the video bytes straight to S3 using the pre-signed POST
    // from preflight. S3 enforces the size limit itself.
    const target = uploadTarget as UploadTarget;
    const form = new FormData();
    for (const [name, value] of Object.entries(target.fields || {})) {
      form.append(name, value);
    }
    form.append("file", item.file); // S3 requires the file to be the last field

    const uploaded = await new Promise<boolean>((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", target.url);

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          updateStaged(item.key, { uploadPct: Math.round((e.loaded / e.total) * 100) });
        }
      };

      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(true);
          return;
        }
        releaseReservation();
        updateStaged(item.key, { uploadPct: null, uploadError: storageFailureMessage(xhr) });
        resolve(false);
      };

      xhr.onerror = () => {
        releaseReservation();
        const message = navigator.onLine
          ? "Could not reach the video storage. It may be unavailable or blocked by CORS. Please retry in a moment."
          : "Your internet connection appears to be offline. Reconnect and retry the upload.";
        updateStaged(item.key, { uploadPct: null, uploadError: message });
        resolve(false);
      };

      xhr.onabort = () => {
        releaseReservation();
        updateStaged(item.key, { uploadPct: null, uploadError: "Upload cancelled. You can retry when ready." });
        resolve(false);
      };

      updateStaged(item.key, { uploadPct: 0, uploadError: null });
      xhr.send(form);
    });
    if (!uploaded) return;

    // Step 2: register the uploaded video as a queued job (same metadata the
    // Railway service received as multipart form fields).
    try {
      const res = await fetch(`${API_URL}/jobs`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Session-Id": sessionId,
          "X-Upload-Reservation": reservationToken,
        },
        body: JSON.stringify({
          course_name: item.courseName,
          unit_number: item.unitNumber,
          chapter_number: item.chapterNumber,
          awarding_body: item.awardingBody,
          filename: item.file.name,
        }),
      });
      if (res.ok) {
        reservationToken = ""; // backend consumed it successfully
        setStaged((prev) => prev.filter((s) => s.key !== item.key));
        fetchJobs();
      } else {
        const text = await res.text();
        releaseReservation();
        updateStaged(item.key, { uploadPct: null, uploadError: requestFailureMessage(res.status, text) });
      }
    } catch {
      releaseReservation();
      const message = navigator.onLine
        ? "Could not reach the video server. It may be restarting, unavailable, or blocked by CORS. Please retry in a moment."
        : "Your internet connection appears to be offline. Reconnect and retry the upload.";
      updateStaged(item.key, { uploadPct: null, uploadError: message });
    }
  }

  async function submitQueue() {
    setError(null);
    if (!API_URL) {
      setError("Backend URL is not configured. Set NEXT_PUBLIC_API_URL in Vercel and redeploy the frontend.");
      return;
    }
    if (!sessionId) {
      setError("Your browser session is still initialising. Please try again in a moment.");
      return;
    }
    if (staged.length === 0) return;
    for (const item of staged) {
      if (!item.courseName.trim() || !item.unitNumber.trim() || !item.chapterNumber.trim()) {
        setError("Every queued video needs a course name, unit number and chapter number.");
        return;
      }
    }

    setSubmitting(true);
    // Keep browser-side upload concurrency at one by default. The backend
    // streams uploads to disk now, but serialising transfers still avoids a
    // burst of several large request bodies from one browser while a small
    // Railway service is also encoding video. Multiple users can still use
    // the app; processing itself remains governed by the backend queue.
    const CONCURRENCY = 1;
    const pending = [...staged];
    let cursor = 0;
    async function worker() {
      while (cursor < pending.length) {
        const item = pending[cursor];
        cursor += 1;
        await uploadOne(item);
      }
    }
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, pending.length) }, worker));
    setSubmitting(false);
  }

  async function saveToDrive(jobId: string) {
    // Optimistically flip the button to "saving" so it feels instant;
    // the next poll tick (≤2s) will confirm from the server either way.
    setJobs((prev) =>
      prev.map((j) => (j.job_id === jobId ? { ...j, drive_status: "saving", drive_error: null } : j))
    );
    try {
      const res = await fetch(`${API_URL}/jobs/${jobId}/drive`, {
        method: "POST",
        headers: { "X-Session-Id": sessionId },
      });
      if (!res.ok) {
        const detail = await res.text();
        throw new Error(detail || `${res.status}`);
      }
    } catch (err: any) {
      setJobs((prev) =>
        prev.map((j) =>
          j.job_id === jobId ? { ...j, drive_status: "failed", drive_error: err.message || "Failed to start save" } : j
        )
      );
    } finally {
      fetchJobs();
    }
  }

  const queuedCount = jobs.filter((j) => j.status === "queued").length;
  const processingCount = jobs.filter((j) => j.status === "processing").length;
  const doneCount = jobs.filter((j) => j.status === "done").length;
  const failedCount = jobs.filter((j) => j.status === "failed").length;

  return (
    <main>
      <header
        style={{
          borderBottom: "1px solid var(--border)",
          background: "rgba(10, 30, 43, 0.85)",
          backdropFilter: "blur(8px)",
          position: "sticky",
          top: 0,
          zIndex: 10,
        }}
      >
        <div className="container" style={{ paddingTop: 22, paddingBottom: 22 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", flexWrap: "wrap", gap: 14 }}>
            <div>
              <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0, letterSpacing: "-0.01em" }}>
                SLC Video Merger
              </h1>
              <p style={{ color: "var(--text-secondary)", margin: "4px 0 0", fontSize: 14 }}>
                Turn NotebookLM exports into branded, watermark-free lesson videos.
              </p>
            </div>
            <div className="header-stats">
              <StatPill label="Queued" value={queuedCount} tone="queued" />
              <StatPill label="Processing" value={processingCount} tone="processing" />
              <StatPill label="Done" value={doneCount} tone="success" />
              {failedCount > 0 && <StatPill label="Failed" value={failedCount} tone="danger" />}
            </div>
          </div>
        </div>
      </header>

      <div className="container" style={{ paddingTop: 28 }}>
        <div className="layout-grid">
          {/* ── Upload panel ───────────────────────────────────────── */}
          <section className="card upload-panel" style={{ padding: 20 }}>
            <h2 style={{ fontSize: 15, fontWeight: 600, margin: "0 0 16px" }}>Add videos</h2>

            <Field label="Awarding body">
              <input
                className="field-input"
                value={awardingBody}
                onChange={(e) => {
                  const value = e.target.value;
                  setAwardingBody(value);
                  setStaged((prev) => prev.map((item) => ({ ...item, awardingBody: value })));
                }}
                placeholder="e.g. Pearson"
              />
            </Field>

            <div style={{ marginTop: 14 }}>
              <Field label="Course name">
                <input
                  className="field-input"
                  value={courseName}
                  onChange={(e) => {
                    const value = e.target.value;
                    setCourseName(value);
                    setStaged((prev) => prev.map((item) => ({ ...item, courseName: value })));
                  }}
                  placeholder="e.g. Intro to Biology"
                />
              </Field>
            </div>

            <div
              className={`dropzone${dragging ? " dragging" : ""}`}
              style={{ marginTop: 16 }}
              onClick={() => fileInputRef.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                handleFilesPicked(e.dataTransfer.files);
              }}
            >
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept="video/mp4,video/quicktime,video/webm,video/x-matroska,video/x-msvideo"
                onChange={(e) => handleFilesPicked(e.target.files)}
                style={{ display: "none" }}
              />
              <div style={{ fontSize: 14, color: "var(--text)", fontWeight: 500 }}>
                Drop video files here, or click to browse
              </div>
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>
                MP4, MOV, WebM, MKV, AVI
              </div>
            </div>

            {staged.length > 0 && (
              <div style={{ marginTop: 18, display: "grid", gap: 8 }}>
                <div style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 600 }}>
                  {staged.length} staged
                </div>
                {staged.map((item) => (
                  <StagedRow key={item.key} item={item} onUpdate={updateStaged} onRemove={removeStaged} />
                ))}

                <button onClick={submitQueue} disabled={submitting} className="btn btn-primary" style={{ marginTop: 4, width: "100%" }}>
                  {submitting
                    ? "Uploading…"
                    : `Add ${staged.length} video${staged.length > 1 ? "s" : ""} to queue`}
                </button>
              </div>
            )}

            {error && (
              <div className="fade-in" style={{ color: "var(--danger)", fontSize: 13, marginTop: 12 }}>
                {error}
              </div>
            )}
          </section>

          {/* ── Queue panel ────────────────────────────────────────── */}
          <section>
            <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12, marginBottom: 14, flexWrap: "wrap" }}>
              <h2 style={{ fontSize: 15, fontWeight: 600, margin: 0 }}>My queue</h2>
              {queueInfo && (
                <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                  Team waiting: {queueInfo.queued}/{queueInfo.capacity} · Processing: {queueInfo.processing} · My active: {queueInfo.session_active}/{queueInfo.session_limit}
                </div>
              )}
            </div>

            {jobs.length === 0 && (
              <div className="card" style={{ padding: 32, textAlign: "center", color: "var(--text-muted)", fontSize: 14 }}>
                Nothing queued for this browser yet — add a video to get started.
              </div>
            )}

            <div style={{ display: "grid", gap: 10 }}>
              {jobs.map((job) => (
                <JobCard key={job.job_id} job={job} sessionId={sessionId} onSaveToDrive={saveToDrive} />
              ))}
            </div>
          </section>
        </div>
      </div>
    </main>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 13, color: "var(--text-secondary)", fontWeight: 500 }}>
      {label}
      {children}
    </label>
  );
}

function StagedRow({
  item,
  onUpdate,
  onRemove,
}: {
  item: StagedItem;
  onUpdate: (key: string, patch: Partial<StagedItem>) => void;
  onRemove: (key: string) => void;
}) {
  const isUploading = item.uploadPct !== null;
  return (
    <div
      style={{
        background: "var(--surface-raised)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-sm)",
        padding: "10px 10px",
      }}
    >
      <div className="staged-row">
        <div style={{ fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text-secondary)" }}>
          {item.file.name}
        </div>
        <input
          className="field-input"
          style={{ padding: "6px 8px", fontSize: 13 }}
          value={item.unitNumber.replace("Unit ", "")}
          onChange={(e) => onUpdate(item.key, { unitNumber: `Unit ${e.target.value.replace(/\D/g, "")}` })}
          placeholder="Unit #"
          disabled={isUploading}
        />
        <input
          className="field-input"
          style={{ padding: "6px 8px", fontSize: 13 }}
          value={item.chapterNumber.replace("Chapter ", "")}
          onChange={(e) => onUpdate(item.key, { chapterNumber: `Chapter ${e.target.value.replace(/\D/g, "")}` })}
          placeholder="Ch. #"
          disabled={isUploading}
        />
        <button
          onClick={() => onRemove(item.key)}
          disabled={isUploading}
          className="icon-btn"
          title="Remove"
          aria-label="Remove"
        >
          ✕
        </button>
      </div>

      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 6 }}>
        {item.unitNumber || "Unit ?"} · {item.chapterNumber || "Chapter ?"}
      </div>

      {isUploading && (
        <div style={{ marginTop: 8 }}>
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${item.uploadPct}%` }} />
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
            Uploading… {item.uploadPct}%
          </div>
        </div>
      )}

      {item.uploadError && (
        <div style={{ fontSize: 12, color: "var(--danger)", marginTop: 6 }}>{item.uploadError}</div>
      )}
    </div>
  );
}

function StatPill({ label, value, tone }: { label: string; value: number; tone: "queued" | "processing" | "success" | "danger" }) {
  const colors: Record<string, { fg: string; bg: string }> = {
    queued: { fg: "var(--queued)", bg: "var(--queued-soft)" },
    processing: { fg: "var(--accent)", bg: "var(--accent-soft)" },
    success: { fg: "var(--success)", bg: "var(--success-soft)" },
    danger: { fg: "var(--danger)", bg: "var(--danger-soft)" },
  };
  const c = colors[tone];
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 7,
        padding: "6px 12px",
        borderRadius: 999,
        background: c.bg,
        color: c.fg,
        fontSize: 12.5,
        fontWeight: 600,
      }}
    >
      {tone === "processing" && value > 0 && <span className="status-dot pulse" />}
      {value} {label}
    </div>
  );
}

function JobCard({
  job,
  sessionId,
  onSaveToDrive,
}: {
  job: Job;
  sessionId: string;
  onSaveToDrive: (jobId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [viewing, setViewing] = useState(false);
  const fileUrl = `${API_URL}/jobs/${job.job_id}/file?session_id=${encodeURIComponent(sessionId)}`;
  const accent = statusAccent(job.status);

  return (
    <div className="card fade-in" style={{ padding: 16, borderLeft: `3px solid ${accent}` }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 600, fontSize: 14.5, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {job.awarding_body ? `${job.awarding_body} · ` : ""}
            {job.course_name} · {job.unit_number}
            {job.chapter_number ? ` · ${job.chapter_number}` : ""}
          </div>
          <div style={{ fontSize: 12.5, color: "var(--text-muted)", marginTop: 2 }}>{job.original_filename}</div>
        </div>
        <StatusPill status={job.status} queuePosition={job.queue_position} />
      </div>

      {job.status === "processing" && (
        <div style={{ marginTop: 12 }}>
          <div className="progress-track">
            <div className="progress-fill indeterminate" />
          </div>
          {job.progress.length > 0 && (
            <div style={{ fontSize: 12.5, color: "var(--text-secondary)", marginTop: 6 }}>
              {job.progress[job.progress.length - 1]}
            </div>
          )}
        </div>
      )}

      {job.error && (
        <div style={{ color: "var(--danger)", fontSize: 13, marginTop: 10, background: "var(--danger-soft)", padding: "8px 10px", borderRadius: "var(--radius-sm)" }}>
          {job.error}
        </div>
      )}

      {job.progress.length > 0 && (
        <button onClick={() => setExpanded((v) => !v)} className="btn btn-ghost" style={{ marginTop: 8, fontSize: 12 }}>
          {expanded ? "Hide log" : "Show log"}
        </button>
      )}
      {expanded && (
        <div
          className="log-view fade-in"
          style={{
            marginTop: 6,
            background: "var(--bg)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-sm)",
            padding: 10,
            maxHeight: 160,
            overflowY: "auto",
            color: "var(--text-secondary)",
          }}
        >
          {job.progress.map((line, i) => (
            <div key={i}>{line}</div>
          ))}
        </div>
      )}

      {job.status === "done" && job.result_filename && (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
            <button onClick={() => setViewing((v) => !v)} className={viewing ? "btn btn-secondary" : "btn btn-primary"}>
              {viewing ? "Hide preview" : "View"}
            </button>

            <a href={fileUrl} className="btn btn-secondary">
              Download
            </a>

            {job.drive_status === "saved" ? (
              <a href={job.drive_link || "#"} target="_blank" rel="noreferrer" style={{ color: "var(--success)", fontSize: 13, fontWeight: 600, textDecoration: "none", marginLeft: 2 }}>
                ✓ Saved to Drive — open folder
              </a>
            ) : (
              <button onClick={() => onSaveToDrive(job.job_id)} disabled={job.drive_status === "saving"} className="btn btn-drive">
                {job.drive_status === "saving" ? "Saving to Drive…" : "Save to Google Drive"}
              </button>
            )}
          </div>

          {viewing && (
            <video
              key={job.job_id}
              controls
              autoPlay
              src={fileUrl}
              style={{ width: "100%", marginTop: 12, borderRadius: "var(--radius-md)", background: "#000", display: "block" }}
            />
          )}
        </>
      )}

      {job.drive_status === "failed" && job.drive_error && (
        <div style={{ color: "var(--danger)", fontSize: 12.5, marginTop: 8 }}>Drive save failed: {job.drive_error}</div>
      )}
    </div>
  );
}

function statusAccent(status: JobStatus): string {
  switch (status) {
    case "queued":
      return "var(--queued)";
    case "processing":
      return "var(--accent)";
    case "done":
      return "var(--success)";
    case "failed":
      return "var(--danger)";
  }
}

function StatusPill({ status, queuePosition }: { status: JobStatus; queuePosition?: number | null }) {
  const map: Record<JobStatus, { label: string; fg: string; bg: string; pulse?: boolean }> = {
    queued: { label: queuePosition ? `Queued — position ${queuePosition}` : "Queued", fg: "var(--queued)", bg: "var(--queued-soft)" },
    processing: { label: "Processing", fg: "var(--accent)", bg: "var(--accent-soft)", pulse: true },
    done: { label: "Done", fg: "var(--success)", bg: "var(--success-soft)" },
    failed: { label: "Failed", fg: "var(--danger)", bg: "var(--danger-soft)" },
  };
  const s = map[status];
  return (
    <span className="status-pill" style={{ background: s.bg, color: s.fg }}>
      <span className={`status-dot${s.pulse ? " pulse" : ""}`} />
      {s.label}
    </span>
  );
}
