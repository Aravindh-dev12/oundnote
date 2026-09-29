#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x ".venv/bin/python" ]]; then
  echo "Oundnote is not installed yet. Run ./install.sh --desktop first." >&2
  exit 1
fi
exec ".venv/bin/python" -m local_meeting_ai.desktop "$@"
