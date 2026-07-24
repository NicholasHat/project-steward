# Project Truth Engine

Ingests a messy, unstructured folder of project artifacts (PDFs, spreadsheets, notes,
slide decks, images) and reconstructs a trustworthy, self-updating picture of the project:
**what happened, in what order, what's missing, and what the project's _current_ direction
actually is** — as opposed to what its earliest artifacts imply.

Three product surfaces:

1. **Browse all artifacts** — a clean, organized index over every file, without touching
   the raw files.
2. **Timeline with gaps surfaced** — a reconstructed chronology with explicitly flagged
   missing phases or expected-but-absent artifacts.
3. **Artifacts contextualized against the current goal** — each artifact labeled
   _current_, _superseded_ (vision drift), or _unclear_, with an inspectable rationale.

> Status: **scaffold**. This repo currently contains the architecture, data schema, and a
> runnable API/DB/deploy skeleton. The processing pipeline is built out on top of this.
> See [`PROJECTSPECS.md`](PROJECTSPECS.md) for the product spec and `CLAUDE.md` for the
> engineering conventions.

## Design invariants

- **Deterministic parsing/extraction**; a general-purpose LLM never reads raw files. LLM
  reasoning is reserved for judgment calls (direction, drift, gaps, renaming, report).
- **Auditable**: every inferred fact traces to the source artifact(s) and signal(s).
- **Reversible**: no automated action is permanent; a human confirmation checkpoint gates
  anything that acts on drift labels; originals are never mutated.
- **Private by default**: local embedding + local LLM; no ingested content leaves the host.
- **Multi-user**: every project is owner-scoped; users only see their own data.

## Tech stack

Python 3.12 · FastAPI · PostgreSQL 16 + pgvector · SQLAlchemy 2.0 + Alembic ·
local `sentence-transformers` embeddings · local LLM via Ollama (adapter allows a hosted
model) · React + Vite dashboard (added later) · Docker Compose + Caddy for deployment.

## Local development

```bash
uv sync --extra dev                 # create venv + install core + dev deps
docker compose up -d db             # Postgres + pgvector only
cp .env.example .env                # then set TRUTH_DATABASE_URL host to localhost
uv run alembic upgrade head         # apply schema
uv run python -m truth_engine.db.seed   # seed phase templates
uv run uvicorn truth_engine.api.app:app --reload   # http://localhost:8000/health
```

Add the parsing/ML stack when working on the pipeline: `uv sync --extra pipeline`.

To change the schema: edit `src/truth_engine/db/models.py`, then
`uv run alembic revision --autogenerate -m "message"` and review the generated migration.

## Deployment (self-hosted)

"Self-hosted" means the whole system runs on a box **you** control — nothing is sent to a
third-party inference API in the default config.

1. **Get a server** — a cloud VM (Hetzner / DigitalOcean / EC2; ~$5–40/mo CPU, more for
   GPU) or a machine you own, with root and Docker installed.
2. **Configure** — `cp .env.example .env`, set `TRUTH_SECRET_KEY` to a random value and
   `SITE_ADDRESS` to your domain (e.g. `truthengine.example.com`). Point the domain's DNS
   at the server.
3. **Launch** — `docker compose up -d`. This brings up FastAPI + Postgres/pgvector +
   Ollama + Caddy. Caddy provisions HTTPS automatically via Let's Encrypt.
4. **Pull a model** — `docker compose exec ollama ollama pull llama3.1:8b`.
5. **Done** — the app is reachable at your domain; other people can register and log in.
   All ingested data lives only in the `pgdata`/`artifacts` volumes on your box.

### Compute caveat

The local LLM needs real compute. A small model (8B) runs on CPU (slower) or a modest GPU
VM (faster, pricier — uncomment the GPU block in `docker-compose.yml`). If you'd rather not
pay for a GPU, set `TRUTH_LLM_PROVIDER=anthropic` + `TRUTH_ANTHROPIC_API_KEY` to route the
few reasoning calls to a hosted model — this trades some privacy for cost/quality. Make it
a conscious choice.
