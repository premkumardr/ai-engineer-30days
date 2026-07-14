# -*- coding: utf-8 -*-
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import os, time, json, logging, io, re

# Load .env file automatically (works both locally and in Docker)
try:
    from dotenv import load_dotenv
    load_dotenv()
    logging.getLogger(__name__).info(".env loaded")
except ImportError:
    pass  # python-dotenv not installed — rely on shell env vars

from .database import init_db, engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------
OPENAI_KEY   = os.getenv("OPENAI_API_KEY", "")
EMBED_MODEL  = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
GEN_MODEL    = os.getenv("OPENAI_GENERATION_MODEL", "gpt-4o")
RERANK_MODEL = os.getenv("OPENAI_RERANK_MODEL", "gpt-4o-mini")
IS_LIVE      = bool(
    OPENAI_KEY
    and not OPENAI_KEY.startswith("sk-your")
    and OPENAI_KEY not in ("demo", "sk-your-key-here", "sk-your-openai-key-here", "")
    and len(OPENAI_KEY) > 20
)

_mem_store:   dict[str, list] = {}
_query_count: dict[str, int]  = {}

_oai = None
if IS_LIVE:
    try:
        from openai import AsyncOpenAI
        _oai = AsyncOpenAI(api_key=OPENAI_KEY)
        logger.info("OpenAI client initialised — live AI mode")
    except ImportError:
        logger.warning("openai package not found — demo mode")
        IS_LIVE = False

app = FastAPI(title="RAG System", version="1.0.0")

# CORS — the storefront widget (frontend served on another port) calls /assistant/*
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo only — lock down to the storefront origin in production
    allow_methods=["*"],
    allow_headers=["*"],
)

from .assistant import router as assistant_router
app.include_router(assistant_router)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class IngestRequest(BaseModel):
    tenant_id: str
    document_id: str
    content: str
    source_page: int = 1

class QueryRequest(BaseModel):
    tenant_id: str
    query: str
    top_k: int = 5
    accept_format: str = "auto"   # "auto" | "text" | "json"

class QueryResponse(BaseModel):
    query: str; answer: str; citations: list
    confidence: float; processing_time: float
    tokens_used: int; mode: str
    response_format: str          # "text" | "json"

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    try:
        await init_db()
        logger.info("DB initialised")
    except Exception as e:
        logger.warning(f"DB init skipped ({e}) — in-memory mode")

# ---------------------------------------------------------------------------
# File parsing helpers
# ---------------------------------------------------------------------------
def _extract_text(filename: str, data: bytes) -> str:
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(data))
            total_pages = len(reader.pages)
            MAX_PAGES = 1000
            pages = reader.pages[:MAX_PAGES]
            text = "\n".join(p.extract_text() or "" for p in pages)
            if total_pages > MAX_PAGES:
                text += f"\n\n[Note: PDF has {total_pages} pages. Only the first {MAX_PAGES} pages were ingested.]"
            return text
        except Exception as e:
            raise HTTPException(400, f"PDF parse error: {e}")
    elif ext == "docx":
        try:
            import docx
            doc = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            raise HTTPException(400, f"DOCX parse error: {e}")
    elif ext in ("xlsx", "xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            rows = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    line = " | ".join(str(c) for c in row if c is not None)
                    if line.strip():
                        rows.append(line)
            return "\n".join(rows)
        except Exception as e:
            raise HTTPException(400, f"Excel parse error: {e}")
    elif ext in ("txt", "md", "csv"):
        try:
            return data.decode("utf-8", errors="replace")
        except Exception as e:
            raise HTTPException(400, f"Text parse error: {e}")
    elif ext == "json":
        try:
            raw = data.decode("utf-8", errors="replace")
            # Pretty-print so content is readable for embedding/search
            parsed = json.loads(raw)
            return json.dumps(parsed, indent=2, ensure_ascii=False)
        except Exception as e:
            raise HTTPException(400, f"JSON parse error: {e}")
    else:
        raise HTTPException(400, f"Unsupported file type: .{ext}  (pdf, docx, xlsx, txt, json supported)")

# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------
def _detect_format(query: str, accept_format: str) -> str:
    """Return 'json' or 'text' based on user preference or query content."""
    if accept_format in ("json", "text"):
        return accept_format
    # Auto-detect: if query looks like JSON or explicitly requests JSON format
    q = query.strip()
    json_hints = ("json", "as json", "in json", "return json", "output json",
                  "give json", "structured", "as object", "key value")
    if q.startswith("{") or q.startswith("[") or any(h in q.lower() for h in json_hints):
        return "json"
    return "text"

# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

# Queries that should never be answered regardless of document content
_BLOCKED_PATTERNS = re.compile(
    r'\b(hack|exploit|malware|ransomware|bomb|weapon|poison|kill|murder|'
    r'suicide|self.harm|drug synthesis|synthesize drugs|illegal|'
    r'credit card number|social security|ssn|password)\b',
    re.IGNORECASE
)

# Phrases that signal the user wants general knowledge, not document Q&A
_OFF_TOPIC_PATTERNS = re.compile(
    r'\b(tell me a joke|write a poem|what is the capital|who is the president|'
    r'weather today|stock price|news today|recipe for|how to cook)\b',
    re.IGNORECASE
)

MAX_QUERY_LENGTH = 1000   # characters
MIN_QUERY_LENGTH = 3

class GuardrailResult:
    def __init__(self, allowed: bool, reason: str = ""):
        self.allowed = allowed
        self.reason  = reason

def _check_input_guardrails(query: str) -> GuardrailResult:
    """Validate query before processing."""
    q = query.strip()

    if len(q) < MIN_QUERY_LENGTH:
        return GuardrailResult(False, "Query is too short. Please ask a specific question.")

    if len(q) > MAX_QUERY_LENGTH:
        return GuardrailResult(False,
            f"Query exceeds {MAX_QUERY_LENGTH} characters. Please be more concise.")

    if _BLOCKED_PATTERNS.search(q):
        return GuardrailResult(False,
            "This query contains content that cannot be processed by this system.")

    if _OFF_TOPIC_PATTERNS.search(q):
        return GuardrailResult(False,
            "This system only answers questions about your uploaded documents. "
            "Please ask something relevant to the ingested content.")

    return GuardrailResult(True)

def _check_output_guardrails(answer: str, chunks: list[dict]) -> str:
    """
    Verify the answer is grounded in the retrieved chunks.
    If the model has hallucinated (answer shares almost no words with any chunk),
    replace with a safe fallback.
    """
    if not chunks or not answer:
        return answer

    # Build a word set from all chunk content
    chunk_words = set()
    for c in chunks:
        chunk_words.update(re.findall(r'\b\w{4,}\b', c.get("content","").lower()))

    # Count how many significant words in the answer appear in the source docs
    answer_words = re.findall(r'\b\w{4,}\b', answer.lower())
    if not answer_words:
        return answer

    overlap = sum(1 for w in answer_words if w in chunk_words)
    ratio   = overlap / len(answer_words)

    # If less than 15% of answer words appear in source documents, flag it
    if ratio < 0.15:
        logger.warning(f"Output guardrail triggered: overlap={ratio:.2%}")
        return (
            "I can only answer based on the documents you have uploaded. "
            "The retrieved content does not appear to contain enough information "
            "to answer this question confidently. Please upload a more relevant document."
        )
    return answer

# ---------------------------------------------------------------------------
# AI helpers
# ---------------------------------------------------------------------------
async def _embed(text: str) -> list[float]:
    resp = await _oai.embeddings.create(model=EMBED_MODEL, input=text[:8000])
    return resp.data[0].embedding

async def _generate(query: str, chunks: list[dict], response_format: str = "text") -> dict:
    ctx = "\n\n".join(
        f"[Doc: {c['document_id']}, Page {c.get('source_page',1)}]\n{c['content']}"
        for c in chunks
    )
    if response_format == "json":
        fmt_instruction = (
            "Reply ONLY with a valid JSON object with keys: "
            "\"answer\" (string), \"key_points\" (array of strings), "
            "\"sources\" (array of doc ids). No markdown, no code fences."
        )
    else:
        fmt_instruction = (
            "Reply in plain text. Cite sources as [Doc: <id>, Page <n>]."
        )

    system_prompt = (
        "You are a document Q&A assistant with STRICT grounding rules:\n"
        "1. Answer ONLY using information found in the provided document excerpts below.\n"
        "2. If the answer is not present in the documents, respond EXACTLY with: "
        "\"The provided documents do not contain information to answer this question.\"\n"
        "3. Do NOT use your training knowledge to supplement or fill gaps.\n"
        "4. Do NOT speculate, infer, or make assumptions beyond what the text states.\n"
        "5. Do NOT answer questions unrelated to the document content.\n"
        f"6. {fmt_instruction}"
    )

    resp = await _oai.chat.completions.create(
        model=GEN_MODEL, temperature=0.0, max_tokens=1024,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": f"Document excerpts:\n{ctx}\n\nQuestion: {query}"}
        ]
    )
    return {"answer": resp.choices[0].message.content,
            "tokens_used": resp.usage.total_tokens}

