"""
AI Résumé Screener - backend
Upload a batch of PDFs (e.g. résumés), describe the requirements, and check each file
against them, with evidence quoted from the source text.
FastAPI + LangChain + Ollama (runs locally, no API key required)

Run:  uvicorn main:app --reload
Docs: http://localhost:8000/docs
"""
import json
import os
import re
import threading
import urllib.request
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

from dotenv import load_dotenv

# Load .env before importing LangChain / Hugging Face so settings such as HF_ENDPOINT take effect
load_dotenv()

from fastapi import FastAPI, File, HTTPException, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from langchain_community.vectorstores import FAISS  # noqa: E402
from langchain_core.documents import Document  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402
from langchain_huggingface import HuggingFaceEmbeddings  # noqa: E402
from langchain_ollama import ChatOllama  # noqa: E402
from langchain_text_splitters import RecursiveCharacterTextSplitter  # noqa: E402

import screening as sc  # noqa: E402

try:  # PyMuPDF >= 1.24 exposes "pymupdf"; older versions only "fitz"
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf

# ── Settings (override with environment variables or backend/.env) ────────────
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:3b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000",
    ).split(",")
    if o.strip()
]
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "20"))
MAX_FILES_PER_BATCH = int(os.getenv("MAX_FILES_PER_BATCH", "200"))
MAX_BATCHES = int(os.getenv("MAX_BATCHES", "10"))  # batches kept in memory; least recently used evicted first
NUM_CTX = int(os.getenv("NUM_CTX", "8192"))  # Ollama's default context window is small and cuts long prompts
# Documents estimated at or below this many tokens (a 1–3 page résumé) are sent in full;
# longer ones send only the most relevant passages.
FULL_CONTEXT_TOKENS = int(os.getenv("FULL_CONTEXT_TOKENS", "4000"))
TOP_K = int(os.getenv("TOP_K", "6"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "80"))
SCREEN_WORKERS = max(1, int(os.getenv("SCREEN_WORKERS", "2")))  # files checked concurrently

# ── App ──────────────────────────────────────────────────────
app = FastAPI(title="AI Résumé Screener", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# format="json" makes Ollama return valid JSON only
llm = ChatOllama(
    model=LLM_MODEL, base_url=OLLAMA_BASE_URL, temperature=0, num_ctx=NUM_CTX, format="json"
)
print(f"[init] LLM: {LLM_MODEL} via {OLLAMA_BASE_URL} (num_ctx={NUM_CTX})")

# Chinese punctuation is included so Chinese sentences aren't split mid-sentence.
splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    separators=["\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "，", ", ", " ", ""],
)

# The embedding model is only needed for long documents, so it loads lazily:
# screening short résumés starts faster and needs no model download.
_embeddings = None
_embeddings_lock = threading.Lock()


def get_embeddings():
    global _embeddings
    with _embeddings_lock:
        if _embeddings is None:
            print(f"[init] Loading embedding model: {EMBEDDING_MODEL}")
            _embeddings = HuggingFaceEmbeddings(
                model_name=EMBEDDING_MODEL, encode_kwargs={"normalize_embeddings": True}
            )
        return _embeddings


# ── Storage: batch_id -> {file_id -> file data}; one batch per browser tab, kept separate ──
_batches: "OrderedDict[str, OrderedDict[str, dict]]" = OrderedDict()
_lock = threading.Lock()
BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _check_batch_id(batch_id: str) -> None:
    if not BATCH_ID_RE.match(batch_id):
        raise HTTPException(status_code=400, detail="Invalid batch id.")


def _add_file(batch_id: str, file_id: str, entry: dict) -> bool:
    with _lock:
        batch = _batches.setdefault(batch_id, OrderedDict())
        _batches.move_to_end(batch_id)
        while len(_batches) > MAX_BATCHES:
            _batches.popitem(last=False)
        if len(batch) >= MAX_FILES_PER_BATCH:
            return False
        batch[file_id] = entry
        return True


def _snapshot(batch_id: str):
    """Return [(file_id, entry)], or None if the batch does not exist."""
    with _lock:
        batch = _batches.get(batch_id)
        if batch is None:
            return None
        _batches.move_to_end(batch_id)
        return list(batch.items())


def _public(file_id: str, entry: dict) -> dict:
    return {
        "file_id": file_id,
        "filename": entry["filename"],
        "pages": entry["pages"],
        "long_document": entry["vectorstore"] is not None,
    }


# ── PDF processing ────────────────────────────────────────────
def _read_pdf_pages(data: bytes, filename: str):
    pages = []
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        if pdf.needs_pass:
            raise ValueError("This PDF is password-protected.")
        total = len(pdf)
        for i, page in enumerate(pdf):
            # sort=True returns text in reading order, so labels and values in two-column résumés stay together
            text = page.get_text(sort=True)
            if text.strip():
                pages.append(Document(page_content=text, metadata={"source": filename, "page": i + 1}))
    return pages, total


def _ingest(upload: UploadFile) -> dict:
    filename = upload.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise ValueError("Only PDF files are supported.")

    max_bytes = MAX_FILE_MB * 1024 * 1024
    data = upload.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"File is larger than {MAX_FILE_MB} MB.")
    if b"%PDF" not in data[:1024]:
        raise ValueError("This file is not a valid PDF.")

    try:
        pages, total = _read_pdf_pages(data, filename)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Could not read the PDF ({e}).")
    if not pages:
        raise ValueError("No text found. It may be a scanned image; run OCR on it first.")

    tokens = sc.estimate_tokens("".join(p.page_content for p in pages))
    vectorstore = None
    if tokens > FULL_CONTEXT_TOKENS:
        vectorstore = FAISS.from_documents(splitter.split_documents(pages), get_embeddings())

    return {
        "filename": filename,
        "pages": total,
        "page_docs": pages,
        "norm_pages": [(p.metadata["page"], sc.normalize_for_search(p.page_content)) for p in pages],
        "tokens": tokens,
        "vectorstore": vectorstore,
    }


