# -*- coding: utf-8 -*-
"""
Langfuse tracing wrapper.

If LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are set, every RAG operation
is traced with full token counts, chunk metadata, latency, and scores.
Falls back to a no-op tracer so the app works without Langfuse configured.
"""
import os
import time
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_LF_PUBLIC  = os.getenv("LANGFUSE_PUBLIC_KEY", "")
_LF_SECRET  = os.getenv("LANGFUSE_SECRET_KEY", "")
_LF_HOST    = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
_LF_ENABLED = bool(
    _LF_PUBLIC
    and _LF_SECRET
    and not _LF_PUBLIC.startswith("pk-lf-your")
    and not _LF_SECRET.startswith("sk-lf-your")
)

# ---------------------------------------------------------------------------
# Real Langfuse client (lazy-initialised)
# ---------------------------------------------------------------------------
_lf_client = None

def _get_client():
    global _lf_client
    if _lf_client is not None:
        return _lf_client
    if not _LF_ENABLED:
        return None
    try:
        from langfuse import Langfuse
        _lf_client = Langfuse(
            public_key=_LF_PUBLIC,
            secret_key=_LF_SECRET,
            host=_LF_HOST,
        )
        logger.info(f"Langfuse connected → {_LF_HOST}")
        return _lf_client
    except Exception as e:
        logger.warning(f"Langfuse init failed: {e} — tracing disabled")
        return None

# ---------------------------------------------------------------------------
# In-process metrics accumulator (always active, no external dependency)
# ---------------------------------------------------------------------------
_metrics = {
    "embed_calls":     0,
    "embed_tokens":    0,
    "generate_calls":  0,
    "generate_tokens": 0,
    "rerank_calls":    0,
    "rerank_tokens":   0,
    "total_latency_ms": 0.0,
    "query_count":     0,
    "ingest_count":    0,
}

# Approximate cost per 1M tokens (USD) — update as pricing changes
_COST_PER_1M = {
    "text-embedding-3-large": 0.13,
    "text-embedding-3-small": 0.02,
    "gpt-4o":                 5.00,   # input
    "gpt-4o-mini":            0.15,
}

def get_metrics() -> dict:
    """Return current accumulated metrics with cost estimate."""
    embed_cost  = (_metrics["embed_tokens"]    / 1_000_000) * _COST_PER_1M.get(
                    os.getenv("OPENAI_EMBEDDING_MODEL","text-embedding-3-large"), 0.13)
    gen_cost    = (_metrics["generate_tokens"] / 1_000_000) * _COST_PER_1M.get(
                    os.getenv("OPENAI_GENERATION_MODEL","gpt-4o"), 5.00)
    rerank_cost = (_metrics["rerank_tokens"]   / 1_000_000) * _COST_PER_1M.get(
                    os.getenv("OPENAI_RERANK_MODEL","gpt-4o-mini"), 0.15)
    total_tokens = (_metrics["embed_tokens"]
                  + _metrics["generate_tokens"]
                  + _metrics["rerank_tokens"])
    return {
        **_metrics,
        "total_tokens":      total_tokens,
        "estimated_cost_usd": round(embed_cost + gen_cost + rerank_cost, 6),
        "langfuse_enabled":  _LF_ENABLED,
        "langfuse_host":     _LF_HOST if _LF_ENABLED else "not configured",
    }

def reset_metrics():
    for k in _metrics:
        _metrics[k] = 0 if isinstance(_metrics[k], int) else 0.0

