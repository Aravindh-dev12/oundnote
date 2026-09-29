# Oundnote for Anna

App 348 (`oundnote`). Anna integration v0.1.3 — tested with Oundnote v0.6.1.
Bundled Executa: 0.1.2. Desktop app, Anna UI and Executa versions are independent.
Windows x86_64 only. Submitted for Marketplace review on September 28, 2026;
Anna confirmed pending_review with candidate v0.1.3. Public release is pending.

## Requirements

- Oundnote installed and running, with MCP access enabled.
- Anna Local Agent on the same Windows computer, selected as the default agent.
- An existing Oundnote RAG index for semantic search.

The adapter provides read-only access to the existing Oundnote HTTP gateway.
It does not record audio, edit meetings, open the database, or rebuild indexes.
Retrieved text passes through Anna and may reach its AI provider in chat.
Original recordings and the database stay in Oundnote.

## Local development

From this directory, with Node 22+ and uv installed:

```powershell
npx.cmd --yes @anna-ai/cli@0.1.56 validate
npx.cmd --yes @anna-ai/cli@0.1.56 dev --no-llm --slug oundnote
```

Open http://localhost:5180/. The harness installs its own lightweight runtime.
Source development uses shared gateway modules from this repository; the released
Windows archive includes those modules and Python.

The CLI generates bundle/anna-tool-ids.js during apps push. It maps the
oundnote-library handle to tool-esteban-oundnote-library-tmze3cnf.
A fresh checkout needs that mapping before testing an already-published identity.
Do not commit credentials or the .anna identity cache.

## Build and stage a new version

```powershell
uv run --no-project --python 3.12 --with pyinstaller==6.20.0 --with httpx --with pydantic --with pydantic-settings --with platformdirs build_windows.py
npx.cmd --yes @anna-ai/cli@0.1.56 apps push
npx.cmd --yes @anna-ai/cli@0.1.56 apps cut <new-app-version>
```

Build on Windows x86_64. The script smoke-tests the binary outside the checkout
and produces a ZIP plus SHA256 in executas/oundnote/dist. Do not overwrite a
frozen version with different bytes. When changing the Executa, update its version
in executa.json, manifest.json, pyproject.toml, oundnote_plugin.py and build_windows.py.
UI-only releases can reuse the existing Executa archive.

The correct UI tool grant is required:* with the tools.invoke permission.
The Executa describe response must use Anna's list of parameter descriptors,
not a top-level JSON Schema object. A regression test checks this contract.

## Acceptance and review

See ACCEPTANCE.md for completed checks and REVIEW.md for reviewer instructions.
Run check_installed.py and check_failure_modes.py with --exe pointing to the
installed executable. The first reads the real library and may load the embedding
model. The second uses an isolated loopback server and synthetic data.

If an app update leaves the old Executa installed, inspect Agents > Details and
update the tool. App and Executa version numbers are independent.

Custom data locations or ports may require M2N_DATA_DIR or M2N_MCP_BASE_URL in
the Local Agent environment. See ../../docs/mcp.md. A Cloud Agent cannot reach
a Oundnote instance on the user's computer through loopback.

## Official references

- https://anna.partners/developers/tools/executa-protocol.md
- https://anna.partners/developers/reference/host-api-tools.md
- https://anna.partners/developers/tools/executa-binary.md
- https://anna.partners/developers/apps/app-publish.md
