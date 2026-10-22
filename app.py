from __future__ import annotations

import os
import sys
from pathlib import Path

# Vercel executes the repository root as the application entrypoint. Oundnote's
# importable package lives under src/, so make that package available explicitly.
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from local_meeting_ai.api.app import create_app
from local_meeting_ai.config import AppSettings

# Vercel Functions only provide temporary local storage. Keep all local writes
# in /tmp for preview deployments; this does NOT make the data durable across
# function instances or cold starts. See docs/vercel-hosting.md.
DATA_DIR = Path(os.environ.get("M2N_DATA_DIR", "/tmp/oundnote"))
MODELS_DIR = Path(os.environ.get("M2N_MODELS_DIR", "/tmp/oundnote/models"))

settings = AppSettings(
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "3000")),
    data_dir=DATA_DIR,
    models_dir=MODELS_DIR,
    open_browser=False,
    max_upload_mb=int(os.environ.get("M2N_MAX_UPLOAD_MB", "4")),
)

app = create_app(settings)
