# Vercel preview hosting

This entrypoint lets Vercel's Python/FastAPI runtime serve Oundnote's UI and API. It is a **private preview / compatibility test**, not yet a durable multi-user service. The ASR, diarization, and summary model adapters are not currently deployable in the same Vercel function because of the function bundle-size limit.

## Current preview status

The Vercel preview is intentionally a **UI/API compatibility preview**, not yet a complete meeting-transcription deployment. The deployed checks have returned 200 for `/healthz`, `/`, `/capture`, the studio JavaScript/CSS, and basic meeting/jobs APIs. The deployed `/api/capabilities` endpoint currently reports transcription, diarization, and summaries as `requires_install`.

The base FastAPI dependency set is kept small so Vercel can bundle it under the function size limit. Adding native Faster Whisper and speaker-diarization runtimes to the same function currently exceeds Vercel's 500 MB total function limit. The optional `transcription`, `diarization`, and `summaries` extras are therefore not installed in this preview. Do not use this deployment to process real recordings or rely on meeting data persisting across requests.

## Vercel configuration

- Root entrypoint: `app.py`, exporting the canonical FastAPI app.
- Functions run in `bom1` by default for the initial India-oriented preview.
- Local files are redirected to `/tmp/oundnote`; model downloads, SQLite, meeting audio, and in-process sessions are temporary and instance-local.
- Vercel Function request bodies and runtimes have platform limits; large direct file uploads and long local model workloads may not fit this deployment model.
- The new browser streaming endpoint keeps capture session state in the app process. Vercel does not guarantee that follow-up requests reach the same function instance. Consequently, live recording is not production reliable until session state is externalized.

## Protecting the preview

Create this as a Vercel preview deployment with deployment protection enabled. Configure `M2N_ALLOWED_HOSTS` to include `*.vercel.app`, `localhost`, and `127.0.0.1`. Never publish a deployment with recording APIs open to the public.

## What is required for production on Vercel

1. Move meeting metadata, jobs, users, and transcript segments from SQLite/in-memory job state to a shared durable database and queue.
2. Store audio assets and model-independent artifacts in durable object storage; do not persist them in `/tmp`.
3. Externalize browser capture session coordination so any serverless instance can accept the next chunk.
4. Use managed inference or a separate persistent worker for CPU/GPU transcription, diarization, and local LLM model processing. Model binaries should not be downloaded on every cold start.
5. Add per-user authentication, tenant ownership checks, retention controls, and billing before inviting other users.

Vercel supports FastAPI functions, but function storage is temporary. This branch includes a persistent-backend Render Blueprint as an alternative when durability is more important than deploying the whole service as request-driven functions.