# ── LLM calls ─────────────────────────────────────────────────
def _ask_json(system: str, user: str):
    """Call the model and parse its JSON; if the format is wrong, ask once more."""
    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    raw = llm.invoke(messages).content
    data = sc.parse_json_object(raw)
    if data is None:
        messages += [
            AIMessage(content=raw),
            HumanMessage(content="That was not valid JSON in the required shape. Reply with the JSON object only."),
        ]
        data = sc.parse_json_object(llm.invoke(messages).content)
    return data


def _split_requirement(requirement: str):
    return sc.parse_conditions(_ask_json(sc.SPLIT_PROMPT, requirement), requirement)


def _judge(conditions, entry) -> dict:
    if entry["vectorstore"] is None:
        context_docs = entry["page_docs"]
    else:
        hits = entry["vectorstore"].similarity_search(" ; ".join(conditions), k=TOP_K)
        context_docs = sorted(hits, key=lambda d: d.metadata.get("page", 0))

    # File names are not sent to the model: they are usually candidate names and could bias the judgment
    data = _ask_json(sc.JUDGE_PROMPT, sc.build_judge_message(conditions, sc.format_context(context_docs)))
    if data is None:
        return {
            "verdict": "error",
            "summary": "The model’s reply couldn’t be read. Try again, or use a larger model.",
            "conditions": [],
        }
    return sc.build_result(data, conditions, entry["norm_pages"])


