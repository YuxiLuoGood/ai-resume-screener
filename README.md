# AI Résumé Screener

Upload a batch of résumés (PDF), describe who you’re looking for in plain language, and see which candidates fit. Each decision comes with the exact lines from the résumé that support it. Everything runs locally with no API key, and the files never leave your machine.

Example request: *“GPA above 3.5, at least one internship, and experience with Python”*

## How it works
Plain vector search can’t reliably answer “GPA above 3.5”, so the app checks every file instead of retrieving a few:

1. **Split the request into conditions, once.** The model turns the request into a short list of separate, checkable conditions. Every file is then checked against the same list, so results are comparable and can be exported as a table.
2. **Check each file against each condition.** For every résumé, the model answers each condition with *meets*, *doesn’t meet* or *not mentioned*. It may compare numbers and add up dates, and it must quote the line it relied on.
3. **Compute the verdict in code, not in the model.** Any condition not met gives **No match**. Otherwise, any condition not mentioned gives **Unclear**. All conditions met gives **Match**.
4. **Verify the quotes.** Every quote is searched for in the original text. If it isn’t found word for word, the UI flags it so a person can check.

Results stream in as each file finishes, sorted with matches first, and can be filtered or exported to CSV.

```
Browser (React, :5173)
   │  /api/*  (Vite dev proxy)
   ▼
FastAPI (:8000)
   ├─ POST /batches/{id}/files   PyMuPDF → text per page (long files are also indexed in FAISS)
   └─ POST /batches/{id}/screen  1. split request → conditions        (1 LLM call)
                                 2. per file: full text (or top passages for long files)
                                    + conditions → judgments + quotes (1 LLM call per file)
                                 3. verify quotes, compute verdict    (code)
                                 → NDJSON stream: start, result × N, done
```

## Fairness by design
- Conditions about personal characteristics (age, gender, race, nationality, religion, marital or family status, disability, appearance) are identified when the request is split, shown as “Not checked”, and never evaluated.
- File names, which are often candidate names, are not sent to the model.
- The verdict rule is fixed in code and every judgment shows its evidence, so decisions can be audited.
- The UI states that results are for prioritizing which résumés to read, not for rejecting candidates automatically. A small local model can misread a file. Laws on automated hiring tools vary by location (for example, New York City’s Local Law 144), so check what applies before using this on real applicants.

## Tech Stack
- **React + Vite** – frontend
- **FastAPI** – REST API with a streaming endpoint
- **LangChain** – LLM calls, text splitting, vector store
- **Ollama + Qwen2.5** – local LLM inference in JSON mode
- **FAISS + sentence-transformers** – retrieval for long documents only
- **PyMuPDF** – PDF text extraction

## Project Structure
```
ai-resume-screener/
├── backend/
│   ├── main.py              # FastAPI app, storage, LLM calls, streaming
│   ├── screening.py         # prompts, JSON parsing, quote check, verdict rules
│   ├── tests/
│   │   └── test_screening.py
│   ├── requirements.txt
│   └── .env.example
├── demo/                    # fictional sample résumés + DEMO-QUESTIONS.md
└── frontend/
    ├── index.html
    ├── package.json
    ├── vite.config.js       # proxies /api to the backend
    └── src/
        ├── main.jsx
        ├── App.jsx
        ├── App.css
        └── api.js           # fetch helpers, NDJSON stream reader
```

