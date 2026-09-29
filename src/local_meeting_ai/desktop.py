from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_HOTKEY = "<ctrl>+<alt>+space"


class DesktopBridge:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.main_window = None

    def open_main(self, meeting_id: int | None = None) -> None:
        if self.main_window is None:
            return
        self.main_window.show()
        if meeting_id:
            self.main_window.load_url(f"{self.base_url}/capture?meeting={meeting_id}")

    def open_home(self) -> None:
        self.open_main()
        if self.main_window is not None:
            self.main_window.load_url(f"{self.base_url}/")

def _request(url: str, method: str = "GET", body: bytes | None = None) -> tuple[int, bytes]:
    request = Request(url, data=body, method=method, headers={"Accept": "application/json"})
    with urlopen(request, timeout=3) as response:
        return int(response.status), response.read()

def _wait_for_server(base_url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _ = _request(base_url + "/")
            if 200 <= status < 500:
                return True
        except (OSError, URLError):
            pass
        time.sleep(0.25)
    return False

def _start_backend(host: str, port: int) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    return subprocess.Popen(
        [sys.executable, "-m", "local_meeting_ai", "--host", host, "--port", str(port), "--no-browser"],
        env=env, stdin=subprocess.DEVNULL, stdout=None, stderr=None, text=True,
    )

def _shutdown_backend(base_url: str, process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        _request(base_url + "/api/application/shutdown", method="POST")
    except (OSError, URLError):
        pass
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()

def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="oundnote-desktop",
        description="Launch Oundnote in a native desktop window with a floating Flow Bar.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--hotkey", default=DEFAULT_HOTKEY)
    parser.add_argument("--no-hotkey", action="store_true")
    parser.add_argument("--no-flowbar", action="store_true")
    parser.add_argument("--debug-webview", action="store_true")
    arguments = parser.parse_args(argv)

    try:
        import webview
    except ImportError as error:
        raise SystemExit("The desktop runtime is not installed. Run `python -m pip install -e '.[desktop]'`.") from error

    base_url = f"http://{arguments.host}:{arguments.port}"
    backend = None
    if not _wait_for_server(base_url, 1.0):
        backend = _start_backend(arguments.host, arguments.port)
        if not _wait_for_server(base_url, 30.0):
            raise SystemExit(f"Oundnote did not become ready at {base_url}. Check the local startup log.")

    bridge = DesktopBridge(base_url)
    main_window = webview.create_window(
        "Oundnote", f"{base_url}/", width=1240, height=820, min_size=(960, 640),
        resizable=True, background_color="#f7f8fb",
    )
    bridge.main_window = main_window
    flowbar = None
    if not arguments.no_flowbar:
        flowbar = webview.create_window(
            "Oundnote Flow Bar", f"{base_url}/flowbar", width=360, height=82,
            min_size=(300, 72), resizable=False, frameless=True, easy_drag=False,
            on_top=True, shadow=True, background_color="#15171d", x=20, y=20,
            js_api=bridge,
        )

    hotkey_listener = None
    if not arguments.no_hotkey and flowbar is not None:
        try:
            from pynput import keyboard
            hotkey_listener = keyboard.GlobalHotKeys({
                arguments.hotkey: lambda: flowbar.run_js(
                    "window.__oundnoteHotkey && window.__oundnoteHotkey();"
                )
            })
            hotkey_listener.start()
        except Exception as error:
            print(f"Oundnote global hotkey disabled: {error}", file=sys.stderr)

    def shutdown() -> None:
        nonlocal hotkey_listener
        if hotkey_listener is not None:
            try:
                hotkey_listener.stop()
            except Exception:
                pass
            hotkey_listener = None
        _shutdown_backend(base_url, backend)

    main_window.events.closed += shutdown
    try:
        webview.start(debug=arguments.debug_webview)
    finally:
        shutdown()
    return 0

if __name__ == "__main__":
    raise SystemExit(run())