async def _rerank(query: str, chunks: list[dict], top_k: int) -> list[dict]:
    if not chunks or len(chunks) <= top_k:
        return chunks[:top_k]
    numbered = "\n".join(f"{i+1}. {c['content'][:200]}" for i, c in enumerate(chunks))
    resp = await _oai.chat.completions.create(
        model=RERANK_MODEL, temperature=0, max_tokens=128,
        messages=[{"role": "user", "content":
            f"Query: {query}\nRank these {len(chunks)} passages by relevance "
            f"(most relevant first). Reply with ONLY a JSON array of 1-based indices.\n\n{numbered}"}]
    )
    try:
        order = json.loads(resp.choices[0].message.content.strip())
        return [chunks[i-1] for i in order if 1 <= i <= len(chunks)][:top_k]
    except Exception:
        return chunks[:top_k]

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
async def _db_ingest(req: IngestRequest, embedding: list | None):
    from sqlalchemy import text
    emb_str = f"'[{','.join(str(x) for x in embedding)}]'" if embedding else "NULL"
    async with engine.begin() as conn:
        await conn.execute(text(f"""
            INSERT INTO documents (tenant_id, document_id, content, source_page, embedding)
            VALUES (:tenant, :doc_id, :content, :page, {emb_str}::vector)
            ON CONFLICT (tenant_id, document_id)
            DO UPDATE SET content=EXCLUDED.content, source_page=EXCLUDED.source_page,
                          embedding={emb_str}::vector, updated_at=NOW()
        """), {"tenant": req.tenant_id, "doc_id": req.document_id,
               "content": req.content, "page": req.source_page})

async def _db_search(tenant_id: str, query: str, embedding: list | None, top_k: int) -> list[dict]:
    from sqlalchemy import text
    async with engine.begin() as conn:
        if embedding:
            emb_str = f"'[{','.join(str(x) for x in embedding)}]'"
            r = await conn.execute(text(f"""
                SELECT document_id, content, source_page,
                       1-(embedding <=> {emb_str}::vector) AS score
                FROM documents WHERE tenant_id=:t AND embedding IS NOT NULL
                ORDER BY embedding <=> {emb_str}::vector LIMIT :k
            """), {"t": tenant_id, "k": top_k * 2})
        else:
            # keyword fallback: search all words >=2 chars
            kw = query.split()[0] if query.split() else query
            r = await conn.execute(text("""
                SELECT document_id, content, source_page, 0.75 AS score
                FROM documents WHERE tenant_id=:t AND content ILIKE :q LIMIT :k
            """), {"t": tenant_id, "q": f"%{kw}%", "k": top_k * 2})
        return [dict(row._mapping) for row in r]

