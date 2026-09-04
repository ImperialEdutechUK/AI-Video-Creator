"use client";

import { useEffect, useRef, useState } from "react";

// Set NEXT_PUBLIC_API_URL in Vercel's project settings to your Railway
// backend URL, e.g. https://slc-merger-backend-production.up.railway.app
const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type JobStatus = "queued" | "processing" | "done" | "failed";

type DriveStatus = "idle" | "saving" | "saved" | "failed";

interface Job {
  job_id: string;
  status: JobStatus;
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

export default function Home() {
  const [awardingBody, setAwardingBody] = useState("");
  const [courseName, setCourseName] = useState("");
  const [staged, setStaged] = useState<StagedItem[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Poll the shared queue continuously — this is a team queue, not a
  // per-session one, so everyone sees the same list and it survives reloads.
  useEffect(() => {
    fetchJobs();
    pollRef.current = setInterval(fetchJobs, 2000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  async function fetchJobs() {
    try {
      const res = await fetch(`${API_URL}/jobs`);
      if (!res.ok) return;
      const data = await res.json();
      setJobs(data.jobs || []);
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

  // Uploads a single staged item via XHR (instead of fetch) so we get real
  // upload-progress events — "adding to queue" is really a full video
  // upload to the backend, and for a large file that transfer time is the
  // actual bottleneck, not server logic. Showing live % makes that visible
  // instead of leaving the button on an unexplained "Queuing…".
  function uploadOne(item: StagedItem): Promise<void> {
    return new Promise((resolve) => {
      const form = new FormData();
      form.append("course_name", item.courseName);
      form.append("unit_number", item.unitNumber);
      form.append("chapter_number", item.chapterNumber);
      form.append("awarding_body", item.awardingBody);
      form.append("video", item.file);

      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${API_URL}/jobs`);

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          updateStaged(item.key, { uploadPct: Math.round((e.loaded / e.total) * 100) });
        }
      };

      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          setStaged((prev) => prev.filter((s) => s.key !== item.key));
          fetchJobs();
        } else {
          updateStaged(item.key, {
            uploadPct: null,
            uploadError: xhr.responseText || `Upload failed (${xhr.status})`,
          });
        }
        resolve();
      };

      xhr.onerror = () => {
        updateStaged(item.key, { uploadPct: null, uploadError: "Network error during upload" });
        resolve();
      };

      updateStaged(item.key, { uploadPct: 0, uploadError: null });
      xhr.send(form);
    });
  }

  async function submitQueue() {
    setError(null);
    if (staged.length === 0) return;
    for (const item of staged) {
      if (!item.courseName.trim() || !item.unitNumber.trim() || !item.chapterNumber.trim()) {
        setError("Every queued video needs a course name, unit number and chapter number.");
        return;
      }
    }

    setSubmitting(true);
    // Upload a few files at once instead of one-at-a-time: waiting for
    // each full video body to finish uploading before starting the next
    // was the main reason "adding to queue" felt slow with more than one
    // file staged.
    const CONCURRENCY = 3;
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
      const res = await fetch(`${API_URL}/jobs/${jobId}/drive`, { method: "POST" });
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
            <h2 style={{ fontSize: 15, fontWeight: 600, margin: "0 0 14px" }}>Queue</h2>

            {jobs.length === 0 && (
              <div className="card" style={{ padding: 32, textAlign: "center", color: "var(--text-muted)", fontSize: 14 }}>
                Nothing queued yet — add a video to get started.
              </div>
            )}

            <div style={{ display: "grid", gap: 10 }}>
              {jobs.map((job) => (
                <JobCard key={job.job_id} job={job} onSaveToDrive={saveToDrive} />
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

function JobCard({ job, onSaveToDrive }: { job: Job; onSaveToDrive: (jobId: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  const [viewing, setViewing] = useState(false);
  const fileUrl = `${API_URL}/jobs/${job.job_id}/file`;
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
        <StatusPill status={job.status} />
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

function StatusPill({ status }: { status: JobStatus }) {
  const map: Record<JobStatus, { label: string; fg: string; bg: string; pulse?: boolean }> = {
    queued: { label: "Queued", fg: "var(--queued)", bg: "var(--queued-soft)" },
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
