import { useMemo, useRef, useState } from "react";
import { newBatchId, request, streamLines } from "./api.js";

const VERDICTS = {
  match: { label: "Match", symbol: "✓", rank: 0 },
  unclear: { label: "Unclear", symbol: "?", rank: 1 },
  no_match: { label: "No match", symbol: "✕", rank: 2 },
  error: { label: "Couldn’t check", symbol: "!", rank: 3 },
};
const MET = {
  yes: { label: "Meets", symbol: "✓" },
  no: { label: "Doesn’t meet", symbol: "✕" },
  unclear: { label: "Not mentioned", symbol: "?" },
};
const UPLOAD_CONCURRENCY = 3;

const isPdf = (file) => file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
const yesCount = (r) => r.conditions.filter((c) => c.met === "yes").length;

function sortResults(list) {
  return [...list].sort(
    (a, b) =>
      VERDICTS[a.verdict].rank - VERDICTS[b.verdict].rank ||
      yesCount(b) - yesCount(a) ||
      a.filename.localeCompare(b.filename)
  );
}

function downloadCsv(results, conditions, requirement) {
  // Excel treats cells starting with = + - @ as formulas; prefix ' to prevent CSV injection
  const cell = (v) => {
    let s = String(v ?? "");
    if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
    return `"${s.replace(/"/g, '""')}"`;
  };
  const header = ["File", "Verdict", "Summary", ...conditions.flatMap((c) => [c, `${c} (evidence)`])];
  const rows = results.map((r) => [
    r.filename,
    VERDICTS[r.verdict].label,
    r.summary,
    ...conditions.flatMap((_, i) => {
      const row = r.conditions[i];
      if (!row) return ["", ""];
      return [MET[row.met].label, row.evidence ? `${row.evidence}${row.page ? ` (page ${row.page})` : ""}` : ""];
    }),
  ]);
  const csv = "\uFEFF" + [[`Requirement: ${requirement}`], header, ...rows].map((r) => r.map(cell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: "screening-results.csv" });
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export default function App() {
  const batchId = useRef(newBatchId());
  const nextKey = useRef(1);
  const fileInput = useRef(null);
  const abortRef = useRef(null);

  // files: [{ key, filename, status: "uploading" | "ready" | "error", file_id?, pages?, error? }]
  const [files, setFiles] = useState([]);
  const [dragOver, setDragOver] = useState(false);

  const [requirement, setRequirement] = useState("");
  const [screening, setScreening] = useState(false);
  const [plan, setPlan] = useState(null); // { requirement, conditions, excluded }
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const [results, setResults] = useState({}); // file_id -> result
  const [screenError, setScreenError] = useState("");
  const [stale, setStale] = useState(false);
  const [filter, setFilter] = useState("all");

  const readyFiles = files.filter((f) => f.status === "ready");
  const uploadingCount = files.filter((f) => f.status === "uploading").length;
  const sorted = useMemo(() => sortResults(Object.values(results)), [results]);
  const counts = useMemo(() => {
    const c = { match: 0, unclear: 0, no_match: 0, error: 0 };
    sorted.forEach((r) => (c[r.verdict] += 1));
    return c;
  }, [sorted]);
  const shown = filter === "all" ? sorted : sorted.filter((r) => r.verdict === filter);

  const patchFile = (key, patch) => setFiles((fs) => fs.map((f) => (f.key === key ? { ...f, ...patch } : f)));

  // ── Upload ───────────────────────────────────────────────
  const uploadOne = async (item) => {
    const form = new FormData();
    form.append("files", item.file);
    try {
      const data = await request(`/batches/${batchId.current}/files`, { method: "POST", body: form });
      const info = data.files?.[0] || {};
      if (info.error) patchFile(item.key, { status: "error", error: info.error });
      else patchFile(item.key, { status: "ready", file_id: info.file_id, pages: info.pages });
    } catch (err) {
      patchFile(item.key, { status: "error", error: err.message });
    }
  };

  const addFiles = async (fileList) => {
    const items = [...(fileList || [])].map((file) => ({
      key: nextKey.current++,
      file,
      filename: file.name,
      status: isPdf(file) ? "uploading" : "error",
      error: isPdf(file) ? "" : "Not a PDF. Only .pdf files can be screened.",
    }));
    if (!items.length) return;
    setFiles((fs) => [...fs, ...items.map(({ file, ...rest }) => rest)]);
    if (Object.keys(results).length) setStale(true);

    const queue = items.filter((i) => i.status === "uploading");
    const worker = async () => {
      while (queue.length) await uploadOne(queue.shift());
    };
    await Promise.all(Array.from({ length: UPLOAD_CONCURRENCY }, worker));
  };

  const removeFile = async (f) => {
    setFiles((fs) => fs.filter((x) => x.key !== f.key));
    if (f.file_id) {
      setResults((r) => {
        const { [f.file_id]: _, ...rest } = r;
        return rest;
      });
      try {
        await request(`/batches/${batchId.current}/files/${f.file_id}`, { method: "DELETE" });
      } catch {
        // Fine if the server no longer has this file
      }
    }
  };

  const removeAll = async () => {
    setFiles([]);
    setResults({});
    setPlan(null);
    setStale(false);
    setScreenError("");
    try {
      await request(`/batches/${batchId.current}`, { method: "DELETE" });
    } catch {
      // Ignore errors: the local list is already cleared
    }
  };

  // ── Screen ───────────────────────────────────────────────
  const runScreen = async () => {
    const req = requirement.trim();
    if (!req || !readyFiles.length || screening || uploadingCount) return;

    const controller = new AbortController();
    abortRef.current = controller;
    setScreening(true);
    setScreenError("");
    setResults({});
    setPlan({ requirement: req, conditions: null, excluded: [] });
    setProgress({ done: 0, total: readyFiles.length });
    setStale(false);
    setFilter("all");

    try {
      const stream = streamLines(`/batches/${batchId.current}/screen`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ requirement: req }),
        signal: controller.signal,
      });
      for await (const msg of stream) {
        if (msg.type === "start") {
          setPlan({ requirement: req, conditions: msg.conditions, excluded: msg.excluded || [] });
          setProgress({ done: 0, total: msg.total });
        } else if (msg.type === "result") {
          setResults((r) => ({ ...r, [msg.file_id]: msg }));
          setProgress((p) => ({ ...p, done: p.done + 1 }));
        } else if (msg.type === "error") {
          setScreenError(msg.detail);
        }
      }
    } catch (err) {
      if (err.name !== "AbortError") {
        setScreenError(err.message);
        // The server restarted and lost the files: clear the list so they get uploaded again
        if (err.status === 404) setFiles((fs) => fs.filter((f) => f.status !== "ready"));
      }
    } finally {
      setScreening(false);
      abortRef.current = null;
    }
  };

  const stopScreen = () => abortRef.current?.abort();

  const onRequirementKeyDown = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.nativeEvent.isComposing) {
      e.preventDefault();
      runScreen();
    }
  };

  const busy = screening || uploadingCount > 0;
  const hasResults = sorted.length > 0;

  return (
    <div className="page">
      <header className="masthead">
        <h1>AI Résumé Screener</h1>
        <p>
          Upload a batch of résumés, describe who you’re looking for, and see which ones fit, with the
          lines that back each decision. Everything runs on your computer.
        </p>
      </header>

      <main className="layout">
        {/* ── Files ── */}
        <section className="files-panel" aria-label="Files">
          <div
            className={`dropzone${dragOver ? " is-over" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              if (!screening) addFiles(e.dataTransfer.files);
            }}
          >
            <p className="dropzone-text">Drop PDFs here, or choose several at once.</p>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => fileInput.current?.click()}
              disabled={screening}
            >
              Choose PDFs
            </button>
            <input
              ref={fileInput}
              type="file"
              accept="application/pdf,.pdf"
              multiple
              hidden
              onChange={(e) => {
                addFiles(e.target.files);
                e.target.value = "";
              }}
            />
          </div>

          {files.length > 0 && (
            <>
              <div className="files-head">
                <p className="files-count" role="status">
                  {readyFiles.length} {readyFiles.length === 1 ? "file" : "files"} ready
                  {uploadingCount > 0 && `, ${uploadingCount} uploading`}
                </p>
                <button type="button" className="link-btn" onClick={removeAll} disabled={screening}>
                  Remove all
                </button>
              </div>
              <ul className="file-list">
                {files.map((f) => (
                  <li key={f.key} className={`file file-${f.status}`}>
                    <div className="file-main">
                      <span className="file-name" title={f.filename}>
                        {f.filename}
                      </span>
                      <span className="file-meta">
                        {f.status === "uploading" && "Uploading…"}
                        {f.status === "ready" && `${f.pages} ${f.pages === 1 ? "page" : "pages"}`}
                        {f.status === "error" && f.error}
                      </span>
                    </div>
                    <button
                      type="button"
                      className="remove-btn"
                      onClick={() => removeFile(f)}
                      disabled={screening || f.status === "uploading"}
                      aria-label={`Remove ${f.filename}`}
                      title="Remove"
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>

        {/* ── Requirement + results ── */}
        <section className="screen-panel" aria-label="Screening">
          <div className="ask-box">
            <label htmlFor="requirement" className="ask-label">
              Who are you looking for?
            </label>
            <textarea
              id="requirement"
              rows={3}
              value={requirement}
              onChange={(e) => setRequirement(e.target.value)}
              onKeyDown={onRequirementKeyDown}
              placeholder="For example: GPA above 3.5, at least one internship, and experience with Python"
            />
            <div className="ask-row">
              <span className="hint">Ctrl or ⌘ + Enter to start</span>
              {screening ? (
                <button type="button" className="btn btn-quiet" onClick={stopScreen}>
                  Stop
                </button>
              ) : (
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={runScreen}
                  disabled={busy || !readyFiles.length || !requirement.trim()}
                >
                  {readyFiles.length
                    ? `Screen ${readyFiles.length} ${readyFiles.length === 1 ? "file" : "files"}`
                    : "Screen files"}
                </button>
              )}
            </div>
          </div>

          {screenError && (
            <p className="error" role="alert">
              {screenError}
            </p>
          )}

          {!plan && !screenError && (
            <p className="empty">
              {readyFiles.length
                ? "Describe the requirements in plain language. Each one is checked separately in every file."
                : "Add the résumés you want to compare, then describe who you’re looking for."}
            </p>
          )}

          {plan && (
            <div className="plan">
              {plan.conditions === null ? (
                <p className="pending" role="status">
                  Breaking your request into separate checks
                  <span className="dots" aria-hidden="true" />
                </p>
              ) : (
                <>
                  {plan.conditions.length > 0 && (
                    <>
                      <p className="plan-title">Each file is checked for</p>
                      <ul className="plan-list">
                        {plan.conditions.map((c) => (
                          <li key={c}>{c}</li>
                        ))}
                      </ul>
                    </>
                  )}
                  {plan.excluded.length > 0 && (
                    <p className="plan-excluded">
                      Not checked: {plan.excluded.join(", ")}. Personal characteristics such as age, gender or
                      nationality aren’t used to screen candidates.
                    </p>
                  )}
                </>
              )}

              {(screening || progress.done > 0) && progress.total > 0 && (
                <div className="progress">
                  <progress max={progress.total} value={progress.done} aria-label="Screening progress" />
                  <span role="status">
                    {screening
                      ? `Checked ${progress.done} of ${progress.total} files`
                      : progress.done < progress.total
                        ? `Stopped after ${progress.done} of ${progress.total} files`
                        : `Checked all ${progress.total} files`}
                  </span>
                </div>
              )}
            </div>
          )}

          {stale && hasResults && !screening && (
            <p className="notice">The file list changed after this screening. Run it again to include every file.</p>
          )}

          {hasResults && (
            <>
              <div className="results-bar">
                <div className="filters" role="group" aria-label="Filter results">
                  {[
                    ["all", "All", sorted.length],
                    ["match", "Match", counts.match],
                    ["unclear", "Unclear", counts.unclear],
                    ["no_match", "No match", counts.no_match],
                    ...(counts.error ? [["error", "Couldn’t check", counts.error]] : []),
                  ].map(([key, label, n]) => (
                    <button
                      key={key}
                      type="button"
                      className={`filter${filter === key ? " is-active" : ""}`}
                      aria-pressed={filter === key}
                      onClick={() => setFilter(key)}
                    >
                      {label} <span className="filter-n">{n}</span>
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  className="btn btn-quiet"
                  onClick={() => downloadCsv(sorted, plan?.conditions || [], plan?.requirement || "")}
                  disabled={screening}
                >
                  Export CSV
                </button>
              </div>

              {shown.length === 0 ? (
                <p className="empty">No files in this group.</p>
              ) : (
                <ol className="results">
                  {shown.map((r) => (
                    <li key={r.file_id} className={`result verdict-${r.verdict}`}>
                      <details>
                        <summary>
                          <span className="badge">
                            <span aria-hidden="true">{VERDICTS[r.verdict].symbol}</span> {VERDICTS[r.verdict].label}
                          </span>
                          <span className="result-name" title={r.filename}>
                            {r.filename}
                          </span>
                          {r.conditions.length > 0 && (
                            <span className="result-score">
                              {yesCount(r)} of {r.conditions.length} met
                            </span>
                          )}
                          {r.summary && <span className="result-summary">{r.summary}</span>}
                        </summary>

                        {r.conditions.length > 0 && (
                          <ul className="checks">
                            {r.conditions.map((c, i) => (
                              <li key={i} className={`check met-${c.met}`}>
                                <p className="check-head">
                                  <span className="check-symbol" aria-hidden="true">
                                    {MET[c.met].symbol}
                                  </span>
                                  <span className="visually-hidden">{MET[c.met].label}: </span>
                                  <span className="check-condition">{c.condition}</span>
                                  {c.note && <span className="check-note">{c.note}</span>}
                                </p>
                                {c.evidence && (
                                  <blockquote className="evidence">
                                    {c.page && <span className="evidence-page">Page {c.page}</span>}
                                    <mark>{c.evidence}</mark>
                                    {c.verified === false && (
                                      <span className="evidence-warning">
                                        This quote wasn’t found word for word in the file. Check it yourself.
                                      </span>
                                    )}
                                  </blockquote>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}
                      </details>
                    </li>
                  ))}
                </ol>
              )}

              <p className="disclaimer">
                Use these results to decide which résumés to read first, not to reject anyone automatically.
                A small local model can misread a file.
              </p>
            </>
          )}
        </section>
      </main>
    </div>
  );
}
