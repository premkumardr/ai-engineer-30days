#!/usr/bin/env python3
"""
Local dev runner — no Docker, no OpenAI key, no Postgres required.

Usage:
    pip install fastapi uvicorn sqlalchemy asyncpg
    python run_local.py

Then open:  http://localhost:8000/demo
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "src.api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
