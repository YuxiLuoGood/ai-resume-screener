// Defaults to /api in development (proxied to the backend by vite.config.js); set VITE_API_URL for a deployed backend
const API_BASE = (import.meta.env.VITE_API_URL || "/api").replace(/\/$/, "");

const UNREACHABLE =
  "Can’t reach the backend. Start it with `uvicorn main:app --reload` in the backend folder.";

function httpError(message, status) {
  const err = new Error(message);
  err.status = status;
  return err;
}

async function send(path, options) {
  try {
    return await fetch(`${API_BASE}${path}`, options);
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw httpError(UNREACHABLE, 0);
  }
}

async function errorFrom(res) {
  let data = null;
  try {
    data = await res.json();
  } catch {
    // Non-JSON response (e.g. the dev proxy couldn't reach the backend)
  }
  const detail = data?.detail;
  if (Array.isArray(detail)) return httpError(detail.map((d) => d.msg).join("; "), res.status);
  if (detail) return httpError(detail, res.status);
  return httpError(
    res.status >= 500
      ? `The backend returned an error (${res.status}). Check that it’s running and look at its terminal output.`
      : `Request failed (${res.status}).`,
    res.status
  );
}

export async function request(path, options) {
  const res = await send(path, options);
  if (!res.ok) throw await errorFrom(res);
  return res.json();
}

/** Read an NDJSON stream and yield one parsed object per line. */
export async function* streamLines(path, options) {
  const res = await send(path, options);
  if (!res.ok) throw await errorFrom(res);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) yield JSON.parse(line);
    }
  }
  if (buffer.trim()) yield JSON.parse(buffer);
}

export function newBatchId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
