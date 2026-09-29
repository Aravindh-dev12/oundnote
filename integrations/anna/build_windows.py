"""Build and smoke-test a standalone Windows Executa without the AI stack.

Run: uv run --with pyinstaller==6.20.0 --with httpx --with pydantic
  --with pydantic-settings --with platformdirs build_windows.py
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXECUTA = ROOT / "executas" / "oundnote"
VERSION = "0.1.2"
NAME = "oundnote-anna"


def main():
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("Build on Windows x86_64; cross-compilation is not supported.")
    with tempfile.TemporaryDirectory(prefix="oundnote-anna-build-") as temporary:
        work = Path(temporary)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--onedir",
                "--name",
                NAME,
                "--paths",
                str(ROOT.parents[1] / "src"),
                "--distpath",
                str(work / "dist"),
                "--workpath",
                str(work / "build"),
                "--specpath",
                str(work),
                str(EXECUTA / "oundnote_plugin.py"),
            ],
            check=True,
        )
        package = work / "dist" / NAME
        # Run outside the checkout: no source-path or installed Oundnote dependency.
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "describe"},
            {"jsonrpc": "2.0", "id": 3, "method": "health"},
        ]
        smoke = subprocess.run(
            [str(package / f"{NAME}.exe")],
            input="\n".join(map(json.dumps, messages)) + "\n",
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=work,
            timeout=60,
            check=True,
        )
        replies = [json.loads(line) for line in smoke.stdout.splitlines()]
        assert len(replies) == 3 and replies[1]["result"]["tools"][0]["name"] == "library"
        assert replies[2]["result"]["status"] == "ready"
        manifest = {
            "name": NAME,
            "version": VERSION,
            "runtime": {"binary": {"entrypoint": {"windows-x86_64": f"{NAME}.exe"}}},
        }
        (package / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (package / "LICENSE.txt").write_text(
            (ROOT.parents[1] / "LICENSE").read_text(), encoding="utf-8"
        )
        output = EXECUTA / "dist"
        output.mkdir(exist_ok=True)
        archive = output / f"{NAME}-{VERSION}-windows-x86_64.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    zipped.write(path, path.relative_to(package))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
        print(f"Verified artifact: {archive}\nSHA256: {digest}")


if __name__ == "__main__":
    main()