def _check_ollama() -> None:
    """Before screening, check that Ollama is running and the model is downloaded; give a clear fix if not."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=3) as r:
            data = json.load(r)
    except Exception:
        raise HTTPException(
            status_code=503, detail=f"Can’t reach Ollama at {OLLAMA_BASE_URL}. Start Ollama and try again."
        )
    names = set()
    for m in data.get("models", []):
        names.update(filter(None, (m.get("name"), m.get("model"))))
    if LLM_MODEL not in names and f"{LLM_MODEL}:latest" not in names:
        raise HTTPException(
            status_code=503, detail=f"The model {LLM_MODEL} isn’t downloaded. Run: ollama pull {LLM_MODEL}"
        )


def _line(obj) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _screen_stream(requirement: str, files):
    """Emit NDJSON, one JSON object per line: start, one result per file, done. The UI renders lines as they arrive."""
    try:
        conditions, excluded = _split_requirement(requirement)
    except Exception as e:
        yield _line({"type": "error", "detail": f"Could not read the requirement ({e})."})
        return

    yield _line({"type": "start", "total": len(files), "conditions": conditions, "excluded": excluded})
    if not conditions:
        yield _line({
            "type": "error",
            "detail": "None of the conditions are job-related, so nothing was checked. Describe skills, experience or education instead.",
        })
        return

    counts = {"match": 0, "unclear": 0, "no_match": 0, "error": 0}
    pool = ThreadPoolExecutor(max_workers=SCREEN_WORKERS)
    try:
        futures = {pool.submit(_judge, conditions, entry): (fid, entry) for fid, entry in files}
        for future in as_completed(futures):
            fid, entry = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = {"verdict": "error", "summary": f"Could not check this file ({e}).", "conditions": []}
            counts[result["verdict"]] += 1
            yield _line({"type": "result", "file_id": fid, "filename": entry["filename"], **result})
        yield _line({"type": "done", "counts": counts})
    finally:
        # If the user clicks Stop or the browser disconnects, cancel tasks that haven't started
        pool.shutdown(wait=False, cancel_futures=True)


# ── Routes ───────────────────────────────────────────────────
# Endpoints are plain def, not async def: PDF parsing and model inference block,
# so FastAPI runs them in its thread pool instead of on the event loop.


@app.get("/")
def root():
    return {
        "status": "AI Résumé Screener is running 🚀",
        "llm_model": LLM_MODEL,
        "num_ctx": NUM_CTX,
        "batches_in_memory": len(_batches),
    }


@app.post("/batches/{batch_id}/files")
def upload_files(batch_id: str, files: List[UploadFile] = File(...)):
    """Upload one or more PDFs. A failing file doesn't affect the others; its error is in that file's error field."""
    _check_batch_id(batch_id)
    results = []
    for upload in files:
        try:
            entry = _ingest(upload)
        except ValueError as e:
            results.append({"filename": upload.filename, "error": str(e)})
            continue
        except Exception as e:  # e.g. the embedding model for long documents failed to download
            results.append({"filename": upload.filename, "error": f"Could not process this file ({e})."})
            continue
        file_id = uuid.uuid4().hex
        if not _add_file(batch_id, file_id, entry):
            results.append({"filename": upload.filename, "error": f"A batch can hold at most {MAX_FILES_PER_BATCH} files."})
            continue
        results.append(_public(file_id, entry))
    return {"files": results}


@app.get("/batches/{batch_id}/files")
def list_files(batch_id: str):
    _check_batch_id(batch_id)
    return {"files": [_public(fid, e) for fid, e in (_snapshot(batch_id) or [])]}


@app.delete("/batches/{batch_id}/files/{file_id}")
def delete_file(batch_id: str, file_id: str):
    _check_batch_id(batch_id)
    with _lock:
        batch = _batches.get(batch_id)
        if batch is None or batch.pop(file_id, None) is None:
            raise HTTPException(status_code=404, detail="File not found.")
    return {"deleted": file_id}


@app.delete("/batches/{batch_id}")
def delete_batch(batch_id: str):
    _check_batch_id(batch_id)
    with _lock:
        _batches.pop(batch_id, None)
    return {"deleted": batch_id}


class ScreenRequest(BaseModel):
    requirement: str = Field(..., min_length=1, max_length=2000)


@app.post("/batches/{batch_id}/screen")
def screen(batch_id: str, req: ScreenRequest):
    """Check every file against the requirement. Returns an application/x-ndjson stream, one JSON object per line."""
    _check_batch_id(batch_id)
    requirement = req.requirement.strip()
    if not requirement:
        raise HTTPException(status_code=400, detail="Describe what you’re looking for first.")

    files = _snapshot(batch_id)
    if files is None:
        raise HTTPException(
            status_code=404,
            detail="These files are no longer on the server (it may have restarted). Upload them again.",
        )
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one PDF first.")

    _check_ollama()
    return StreamingResponse(
        _screen_stream(requirement, files),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
