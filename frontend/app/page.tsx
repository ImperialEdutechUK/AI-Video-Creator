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
  created_at: number;
  drive_status: DriveStatus;
  drive_error: string | null;
  drive_link: string | null;
}

interface StagedItem {
  key: string;
  file: File;
  courseName: string;
  unitNumber: string;
  chapterNumber: string;
}

export default function Home() {
  const [courseName, setCourseName] = useState("");
  const [staged, setStaged] = useState<StagedItem[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
      courseName,
      unitNumber: `Unit ${startUnit + i}`,
      chapterNumber: "",
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
    try {
      // Fire uploads sequentially — the backend queues processing anyway,
      // but sequential uploads avoid saturating upload bandwidth on large
      // files all at once.
      for (const item of staged) {
        const form = new FormData();
        form.append("course_name", item.courseName);
        form.append("unit_number", item.unitNumber);
        form.append("chapter_number", item.chapterNumber);
        form.append("video", item.file);
        const res = await fetch(`${API_URL}/jobs`, { method: "POST", body: form });
        if (!res.ok) {
          const detail = await res.text();
          throw new Error(`${item.file.name}: ${detail || res.status}`);
        }
      }
      setStaged([]);
      fetchJobs();
    } catch (err: any) {
      setError(err.message || "Failed to queue one or more videos.");
    } finally {
      setSubmitting(false);
    }
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

  const activeCount = jobs.filter((j) => j.status === "queued" || j.status === "processing").length;

  return (
    <main style={{ maxWidth: 760, margin: "0 auto", padding: "48px 24px" }}>
      <h1 style={{ fontSize: 28, marginBottom: 4 }}>🎬 SLC Video Merger</h1>
      <p style={{ color: "#9fc4d4", marginTop: 0, marginBottom: 32 }}>
        Queue up several NotebookLM exports at once — they process one at a
        time in the background so nothing slows down from contention.
      </p>

      <section style={{ marginBottom: 32 }}>
        <label style={fieldLabel}>
          Course name (applied to new videos you add below)
          <input
            style={inputStyle}
            value={courseName}
            onChange={(e) => {
              const value = e.target.value;
              setCourseName(value);
              setStaged((prev) => prev.map((item) => ({ ...item, courseName: value })));
            }}
            placeholder="e.g. Intro to Biology"
          />
        </label>

        <div style={{ marginTop: 16 }}>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept="video/mp4,video/quicktime,video/webm,video/x-matroska,video/x-msvideo"
            onChange={(e) => handleFilesPicked(e.target.files)}
          />
        </div>

        {staged.length > 0 && (
          <div style={{ marginTop: 20, display: "grid", gap: 10 }}>
            {staged.map((item) => (
              <div
                key={item.key}
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 120px 120px 32px",
                  gap: 8,
                  alignItems: "center",
                  background: "#0d3b54",
                  borderRadius: 8,
                  padding: "8px 10px",
                }}
              >
                <div style={{ fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  📄 {item.file.name}
                </div>
                <input
                  style={{ ...inputStyle, padding: "6px 8px", fontSize: 13 }}
                  value={item.unitNumber.replace("Unit ", "")}
                  onChange={(e) => updateStaged(item.key, { unitNumber: `Unit ${e.target.value.replace(/\D/g, "")}` })}
                  placeholder="1"
                />
                <input
                  style={{ ...inputStyle, padding: "6px 8px", fontSize: 13 }}
                  value={item.chapterNumber.replace("Chapter ", "")}
                  onChange={(e) => updateStaged(item.key, { chapterNumber: `Chapter ${e.target.value.replace(/\D/g, "")}` })}
                  placeholder="1"
                />
                <div style={{ gridColumn: "1 / span 3", fontSize: 12, color: "#9fc4d4" }}>
                  {item.unitNumber || "Unit ?"} | {item.chapterNumber || "Chapter ?"}
                </div>
                <button
                  onClick={() => removeStaged(item.key)}
                  title="Remove"
                  style={{
                    background: "transparent",
                    border: "none",
                    color: "#ff8a8a",
                    cursor: "pointer",
                    fontSize: 16,
                  }}
                >
                  ✕
                </button>
              </div>
            ))}

            <button
              onClick={submitQueue}
              disabled={submitting}
              style={{ ...buttonStyle(submitting), marginTop: 8, justifySelf: "start" }}
            >
              {submitting
                ? "Queuing…"
                : `Add ${staged.length} video${staged.length > 1 ? "s" : ""} to queue`}
            </button>
          </div>
        )}

        {error && <p style={{ color: "#ff8a8a", marginTop: 12 }}>⚠️ {error}</p>}
      </section>

      <section>
        <h2 style={{ fontSize: 18, marginBottom: 12 }}>
          Queue {activeCount > 0 && <span style={{ color: "#60ccbe" }}>({activeCount} active)</span>}
        </h2>

        {jobs.length === 0 && (
          <p style={{ color: "#6f97a6" }}>Nothing queued yet — add a video above.</p>
        )}

        <div style={{ display: "grid", gap: 12 }}>
          {jobs.map((job) => (
            <JobCard key={job.job_id} job={job} onSaveToDrive={saveToDrive} />
          ))}
        </div>
      </section>
    </main>
  );
}

function JobCard({ job, onSaveToDrive }: { job: Job; onSaveToDrive: (jobId: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  const badge = statusBadge(job.status);

  return (
    <div style={{ background: "#0d3b54", borderRadius: 10, padding: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 600, fontSize: 15, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {job.course_name} — {job.unit_number}
            {job.chapter_number ? ` — ${job.chapter_number}` : ""}
          </div>
          <div style={{ fontSize: 12, color: "#7fa9b8" }}>{job.original_filename}</div>
        </div>
        <span
          style={{
            ...badge,
            padding: "4px 10px",
            borderRadius: 999,
            fontSize: 12,
            fontWeight: 600,
            whiteSpace: "nowrap",
          }}
        >
          {job.status}
        </span>
      </div>

      {job.status === "processing" && job.progress.length > 0 && (
        <div style={{ fontSize: 12, color: "#9fc4d4", marginTop: 8 }}>
          {job.progress[job.progress.length - 1]}
        </div>
      )}

      {job.error && <div style={{ color: "#ff8a8a", fontSize: 13, marginTop: 8 }}>⚠️ {job.error}</div>}

      {job.progress.length > 0 && (
        <button
          onClick={() => setExpanded((v) => !v)}
          style={{ background: "none", border: "none", color: "#60ccbe", fontSize: 12, cursor: "pointer", padding: 0, marginTop: 8 }}
        >
          {expanded ? "Hide log" : "Show log"}
        </button>
      )}
      {expanded && (
        <div
          style={{
            marginTop: 8,
            background: "#062a30",
            borderRadius: 6,
            padding: 10,
            fontFamily: "monospace",
            fontSize: 12,
            maxHeight: 160,
            overflowY: "auto",
          }}
        >
          {job.progress.map((line, i) => (
            <div key={i}>{line}</div>
          ))}
        </div>
      )}

      {job.status === "done" && job.result_filename && (
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 12, flexWrap: "wrap" }}>
          <a
            href={`${API_URL}/jobs/${job.job_id}/file`}
            style={{ ...buttonStyle(false), display: "inline-block", textDecoration: "none", padding: "8px 16px", fontSize: 13 }}
          >
            ⬇ Download
          </a>

          {job.drive_status === "saved" ? (
            <a
              href={job.drive_link || "#"}
              target="_blank"
              rel="noreferrer"
              style={{ color: "#8fe8b0", fontSize: 13, textDecoration: "none" }}
            >
              ✅ Saved to Google Drive — open folder
            </a>
          ) : (
            <button
              onClick={() => onSaveToDrive(job.job_id)}
              disabled={job.drive_status === "saving"}
              style={{
                ...buttonStyle(job.drive_status === "saving"),
                background: job.drive_status === "saving" ? "#3a6a76" : "#4285F4",
                color: "#fff",
                padding: "8px 16px",
                fontSize: 13,
              }}
            >
              {job.drive_status === "saving" ? "Saving to Drive…" : "📁 Save to Google Drive"}
            </button>
          )}
        </div>
      )}

      {job.drive_status === "failed" && job.drive_error && (
        <div style={{ color: "#ff8a8a", fontSize: 12, marginTop: 6 }}>
          ⚠️ Drive save failed: {job.drive_error}
        </div>
      )}
    </div>
  );
}

function statusBadge(status: JobStatus): React.CSSProperties {
  switch (status) {
    case "queued":
      return { background: "#3a4a56", color: "#cfe8f0" };
    case "processing":
      return { background: "#2a5a6e", color: "#8fe8d8" };
    case "done":
      return { background: "#1f5c3a", color: "#8fe8b0" };
    case "failed":
      return { background: "#5c2323", color: "#ff8a8a" };
  }
}

const fieldLabel: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  fontSize: 14,
  color: "#cfe8f0",
  gap: 4,
};

const inputStyle: React.CSSProperties = {
  padding: "10px 12px",
  borderRadius: 8,
  border: "1px solid #1c5670",
  background: "#08303f",
  color: "#fff",
  fontSize: 15,
};

function buttonStyle(disabled: boolean): React.CSSProperties {
  return {
    padding: "12px 20px",
    borderRadius: 8,
    border: "none",
    background: disabled ? "#3a6a76" : "#60ccbe",
    color: "#062a30",
    fontWeight: 600,
    fontSize: 15,
    cursor: disabled ? "not-allowed" : "pointer",
  };
}
