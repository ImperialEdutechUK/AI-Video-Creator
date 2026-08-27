"use client";

import { useEffect, useRef, useState } from "react";

// Set NEXT_PUBLIC_API_URL in Vercel's project settings to your Railway
// backend URL, e.g. https://slc-merger-backend-production.up.railway.app
const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type JobStatus = "idle" | "uploading" | "pending" | "processing" | "done" | "failed";

export default function Home() {
  const [courseName, setCourseName] = useState("");
  const [unitNumber, setUnitNumber] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [status, setStatus] = useState<JobStatus>("idle");
  const [progress, setProgress] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [resultFilename, setResultFilename] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setResultFilename(null);
    setProgress([]);

    if (!file) {
      setError("Choose a video file first.");
      return;
    }
    if (!courseName.trim() || !unitNumber.trim()) {
      setError("Course name and unit number are required.");
      return;
    }

    const form = new FormData();
    form.append("course_name", courseName);
    form.append("unit_number", unitNumber);
    form.append("video", file);

    setStatus("uploading");
    try {
      const res = await fetch(`${API_URL}/jobs`, { method: "POST", body: form });
      if (!res.ok) {
        const detail = await res.text();
        throw new Error(detail || `Upload failed (${res.status})`);
      }
      const data = await res.json();
      setJobId(data.job_id);
      setStatus("pending");
      startPolling(data.job_id);
    } catch (err: any) {
      setStatus("failed");
      setError(err.message || "Upload failed.");
    }
  }

  function startPolling(id: string) {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const res = await fetch(`${API_URL}/jobs/${id}`);
        if (!res.ok) return;
        const data = await res.json();
        setStatus(data.status);
        setProgress(data.progress || []);
        if (data.status === "done") {
          setResultFilename(data.result_filename);
          if (pollRef.current) clearInterval(pollRef.current);
        } else if (data.status === "failed") {
          setError(data.error || "Processing failed.");
          if (pollRef.current) clearInterval(pollRef.current);
        }
      } catch {
        // transient network hiccup — next tick will retry
      }
    }, 2000);
  }

  const busy = status === "uploading" || status === "pending" || status === "processing";

  return (
    <main style={{ maxWidth: 640, margin: "0 auto", padding: "48px 24px" }}>
      <h1 style={{ fontSize: 28, marginBottom: 4 }}>🎬 SLC Video Merger</h1>
      <p style={{ color: "#9fc4d4", marginTop: 0, marginBottom: 32 }}>
        Upload a NotebookLM export — we&apos;ll add the intro/outro, strip the
        watermark, and hand back a branded MP4.
      </p>

      <form onSubmit={handleSubmit} style={{ display: "grid", gap: 16 }}>
        <label style={fieldLabel}>
          Course name
          <input
            style={inputStyle}
            value={courseName}
            onChange={(e) => setCourseName(e.target.value)}
            disabled={busy}
            placeholder="e.g. Intro to Biology"
          />
        </label>

        <label style={fieldLabel}>
          Unit number
          <input
            style={inputStyle}
            value={unitNumber}
            onChange={(e) => setUnitNumber(e.target.value)}
            disabled={busy}
            placeholder="e.g. Unit 3"
          />
        </label>

        <label style={fieldLabel}>
          Video file
          <input
            type="file"
            accept="video/mp4,video/quicktime,video/webm,video/x-matroska,video/x-msvideo"
            onChange={(e) => setFile(e.target.files?.[0] || null)}
            disabled={busy}
            style={{ marginTop: 6 }}
          />
        </label>

        <button type="submit" disabled={busy} style={buttonStyle(busy)}>
          {busy ? "Working…" : "Upload & Merge"}
        </button>
      </form>

      {error && (
        <p style={{ color: "#ff8a8a", marginTop: 20 }}>⚠️ {error}</p>
      )}

      {jobId && status !== "idle" && (
        <div style={{ marginTop: 32 }}>
          <h3 style={{ marginBottom: 8 }}>Status: {status}</h3>
          <div
            style={{
              background: "#0d3b54",
              borderRadius: 8,
              padding: 12,
              maxHeight: 220,
              overflowY: "auto",
              fontFamily: "monospace",
              fontSize: 13,
              lineHeight: 1.6,
            }}
          >
            {progress.length === 0 && <div style={{ opacity: 0.6 }}>Waiting for the job to start…</div>}
            {progress.map((line, i) => (
              <div key={i}>{line}</div>
            ))}
          </div>

          {status === "done" && resultFilename && (
            <a
              href={`${API_URL}/jobs/${jobId}/file`}
              style={{ ...buttonStyle(false), display: "inline-block", marginTop: 16, textDecoration: "none" }}
            >
              ⬇ Download {resultFilename}
            </a>
          )}
        </div>
      )}
    </main>
  );
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
  background: "#0d3b54",
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