# ---------------------------------------------------------------------------
# Trace context manager
# ---------------------------------------------------------------------------
class RAGTrace:
    """
    Wraps a single user query as a Langfuse trace with child spans for
    each RAG step (embed, retrieve, rerank, generate).
    Falls back silently if Langfuse is unavailable.
    """
    def __init__(self, query: str, tenant_id: str, trace_id: str | None = None):
        self.query      = query
        self.tenant_id  = tenant_id
        self.trace_id   = trace_id or str(uuid.uuid4())
        self._trace     = None
        self._start     = time.time()
        self._spans: list = []

        client = _get_client()
        if client:
            try:
                self._trace = client.trace(
                    id=self.trace_id,
                    name="rag-query",
                    input={"query": query, "tenant_id": tenant_id},
                    metadata={"tenant_id": tenant_id},
                )
            except Exception as e:
                logger.debug(f"Trace create failed: {e}")

    # ---- Embedding span ------------------------------------------------
    def record_embed(self, text_len: int, tokens: int, latency_ms: float,
                     model: str, purpose: str = "query"):
        _metrics["embed_calls"]  += 1
        _metrics["embed_tokens"] += tokens
        _metrics["total_latency_ms"] += latency_ms
        logger.debug(f"[trace] embed purpose={purpose} tokens={tokens} lat={latency_ms:.0f}ms")
        if self._trace:
            try:
                self._trace.span(
                    name=f"embed-{purpose}",
                    input={"text_length": text_len, "model": model},
                    output={"tokens": tokens},
                    metadata={"latency_ms": latency_ms, "model": model},
                )
            except Exception:
                pass

    # ---- Retrieval span ------------------------------------------------
    def record_retrieval(self, num_chunks: int, latency_ms: float,
                         method: str = "vector", top_k: int = 5):
        _metrics["total_latency_ms"] += latency_ms
        logger.debug(f"[trace] retrieve method={method} chunks={num_chunks} lat={latency_ms:.0f}ms")
        if self._trace:
            try:
                self._trace.span(
                    name="retrieve",
                    input={"top_k": top_k, "method": method},
                    output={"chunks_returned": num_chunks},
                    metadata={"latency_ms": latency_ms},
                )
            except Exception:
                pass

    # ---- Rerank span ---------------------------------------------------
    def record_rerank(self, input_chunks: int, output_chunks: int,
                      tokens: int, latency_ms: float, model: str):
        _metrics["rerank_calls"]  += 1
        _metrics["rerank_tokens"] += tokens
        _metrics["total_latency_ms"] += latency_ms
        logger.debug(f"[trace] rerank in={input_chunks} out={output_chunks} tokens={tokens}")
        if self._trace:
            try:
                self._trace.span(
                    name="rerank",
                    input={"input_chunks": input_chunks, "model": model},
                    output={"output_chunks": output_chunks, "tokens": tokens},
                    metadata={"latency_ms": latency_ms, "model": model},
                )
            except Exception:
                pass

    # ---- Generation span -----------------------------------------------
    def record_generation(self, prompt_tokens: int, completion_tokens: int,
                          total_tokens: int, latency_ms: float,
                          model: str, response_format: str = "text"):
        _metrics["generate_calls"]  += 1
        _metrics["generate_tokens"] += total_tokens
        _metrics["total_latency_ms"] += latency_ms
        _metrics["query_count"] += 1
        logger.debug(f"[trace] generate tokens={total_tokens} lat={latency_ms:.0f}ms")
        if self._trace:
            try:
                self._trace.generation(
                    name="llm-generate",
                    model=model,
                    input=self.query,
                    usage={
                        "input":  prompt_tokens,
                        "output": completion_tokens,
                        "total":  total_tokens,
                        "unit":   "TOKENS",
                    },
                    metadata={
                        "latency_ms":      latency_ms,
                        "response_format": response_format,
                    },
                )
            except Exception:
                pass

    # ---- Ingest event --------------------------------------------------
    @staticmethod
    def record_ingest(tenant_id: str, document_id: str, chunks: int,
                      chars: int, embed_tokens: int, latency_ms: float):
        _metrics["ingest_count"]  += 1
        _metrics["embed_tokens"]  += embed_tokens
        _metrics["embed_calls"]   += 1
        _metrics["total_latency_ms"] += latency_ms
        logger.debug(f"[trace] ingest doc={document_id} chunks={chunks} embed_tokens={embed_tokens}")
        client = _get_client()
        if client:
            try:
                client.event(
                    name="document-ingested",
                    input={"document_id": document_id, "tenant_id": tenant_id,
                           "chars": chars, "chunks": chunks},
                    metadata={"embed_tokens": embed_tokens, "latency_ms": latency_ms},
                )
            except Exception:
                pass

    # ---- Finalise trace ------------------------------------------------
    def finish(self, answer: str, confidence: float, num_citations: int):
        elapsed_ms = (time.time() - self._start) * 1000
        if self._trace:
            try:
                self._trace.update(
                    output={
                        "answer_preview": answer[:200],
                        "confidence":     confidence,
                        "citations":      num_citations,
                    },
                    metadata={"total_latency_ms": elapsed_ms},
                )
                # Flush to Langfuse
                client = _get_client()
                if client:
                    client.flush()
            except Exception as e:
                logger.debug(f"Trace finish failed: {e}")
