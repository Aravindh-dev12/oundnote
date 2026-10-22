# Hosted Oundnote (private beta)

Oundnote is designed first for a single-user local workspace. This deployment recipe runs
the existing FastAPI UI and API in one persistent service, protected by a workspace password.
It is **not yet a multi-user SaaS**: do not use it for unrelated customers or invite other users
until per-user authentication, tenant-scoped database queries, and storage isolation are implemented.

## What this deployment supports

- Browser-based workspace, meeting library, transcript editor, notes, settings, and API.
- Audio file upload and processing with the installed CPU-capable transcription backend.
- Local background jobs, SQLite, meeting files, model downloads, and RAG index on persistent disk.
- Existing diarization, summarization, and retrieval capabilities when the required model assets are installed.
- HTTPS termination at the hosting provider.

The hosted service cannot access the microphone or system-audio devices on your laptop. Browser-native recording
must be implemented/enabled separately if the browser UI does not expose it; otherwise upload a recording.
Local desktop capture, global hotkeys, tray integration, and system loopback capture remain desktop-only.
CPU model inference can be slower than a local GPU. Remote LLM providers require configuring their supported
credentials in Oundnote settings; do not commit API keys.

## Deploy on Render

1. Push or merge the `cloud-hosting` branch, then create a Render Blueprint from this repository with
   `render.yaml`. Review the plan and persistent disk cost before creating resources.
2. Set `M2N_WEB_PASSWORD` in the service's Environment settings to a long, unique password. The blueprint
   generates `M2N_WEB_SESSION_SECRET`; never publish either value.
3. Confirm the host name configured by `M2N_ALLOWED_HOSTS` matches the actual Render service domain.
4. Deploy and wait for `GET /healthz` to return `{"status":"ok"}`.
5. Sign in at the service URL. Open Settings and install the desired transcription/diarization/summarization
   model profiles. Ensure sufficient free disk space before downloading models.
6. Test with a short non-sensitive audio file, confirm transcript and notes generation, and restart the service
   to confirm recordings and transcripts persist.

## Why not deploy the whole backend to Vercel Functions?

This application starts in-process background job queues and model runtimes and persists SQLite databases,
audio, and model files on disk. Vercel Functions have ephemeral writable storage and a request/response execution
model, so deploying the unmodified backend as a Vercel Function would risk lost data and interrupted long jobs.
Use Vercel for a static marketing site or separate browser client, and route authenticated API calls to the persistent
backend only after a dedicated frontend/API auth design is in place.

## Security and operational limits

- The password gate is for a private, single-workspace beta, not a multi-user identity system.
- Use a unique random session secret (the Render Blueprint generates it) and a long password.
- Configure host names explicitly. The default middleware allowlist stays strict for local deployments.
- Recording consent, retention policies, backups, and account recovery remain operator responsibilities.
- The 40 GB persistent disk is a starting point, not a guarantee it fits all model/audio workloads; review usage.
- Don't expose MCP over the public internet. Keep desktop MCP in the local desktop environment.