## Prerequisites
- **Python 3.10–3.12**
- **Node.js 18+**
- **[Ollama](https://ollama.com/download)**

## Setup

### 1. Pull the model
```bash
ollama pull qwen2.5:3b
```
`qwen2.5:7b` is noticeably more accurate if your machine can run it (set `LLM_MODEL=qwen2.5:7b`).

### 2. Start the backend
```bash
cd backend
python -m venv .venv
# macOS / Linux:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
uvicorn main:app --reload
```

### 3. Start the frontend (in a second terminal)
```bash
cd frontend
npm install
npm run dev
```
Open **http://localhost:5173**.

### Try it with the sample résumés
The `demo/` folder has nine fictional résumés: English and Chinese, a two-column layout, missing GPAs, an edge-case GPA of 3.55, and a scanned PDF with no text layer. `demo/DEMO-QUESTIONS.md` lists ready-made requests and the expected result for every file.

### Run the tests
```bash
cd backend
python -m unittest discover tests
```

## How long does it take?
There is one model call per file. As a rough guide, expect a few seconds per résumé on Apple Silicon or a machine with a GPU, and 10–30 seconds per résumé on a laptop CPU. Results appear as each file finishes, and you can stop at any time.

## Configuration
All settings are optional. Copy `backend/.env.example` to `backend/.env`, or set environment variables.

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_MODEL` | `qwen2.5:3b` | Ollama model |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Where Ollama is running |
| `SCREEN_WORKERS` | `2` | Files checked at the same time. Raise it only if Ollama runs with `OLLAMA_NUM_PARALLEL` > 1 |
| `NUM_CTX` | `8192` | Context window passed to Ollama; its default is small and silently cuts long prompts |
| `FULL_CONTEXT_TOKENS` | `4000` | Files up to about this many tokens are sent in full; longer files send only the most relevant passages |
| `TOP_K` | `6` | Passages used for long files |
| `EMBEDDING_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | Used only for long files; downloaded on first use (about 470 MB) |
| `HF_ENDPOINT` | — | Set to `https://hf-mirror.com` if Hugging Face downloads are slow or blocked |
| `MAX_FILE_MB` | `20` | Largest PDF accepted |
| `MAX_FILES_PER_BATCH` | `200` | Files per batch |
| `MAX_BATCHES` | `10` | Batches kept in memory; the least recently used is dropped first |
| `ALLOWED_ORIGINS` | `http://localhost:5173,…` | Origins allowed by CORS |

## API

Each browser tab creates its own random `batch_id`, so different users don’t see each other’s files.

| Method | Endpoint | Body | Returns |
|--------|----------|------|---------|
| GET | `/` | — | Status and model name |
| POST | `/batches/{batch_id}/files` | multipart, one or more `files` | Per file: `file_id`, `filename`, `pages`, or `error` |
| GET | `/batches/{batch_id}/files` | — | Files in the batch |
| DELETE | `/batches/{batch_id}/files/{file_id}` | — | Removes one file |
| DELETE | `/batches/{batch_id}` | — | Removes the batch |
| POST | `/batches/{batch_id}/screen` | JSON `{ "requirement": "..." }` | NDJSON stream (below) |

The screening stream sends one JSON object per line:
```
{"type": "start", "total": 3, "conditions": ["GPA above 3.5", "..."], "excluded": ["under 30"]}
{"type": "result", "file_id": "...", "filename": "a.pdf", "verdict": "match", "summary": "...",
 "conditions": [{"condition": "GPA above 3.5", "met": "yes", "note": "3.86 > 3.5",
                 "evidence": "GPA: 3.86 / 4.0", "page": 1, "verified": true}]}
{"type": "done", "counts": {"match": 1, "unclear": 1, "no_match": 1, "error": 0}}
```

### Example (bash)
```bash
B=demo12345
curl -X POST http://localhost:8000/batches/$B/files -F "files=@alice.pdf" -F "files=@bob.pdf"
curl -N -X POST http://localhost:8000/batches/$B/screen \
  -H "Content-Type: application/json" \
  -d '{"requirement": "GPA above 3.5 and Python experience"}'
```
Interactive docs: **http://localhost:8000/docs**

## Limitations
- Files live in memory; restarting the backend clears them.
- Scanned (image-only) PDFs have no text layer and are rejected with a message; run OCR first.
- A 3B model can still misjudge a condition. Read the evidence before acting on a result, especially for close calls.

## Troubleshooting
| Symptom | Fix |
|---------|-----|
| “Can’t reach the backend” | Start the backend on port 8000 |
| “Can’t reach Ollama” or “model isn’t downloaded” | Start Ollama and run `ollama pull <LLM_MODEL>` |
| “These files are no longer on the server” | The backend restarted; upload the files again |
| Many results are **Unclear** | The résumés may not state that information, or the model is too small; try `qwen2.5:7b` |
| `faiss-cpu` fails to install | Use Python 3.10–3.12 |
