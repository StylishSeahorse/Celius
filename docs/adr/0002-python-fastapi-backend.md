# ADR 0002: Python + FastAPI for the backend

Status: accepted · 2026-10-19

## Decision
Write the API and workers in Python 3.12 with FastAPI, Pydantic v2, SQLAlchemy 2 (async)
and Alembic, managed with `uv`. Rust is used only in the Tauri desktop shell.

## Rationale
Python has the strongest ecosystem for document parsing, embeddings and AI
orchestration. Throughput here is limited by the GPU, not by the API. If a hot path
needs rewriting later, it can move to another service without changing the public API.

## Consequences
- Unit tests run on SQLite for speed, so models use portable column types (`Uuid`,
  JSON with a JSONB variant). A migration round-trip test runs against real
  PostgreSQL in CI to catch dialect-specific problems.