async def _db_stats(tenant_id: str) -> dict:
    from sqlalchemy import text
    async with engine.begin() as conn:
        r = await conn.execute(text(
            "SELECT COUNT(*) as docs, SUM(length(content)/200+1) as chunks "
            "FROM documents WHERE tenant_id=:t"), {"t": tenant_id})
        row = dict(r.mappings().first() or {})
    return {"total_documents": row.get("docs", 0), "total_chunks": row.get("chunks", 0)}

# ---------------------------------------------------------------------------
# Memory helpers
# ---------------------------------------------------------------------------
def _mem_ingest(req: IngestRequest, embedding):
    docs = _mem_store.setdefault(req.tenant_id, [])
    _mem_store[req.tenant_id] = [d for d in docs if d["document_id"] != req.document_id]
    _mem_store[req.tenant_id].append({
        "document_id": req.document_id, "content": req.content,
        "source_page": req.source_page, "_emb": embedding})

def _mem_search(tenant_id: str, query: str, top_k: int) -> list[dict]:
    """
    Improved keyword search that works on short words, numbers, and financial terms.
    Falls back to full document scan so uploaded docs are always found.
    """
    q_lower = query.lower()
    # Split into tokens — keep anything >=2 chars (catches INR, ID, etc.)
    q_words = [w for w in re.split(r'[\s,]+', q_lower) if len(w) >= 2]
    all_docs = _mem_store.get(tenant_id, [])

    if not all_docs:
        return []

    scored = []
    for d in all_docs:
        c_lower = d["content"].lower()
        # Count how many query words appear in the document
        hits = sum(1 for w in q_words if w in c_lower)
        # Bonus: exact phrase match
        phrase_bonus = 2 if q_lower in c_lower else 0
        total = hits + phrase_bonus
        scored.append((total, d))

    scored.sort(key=lambda x: x[0], reverse=True)

    # If nothing matched by keyword, return all docs (let the answer explain)
    if scored[0][0] == 0:
        return all_docs[:top_k]

    return [d for _, d in scored[:top_k * 2] if _ > 0][:top_k] or all_docs[:top_k]

# ---------------------------------------------------------------------------
# Core ingest logic (shared by JSON and file upload routes)
# ---------------------------------------------------------------------------
async def _do_ingest(tenant_id: str, document_id: str, content: str, source_page: int):
    start = time.time()
    req = IngestRequest(tenant_id=tenant_id, document_id=document_id,
                        content=content, source_page=source_page)
    embedding, embed_info = None, "no embedding (demo mode)"

    if IS_LIVE:
        try:
            embedding = await _embed(content)
            embed_info = EMBED_MODEL
        except Exception as e:
            logger.warning(f"Embed failed: {e}")

    db_ok = False
    if engine is not None:
        try:
            await _db_ingest(req, embedding)
            db_ok = True
        except Exception as e:
            logger.warning(f"DB write failed ({e})")

    _mem_ingest(req, embedding)

    return {
        "status": "success",
        "document_id": document_id,
        "tenant_id": tenant_id,
        "chunks_created": max(1, len(content) // 200),
        "embedding_model": embed_info,
        "storage": ("pgvector + memory" if (db_ok and embedding) else
                    "pgvector (no embed)" if db_ok else "in-memory"),
        "elapsed_s": round(time.time() - start, 3),
    }

# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    key_status = "not set"
    if OPENAI_KEY:
        if IS_LIVE:
            key_status = f"set ({OPENAI_KEY[:8]}...)"
        else:
            key_status = "set but looks like a placeholder — update .env with a real key"
    return {
        "status":      "healthy",
        "service":     "RAG System",
        "version":     "1.0.0",
        "ai_mode":     "live" if IS_LIVE else "demo",
        "openai_key":  key_status,
        "database":    "connected" if engine else "unavailable (in-memory)",
        "embed_model": EMBED_MODEL if IS_LIVE else "none",
        "gen_model":   GEN_MODEL   if IS_LIVE else "none",
    }

@app.post("/ingest")
async def ingest_json(req: IngestRequest):
    return await _do_ingest(req.tenant_id, req.document_id, req.content, req.source_page)

@app.post("/upload")
async def upload_file(
    tenant_id:   str        = Form(...),
    document_id: str        = Form(""),
    source_page: int        = Form(1),
    file:        UploadFile = File(...),
):
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "File too large — max 10 MB")

    text = _extract_text(file.filename or "file.txt", data)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if not text:
        raise HTTPException(422, "No text could be extracted from the file")

    doc_id = document_id.strip() or re.sub(r'[^a-zA-Z0-9_-]', '_',
                                            (file.filename or "doc").rsplit(".", 1)[0])
    result = await _do_ingest(tenant_id, doc_id, text, source_page)
    result["filename"]   = file.filename
    result["chars"]      = len(text)
    result["preview"]    = text[:300]
    return result

@app.post("/query", response_model=QueryResponse)
async def query_documents(req: QueryRequest):
    start = time.time()
    mode = "live" if IS_LIVE else "demo"
    response_format = _detect_format(req.query, req.accept_format)
    embedding = None

    if IS_LIVE:
        try:
            embedding = await _embed(req.query)
        except Exception as e:
            logger.warning(f"Query embed failed: {e}")

    chunks = []
    if engine is not None:
        try:
            chunks = await _db_search(req.tenant_id, req.query, embedding, req.top_k)
        except Exception as e:
            logger.warning(f"DB search failed ({e})")

    if not chunks:
        chunks = _mem_search(req.tenant_id, req.query, req.top_k)

    if not chunks:
        return QueryResponse(query=req.query,
            answer="No documents found. Upload a document first.",
            citations=[], confidence=0.0,
            processing_time=round(time.time()-start, 4),
            tokens_used=0, mode=mode, response_format=response_format)

    if IS_LIVE:
        try:
            chunks = await _rerank(req.query, chunks, req.top_k)
        except Exception as e:
            logger.warning(f"Rerank failed: {e}")
            chunks = chunks[:req.top_k]
    else:
        chunks = chunks[:req.top_k]

    answer, tokens_used = "", 0
    if IS_LIVE:
        try:
            gen = await _generate(req.query, chunks, response_format)
            answer, tokens_used = gen["answer"], gen["tokens_used"]
        except Exception as e:
            logger.warning(f"Generation failed: {e}")

    if not answer:
        mode = "demo"
        top = chunks[0]
        content = top.get("content", "")
        q_lower = req.query.lower()

        lines = [l.strip() for l in content.splitlines() if l.strip()]
        q_words = [w for w in re.split(r'[\s,]+', q_lower) if len(w) >= 2]
        best_line, best_score = "", 0
        for line in lines:
            score = sum(1 for w in q_words if w in line.lower())
            if score > best_score:
                best_score, best_line = score, line

        excerpt = best_line if best_line else content[:400]

        if response_format == "json":
            answer = json.dumps({
                "answer": excerpt,
                "key_points": [l.strip() for l in lines[:5] if l.strip()],
                "sources": [top.get("document_id", "unknown")],
                "note": "Keyword-based answer — results may be limited without semantic search"
            }, indent=2)
        else:
            answer = (
                f"Based on document '{top.get('document_id', 'doc')}':\n\n"
                f"{excerpt}"
            )

    citations = [{
        "source":     c.get("document_id", "unknown"),
        "page":       c.get("source_page", c.get("page", 1)),
        "snippet":    c.get("content", "")[:160] + "...",
        "confidence": round(float(c.get("score", 0.85)), 3),
    } for c in chunks[:5]]

    avg_conf = sum(c["confidence"] for c in citations) / len(citations) if citations else 0.0
    _query_count[req.tenant_id] = _query_count.get(req.tenant_id, 0) + 1

    return QueryResponse(query=req.query, answer=answer, citations=citations,
        confidence=round(avg_conf, 3),
        processing_time=round(time.time()-start, 4),
        tokens_used=tokens_used, mode=mode, response_format=response_format)

@app.get("/stats/{tenant_id}")
async def get_stats(tenant_id: str):
    db_stats = {"total_documents": 0, "total_chunks": 0}
    if engine is not None:
        try:
            db_stats = await _db_stats(tenant_id)
        except Exception:
            pass
    if not db_stats["total_documents"]:
        mem = _mem_store.get(tenant_id, [])
        db_stats = {"total_documents": len(mem),
                    "total_chunks": sum(max(1, len(d["content"])//200) for d in mem)}
    # Return document names list for the UI
    doc_names = [d["document_id"] for d in _mem_store.get(tenant_id, [])]
    return {
        "total_documents":   db_stats["total_documents"],
        "total_chunks":      db_stats["total_chunks"],
        "queries_processed": _query_count.get(tenant_id, 0),
        "ai_mode":           "live" if IS_LIVE else "demo",
        "storage":           "pgvector" if engine else "in-memory",
        "documents":         doc_names,
    }

@app.get("/")
async def root():
    return {"name": "RAG System", "version": "1.0.0",
            "ui": "/demo", "docs": "/docs"}

@app.get("/demo", response_class=HTMLResponse)
async def demo_ui():
    return HTMLResponse(content=_build_html())

# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------
def _build_html() -> str:
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>RAG System</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0b0f1a;color:#e2e8f0;min-height:100vh}
a{color:#63b3ed}

/* ---- header ---- */
header{background:linear-gradient(135deg,#0d1b2a,#1a2744);border-bottom:1px solid #1e3a5f;padding:16px 28px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
.hdr-l{display:flex;align-items:center;gap:12px}
header h1{font-size:1.2rem;font-weight:700;color:#90cdf4;letter-spacing:-.01em}
header h1 em{color:#63b3ed;font-style:normal;font-weight:400}
.badge{font-size:.65rem;padding:3px 9px;border-radius:99px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.bdemo{background:#2d3748;color:#f6ad55}
.blive{background:#1a4731;color:#68d391}
.mode-txt{font-size:.73rem;color:#4a6fa5}

/* ---- tenant bar ---- */
.tbar{background:#0d1b30;border-bottom:1px solid #1e3a5f;padding:10px 28px;display:flex;align-items:center;gap:12px}
.tbar label{font-size:.78rem;color:#718096;white-space:nowrap}
.tbar input{background:#0b0f1a;border:1px solid #2a4a7f;border-radius:6px;color:#e2e8f0;padding:7px 12px;font-size:.85rem;outline:none;width:260px;transition:border-color .2s}
.tbar input:focus{border-color:#3182ce}
.tbar .hint{font-size:.7rem;color:#2d4a6e}

/* ---- status bar ---- */
.sbar{background:#090d16;border-bottom:1px solid #1e3a5f;padding:6px 28px;display:flex;gap:18px;font-size:.72rem;color:#4a6fa5;flex-wrap:wrap}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:4px;background:#68d391;vertical-align:middle}
.dot.red{background:#fc8181} .dot.yellow{background:#f6ad55}

/* ---- layout ---- */
.layout{display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:18px 28px;max-width:1400px;margin:0 auto}
@media(max-width:800px){.layout{grid-template-columns:1fr}}
.full{grid-column:1/-1}

/* ---- card ---- */
.card{background:#111827;border:1px solid #1e3a5f;border-radius:12px;padding:20px}
.card h2{font-size:.72rem;font-weight:700;color:#4a6fa5;text-transform:uppercase;letter-spacing:.1em;margin-bottom:14px}
.tip{font-size:.71rem;color:#2d4a6e;margin-bottom:12px;line-height:1.55}

/* ---- form elements ---- */
label{display:block;font-size:.76rem;color:#718096;margin:11px 0 4px}
input[type=text],input[type=number],textarea,select{width:100%;background:#0b0f1a;border:1px solid #1e3a5f;border-radius:6px;color:#e2e8f0;padding:8px 12px;font-size:.84rem;outline:none;transition:border-color .2s;resize:vertical;font-family:inherit}
input:focus,textarea:focus{border-color:#3182ce}

/* ---- file drop zone ---- */
.dropzone{border:2px dashed #1e3a5f;border-radius:8px;padding:28px 16px;text-align:center;cursor:pointer;transition:all .2s;margin-top:4px;background:#0b0f1a}
.dropzone:hover,.dropzone.over{border-color:#3182ce;background:#0d1b2a}
.dropzone .dz-icon{font-size:2rem;margin-bottom:8px}
.dropzone .dz-main{font-size:.85rem;color:#a0aec0}
.dropzone .dz-sub{font-size:.72rem;color:#2d4a6e;margin-top:4px}
.dz-file{display:flex;align-items:center;gap:8px;background:#0d1b2a;border:1px solid #2a4a7f;border-radius:6px;padding:8px 12px;margin-top:10px;font-size:.8rem}
.dz-file .fname{color:#90cdf4;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dz-file .fsize{color:#4a6fa5;font-size:.7rem}
.dz-rm{background:none;border:none;color:#fc8181;cursor:pointer;font-size:1rem;padding:0;margin:0;width:auto}

/* ---- buttons ---- */
button{margin-top:12px;width:100%;padding:9px;border:none;border-radius:7px;font-size:.86rem;font-weight:600;cursor:pointer;transition:all .15s}
button:active{transform:scale(.98)} button:disabled{opacity:.4;cursor:not-allowed}
.bblue{background:#2b6cb0;color:#fff} .bblue:hover:not(:disabled){background:#2c5282}
.bgreen{background:#22543d;color:#9ae6b4;border:1px solid #276749} .bgreen:hover:not(:disabled){background:#276749}
.bred{background:#742a2a;color:#fed7d7;border:1px solid #9b2c2c} .bred:hover:not(:disabled){background:#9b2c2c}

/* ---- results ---- */
.result{margin-top:12px;background:#0b0f1a;border:1px solid #1e3a5f;border-radius:7px;padding:14px;font-size:.8rem;line-height:1.65;min-height:60px;max-height:420px;overflow-y:auto}
.result.empty{color:#2d4a6e;font-style:italic}
.tag{display:inline-block;background:#1a2744;border:1px solid #2a4a7f;border-radius:4px;padding:1px 7px;font-size:.7rem;color:#90cdf4;margin:2px}
.citation{background:#0d1b30;border-left:3px solid #3182ce;padding:7px 11px;margin:5px 0;border-radius:0 6px 6px 0}
.csrc{font-weight:700;color:#63b3ed;font-size:.78rem}
.csnip{color:#718096;font-size:.73rem;margin-top:2px;line-height:1.5}
.score{color:#68d391;font-weight:600}
.answer-box{background:#0d1b2a;border:1px solid #1e4976;border-radius:7px;padding:13px;margin:9px 0;line-height:1.75;white-space:pre-wrap;word-break:break-word;font-size:.84rem}
.meta-row{display:flex;gap:14px;flex-wrap:wrap;margin:7px 0;font-size:.72rem;color:#4a6fa5}
.meta-row b{color:#718096}
hr{border:none;border-top:1px solid #1e3a5f;margin:10px 0}

/* ---- doc list ---- */
.doclist{list-style:none;margin-top:8px}
.doclist li{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #1a2744;font-size:.78rem}
.doclist li:last-child{border-bottom:none}
.doclist .dname{flex:1;color:#90cdf4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.doclist .dpg{color:#4a6fa5;font-size:.68rem}
.doclist .drm{background:none;border:none;color:#fc8181;cursor:pointer;font-size:.8rem;padding:1px 5px;margin:0;width:auto}

/* ---- health grid ---- */
.hgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-top:12px}
.hi{background:#0b0f1a;border:1px solid #1e3a5f;border-radius:8px;padding:11px;text-align:center}
.hi .k{font-size:.62rem;color:#4a6fa5;text-transform:uppercase;letter-spacing:.06em}
.hi .v{font-size:.88rem;font-weight:700;color:#90cdf4;margin-top:4px;word-break:break-all}

.spinner{display:inline-block;width:12px;height:12px;border:2px solid #1e3a5f;border-top-color:#63b3ed;border-radius:50%;animation:spin .6s linear infinite;margin-right:6px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}

/* ---- tabs ---- */
.tab-btn{background:#0b0f1a;color:#4a6fa5;font-weight:600;transition:all .15s;border:none}
.tab-btn:hover:not(:disabled){background:#111827;color:#90cdf4}
.tab-active{background:#1a2744 !important;color:#90cdf4 !important}
</style>
</head>
<body>
<header>
  <div class="hdr-l">
    <h1>RAG <em>System</em></h1>
    <span class="badge bdemo" id="mode-badge">DEMO</span>
  </div>
  <div class="mode-txt" id="mode-info">Checking...</div>
</header>

<!-- Global tenant bar -->
<div class="tbar">
  <label for="g-tenant">Tenant ID</label>
  <input id="g-tenant" type="text" value="default" placeholder="e.g. my_tenant" oninput="syncTenant()"/>
  <span class="hint">This ID is used for all operations below</span>
</div>

<div class="sbar">
  <span><span class="dot" id="api-dot"></span><span id="api-status">Connecting...</span></span>
  <span><span class="dot yellow" id="db-dot"></span><span id="db-status">DB unknown</span></span>
  <span id="ai-sbar">AI: checking...</span>
</div>

<div class="layout">

<!-- System status -->
<div class="card full">
  <h2>System Status</h2>
  <div class="hgrid">
    <div class="hi"><div class="k">Status</div><div class="v" id="h-status">...</div></div>
    <div class="hi"><div class="k">AI Mode</div><div class="v" id="h-mode">...</div></div>
    <div class="hi"><div class="k">OpenAI Key</div><div class="v" id="h-key" style="font-size:.72rem">...</div></div>
    <div class="hi"><div class="k">Database</div><div class="v" id="h-db">...</div></div>
    <div class="hi"><div class="k">Embed Model</div><div class="v" id="h-embed">...</div></div>
    <div class="hi"><div class="k">Gen Model</div><div class="v" id="h-gen">...</div></div>
    <div class="hi"><div class="k">Version</div><div class="v" id="h-ver">...</div></div>
  </div>
</div>

<!-- Upload / Paste tabs -->
<div class="card">
  <h2>Add Document</h2>
  <div class="tip">Upload a file or paste text directly. Both are ingested the same way — searchable by the query panel.</div>

  <!-- Tab switcher -->
  <div style="display:flex;gap:0;margin-bottom:14px;border:1px solid #1e3a5f;border-radius:7px;overflow:hidden">
    <button id="tab-file" class="tab-btn tab-active" onclick="switchTab('file')" style="margin:0;border-radius:0;flex:1;padding:8px;font-size:.78rem">Upload File</button>
    <button id="tab-text" class="tab-btn" onclick="switchTab('text')" style="margin:0;border-radius:0;flex:1;padding:8px;font-size:.78rem;border-left:1px solid #1e3a5f">Paste Text</button>
  </div>

  <!-- File upload panel -->
  <div id="panel-file">
    <div class="dropzone" id="dropzone" onclick="document.getElementById('file-input').click()"
         ondragover="dzOver(event)" ondragleave="dzLeave()" ondrop="dzDrop(event)">
      <div class="dz-icon">&#128196;</div>
      <div class="dz-main">Drop file here or <u>click to browse</u></div>
      <div class="dz-sub">PDF &nbsp;&middot;&nbsp; DOCX &nbsp;&middot;&nbsp; XLSX &nbsp;&middot;&nbsp; TXT &nbsp;&middot;&nbsp; JSON</div>
    </div>
    <input id="file-input" type="file" accept=".pdf,.docx,.xlsx,.xls,.txt,.md,.csv,.json" style="display:none" onchange="fileChosen(this.files[0])"/>
    <div id="dz-file-info" style="display:none"></div>
    <label>Document ID <span style="color:#4a6fa5;font-size:.68rem">(auto-filled from filename)</span></label>
    <input id="up-docid" placeholder="auto"/>
    <label>Source Page</label>
    <input id="up-page" type="number" value="1"/>
    <button class="bgreen" id="btn-upload" onclick="uploadFile()" disabled>Upload &amp; Ingest</button>
  </div>

  <!-- Paste text panel -->
  <div id="panel-text" style="display:none">
    <label>Document ID <span style="color:#4a6fa5;font-size:.68rem">(auto-generated — edit if needed)</span></label>
    <input id="txt-docid" placeholder="auto-generated on first keystroke"
           oninput="_autoIdLocked=this.value.length>0"
           onblur="if(!this.value.trim())_autoIdLocked=false"/>
    <label>Paste your text or JSON content here</label>
    <textarea id="txt-content" rows="8" placeholder="Paste any text, JSON, notes, or document content here..." oninput="autoDocId()"></textarea>
    <label>Source Page</label>
    <input id="txt-page" type="number" value="1"/>
    <button class="bgreen" id="btn-txt-ingest" onclick="ingestText()">Ingest Text</button>
  </div>

  <div class="result empty" id="up-result">Result will appear here...</div>
</div>

<!-- Query -->
<div class="card">
  <h2>Ask a Question</h2>
  <div class="tip">Ask anything about your uploaded documents. Results are grounded strictly to ingested content.</div>
  <label>Question</label>
  <textarea id="q-query" rows="4" placeholder="Ask anything about your uploaded documents..."></textarea>
  <label>Top-K Results</label>
  <input id="q-topk" type="number" value="5" min="1" max="20"/>
  <button class="bblue" id="btn-query" onclick="runQuery()">Run Query</button>
  <div class="result empty" id="q-result">Results will appear here...</div>
</div>

<!-- Ingested docs + stats -->
<div class="card full">
  <h2>Documents &amp; Stats</h2>
  <button class="bblue" id="btn-stats" onclick="getStats()">Refresh Stats &amp; Document List</button>
  <div class="result empty" id="stats-result">Stats will appear here...</div>
</div>

</div><!-- /layout -->

<script>
const SAMPLES=[
  {id:"liability_clause_v1",page:3,content:"The liability clause in this agreement limits all damages to direct losses only. Consequential, indirect, or punitive damages are explicitly excluded regardless of the theory of liability, including negligence or strict liability."},
  {id:"patent_license_v2",page:42,content:"The licensor grants an exclusive, non-transferable patent license within the defined territory for the duration of the patent. Sub-licensing to third parties requires prior written consent from the licensor."},
  {id:"contract_interpretation_2023",page:15,content:"Courts have consistently applied the plain-meaning rule as the primary method of contract interpretation. Extrinsic or parol evidence is only admissible when contractual language is genuinely ambiguous on its face."},
  {id:"indemnification_clause_v3",page:7,content:"The indemnifying party agrees to defend, indemnify, and hold harmless the indemnitee from and against all third-party claims, damages, losses, and expenses arising out of the indemnifying party breach of representations."},
  {id:"force_majeure_2024",page:22,content:"Neither party shall be held liable for failure to perform obligations caused by force majeure events including natural disasters, government actions, pandemics, or other events beyond reasonable control that were unforeseeable at contracting."},
  {id:"confidentiality_nda_v1",page:5,content:"Each party agrees to hold in strict confidence and not disclose any Confidential Information received from the other party. This obligation survives termination of the agreement for a period of five years."},
];

let _chosenFile=null;

/* ---- tenant sync ---- */
function getTenant(){return document.getElementById('g-tenant').value.trim()||'default';}
function syncTenant(){} // tenant read live from g-tenant everywhere

/* ---- spinner ---- */
function spin(id){const b=document.getElementById(id);b._t=b.innerHTML;b.innerHTML='<span class="spinner"></span>Working...';b.disabled=true;}
function unspin(id){const b=document.getElementById(id);b.innerHTML=b._t;b.disabled=false;}
function setResult(id,html,empty){const el=document.getElementById(id);el.innerHTML=html;el.className='result'+(empty?' empty':'');}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

/* ---- fetch wrapper ---- */
async function api(path,method,body){
  const o={method,headers:{'Content-Type':'application/json'}};
  if(body) o.body=JSON.stringify(body);
  const r=await fetch(path,o);const d=await r.json();
  if(!r.ok) throw new Error(d.detail||JSON.stringify(d));
  return d;
}

/* ---- health ---- */
async function refreshHealth(){
  try{
    const d=await api('/health','GET');
    document.getElementById('h-status').textContent=d.status;
    document.getElementById('h-mode').textContent=d.ai_mode;
    document.getElementById('h-key').textContent=d.openai_key||'not set';
    document.getElementById('h-key').style.color=d.ai_mode==='live'?'#68d391':'#fc8181';
    document.getElementById('h-db').textContent=d.database;
    document.getElementById('h-embed').textContent=d.embed_model||'none';
    document.getElementById('h-gen').textContent=d.gen_model||'none';
    document.getElementById('h-ver').textContent=d.version;
    document.getElementById('api-dot').className='dot';
    document.getElementById('api-status').textContent='API online';
    const dbOk=d.database.startsWith('connected');
    document.getElementById('db-dot').className='dot'+(dbOk?'':' yellow');
    document.getElementById('db-status').textContent='DB: '+d.database;
    document.getElementById('ai-sbar').textContent='AI: '+(d.ai_mode==='live'?'Live ('+d.gen_model+')':'Demo (no key)');
    const live=d.ai_mode==='live';
    const b=document.getElementById('mode-badge');
    b.textContent=live?'LIVE AI':'DEMO';b.className='badge '+(live?'blive':'bdemo');
    document.getElementById('mode-info').textContent=live?'Real embeddings + GPT-4o active':'No OpenAI key — keyword search + canned answers';
  }catch(e){
    document.getElementById('api-dot').className='dot red';
    document.getElementById('api-status').textContent='API offline';
  }
}

/* ---- drag-drop ---- */
function dzOver(e){e.preventDefault();document.getElementById('dropzone').classList.add('over');}
function dzLeave(){document.getElementById('dropzone').classList.remove('over');}
function dzDrop(e){e.preventDefault();dzLeave();if(e.dataTransfer.files[0]) fileChosen(e.dataTransfer.files[0]);}
function fmtSize(b){return b>1048576?(b/1048576).toFixed(1)+' MB':(b/1024).toFixed(0)+' KB';}

function fileChosen(f){
  if(!f) return;
  _chosenFile=f;
  const ext=f.name.split('.').pop().toLowerCase();
  const allowed=['pdf','docx','xlsx','xls','txt','md','csv','json'];
  if(!allowed.includes(ext)){alert('Unsupported file type: .'+ext);_chosenFile=null;return;}
  const docId=f.name.replace(/\.[^.]+$/,'').replace(/[^a-zA-Z0-9_-]/g,'_');
  document.getElementById('up-docid').value=docId;
  document.getElementById('dz-file-info').style.display='block';
  document.getElementById('dz-file-info').innerHTML=
    `<div class="dz-file"><span class="fname">${esc(f.name)}</span>
     <span class="fsize">${fmtSize(f.size)}</span>
     <button class="dz-rm" onclick="clearFile()" title="Remove">&#x2715;</button></div>`;
  document.getElementById('btn-upload').disabled=false;
}
function clearFile(){
  _chosenFile=null;
  document.getElementById('dz-file-info').style.display='none';
  document.getElementById('dz-file-info').innerHTML='';
  document.getElementById('file-input').value='';
  document.getElementById('up-docid').value='';
  document.getElementById('btn-upload').disabled=true;
}

/* ---- tab switching ---- */
function switchTab(tab){
  document.getElementById('panel-file').style.display = tab==='file' ? '' : 'none';
  document.getElementById('panel-text').style.display = tab==='text' ? '' : 'none';
  document.getElementById('tab-file').className = 'tab-btn' + (tab==='file' ? ' tab-active' : '');
  document.getElementById('tab-text').className = 'tab-btn' + (tab==='text' ? ' tab-active' : '');
  setResult('up-result','',true);
}

/* ---- auto doc ID from pasted text ---- */
let _autoIdLocked = false;
function autoDocId(){
  if(_autoIdLocked) return;           // user has manually edited the field — don't overwrite
  const txt = document.getElementById('txt-content').value.trim();
  if(!txt) return;
  // Take first 5 meaningful words, slugify, append short timestamp
  const slug = txt.replace(/[\r\n]+/g,' ')
                   .split(/\s+/)
                   .filter(w=>w.length>1)
                   .slice(0,5)
                   .join('_')
                   .replace(/[^a-zA-Z0-9_]/g,'')
                   .toLowerCase()
                   .substring(0,40);
  const ts = Date.now().toString(36);   // e.g. "lq3k7f" — short & unique
  document.getElementById('txt-docid').value = (slug||'doc') + '_' + ts;
}

/* ---- ingest pasted text ---- */
async function ingestText(){
  const content = document.getElementById('txt-content').value.trim();
  if(!content){alert('Paste some text first');return;}
  const rawId = document.getElementById('txt-docid').value.trim();
  if(!rawId){alert('Please enter a Document ID');return;}
  const docId = rawId.replace(/[^a-zA-Z0-9_-]/g,'_');
  const page  = parseInt(document.getElementById('txt-page').value)||1;
  spin('btn-txt-ingest');
  try{
    const d = await api('/ingest','POST',{
      tenant_id: getTenant(),
      document_id: docId,
      content: content,
      source_page: page,
    });
    setResult('up-result',
      `<span class="score">Ingested successfully</span><hr>
       Doc ID: <span class="tag">${esc(d.document_id)}</span>
       Chunks: <span class="tag">${d.chunks_created}</span>
       Storage: <span class="tag">${esc(d.storage)}</span>
       Chars: <span class="tag">${content.length.toLocaleString()}</span>
       Time: <span class="tag">${d.elapsed_s}s</span>`,false);
    document.getElementById('txt-content').value='';
    document.getElementById('txt-docid').value='';
    _autoIdLocked = false;
  }catch(e){
    setResult('up-result','<span style="color:#fc8181">Error: '+esc(e.message)+'</span>',false);
  }
  unspin('btn-txt-ingest');
}


async function uploadFile(){
  if(!_chosenFile){alert('Choose a file first');return;}
  spin('btn-upload');
  const fd=new FormData();
  fd.append('file',_chosenFile);
  fd.append('tenant_id',getTenant());
  fd.append('document_id',document.getElementById('up-docid').value.trim());
  fd.append('source_page',document.getElementById('up-page').value||'1');
  try{
    const r=await fetch('/upload',{method:'POST',body:fd});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail||JSON.stringify(d));
    setResult('up-result',
      `<span class="score">Ingested successfully</span><hr>
       File: <span class="tag">${esc(d.filename)}</span>
       Doc ID: <span class="tag">${esc(d.document_id)}</span>
       Chunks: <span class="tag">${d.chunks_created}</span>
       Storage: <span class="tag">${esc(d.storage)}</span>
       Chars: <span class="tag">${d.chars.toLocaleString()}</span>
       Time: <span class="tag">${d.elapsed_s}s</span><hr>
       <b style="font-size:.72rem;color:#4a6fa5">PREVIEW</b><br>
       <div class="answer-box" style="max-height:120px;overflow-y:auto;font-size:.75rem">${esc(d.preview)}...</div>`,false);
    clearFile();
  }catch(e){
    setResult('up-result','<span style="color:#fc8181">Error: '+esc(e.message)+'</span>',false);
  }
  unspin('btn-upload');
}

/* ---- query ---- */
async function runQuery(){
  spin('btn-query');
  try{
    const d=await api('/query','POST',{
      tenant_id:getTenant(),
      query:document.getElementById('q-query').value.trim(),
      top_k:parseInt(document.getElementById('q-topk').value)||5,
    });
    const mTag=d.mode==='live'?
      '<span class="tag" style="color:#68d391">Live AI</span>':
      '<span class="tag" style="color:#f6ad55">Demo</span>';
    const fmtTag=d.response_format==='json'?
      '<span class="tag" style="color:#90cdf4">JSON</span>':
      '<span class="tag" style="color:#a0aec0">Text</span>';

    // Render answer — pretty JSON viewer or plain text
    let answerHtml;
    if(d.response_format==='json'){
      let pretty=d.answer;
      try{pretty=JSON.stringify(JSON.parse(d.answer),null,2);}catch(e){}
      answerHtml=`<pre style="background:#0a1628;border:1px solid #1e4976;border-radius:6px;padding:12px;
        overflow-x:auto;font-size:.78rem;line-height:1.6;color:#90cdf4;white-space:pre-wrap">${esc(pretty)}</pre>`;
    } else {
      answerHtml=`<div class="answer-box">${esc(d.answer)}</div>`;
    }

    const cits=d.citations.length
      ?d.citations.map(c=>`<div class="citation">
          <div class="csrc">&#128196; ${esc(c.source)} &middot; Page ${c.page}
            &middot; <span class="score">${(c.confidence*100).toFixed(0)}% match</span></div>
          <div class="csnip">${esc(c.snippet)}</div></div>`).join('')
      :"<em style='color:#2d4a6e'>No citations — upload a document first</em>";

    setResult('q-result',
      `<div class="meta-row">${mTag}${fmtTag}<b>Confidence:</b><span class="score">${(d.confidence*100).toFixed(1)}%</span>
       <b>Time:</b>${d.processing_time}s <b>Tokens:</b>${d.tokens_used}</div>
       ${answerHtml}
       <b style="font-size:.72rem;color:#4a6fa5">CITATIONS (${d.citations.length})</b><br>${cits}`,false);
  }catch(e){
    setResult('q-result','<span style="color:#fc8181">Error: '+esc(e.message)+'</span>',false);
  }
  unspin('btn-query');
}

/* ---- sample docs ---- */
async function loadSamples(){
  spin('btn-bulk');
  const tenant=getTenant();
  const rows=[];
  for(const s of SAMPLES){
    try{
      await api('/ingest','POST',{tenant_id:tenant,document_id:s.id,content:s.content,source_page:s.page});
      rows.push(`<span class="score">+</span> <span class="tag">${esc(s.id)}</span>`);
    }catch(e){
      rows.push(`<span style="color:#fc8181">x ${esc(s.id)}: ${esc(e.message)}</span>`);
    }
  }
  setResult('bulk-result',
    `Loaded for tenant <span class="tag">${esc(tenant)}</span>:<br><br>`+rows.join('<br>'),false);
  unspin('btn-bulk');
}

/* ---- stats ---- */
async function getStats(){
  spin('btn-stats');
  const tenant=getTenant();
  try{
    const d=await api(`/stats/${encodeURIComponent(tenant)}`,'GET');
    const docs=_localDocs[tenant]||[];
    const docRows=docs.length
      ?`<ul class="doclist">${docs.map(n=>`<li>
          <span class="dname">&#128196; ${esc(n)}</span>
          <button class="drm" onclick="removeDoc('${esc(n)}')">remove</button>
         </li>`).join('')}</ul>`
      :'<div style="color:#2d4a6e;font-style:italic;font-size:.76rem">No documents tracked locally</div>';
    setResult('stats-result',
      Object.entries(d).map(([k,v])=>`<b>${esc(k)}:</b> ${esc(String(v))}`).join('<br>')
      +'<hr><b style="font-size:.72rem;color:#4a6fa5">INGESTED DOCS</b>'+docRows,false);
  }catch(e){
    setResult('stats-result','<span style="color:#fc8181">Error: '+esc(e.message)+'</span>',false);
  }
  unspin('btn-stats');
}

// track doc names locally for display
const _localDocs={};
const _origIngest=window.loadSamples;
// patch upload result to track doc names — done inline above

window.addEventListener('load',()=>{refreshHealth();setInterval(refreshHealth,15000);});
</script>
</body></html>
"""
