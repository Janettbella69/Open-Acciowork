"""Exercise the built macOS WebKit shell against a second loopback origin.

Run on a Mac with a GUI session after ``zsh desktop/macos/build-app.sh``.
The script uses a copy of the app and a temporary data directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

APP = Path(__file__).resolve().parents[1] / "build" / "OPHIRA.app"


def main() -> None:
    if not APP.is_dir():
        raise SystemExit("Build the desktop app before running this smoke test")
    foreign_requests: list[str] = []

    class ForeignOrigin(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            foreign_requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"outside")

        def log_message(self, *_args: object) -> None:
            pass

    with TemporaryDirectory(prefix="ophira-web-policy-") as temporary:
        root = Path(temporary)
        app = root / "OPHIRA.app"
        shutil.copytree(APP, app)
        foreign = ThreadingHTTPServer(("127.0.0.1", 0), ForeignOrigin)
        thread = Thread(target=foreign.serve_forever, daemon=True)
        thread.start()
        try:
            foreign_url = f"http://127.0.0.1:{foreign.server_port}"
            ui = app / "Contents/Resources/ui"
            index = ui / "index.html"
            index_html = index.read_text(encoding="utf-8")
            assert "<head>" in index_html
            index.write_text(index_html.replace(
                "<head>",
                '<head><meta http-equiv="refresh" content="0;url=/diagnostic.html">',
                1,
            ), encoding="utf-8")
            # StaticFiles serves this local document without the shell CSP. It
            # checks that the native content rules alone block another port.
            (ui / "diagnostic.html").write_text(f"""<!doctype html><html><body>
<img src="{foreign_url}/image"><img src="/diagnostic-redirect">
<script src="{foreign_url}/script"></script>
<iframe src="{foreign_url}/frame"></iframe>
<script>
fetch('{foreign_url}/fetch').catch(() => {{}});
try {{ new WebSocket('ws://127.0.0.1:{foreign.server_port}/websocket'); }} catch (_) {{}}
fetch('/api/desktop/status').then(r => {{
  if (r.status === 200) fetch('/diagnostic-pass');
}});
</script></body></html>""", encoding="utf-8")
            host = app / "Contents/Resources/backend/src/ophira_backend/desktop_host.py"
            host_source = host.read_text(encoding="utf-8")
            marker = "        path = request.url.path\n"
            assert host_source.count(marker) == 1
            fixture = """
        if path in ("/diagnostic.html", "/diagnostic-pass"):
            __import__("sys").stderr.write("NETWORK-SMOKE " + path + "\\n")
            __import__("sys").stderr.flush()
        if path == "/diagnostic-redirect":
            await JSONResponse({}, status_code=302, headers={"Location": REDIRECT_URL})(
                scope, receive, send
            )
            return
""".replace("REDIRECT_URL", repr(f"{foreign_url}/redirect"))
            host.write_text(host_source.replace(marker, marker + fixture, 1), encoding="utf-8")

            environment = os.environ.copy()
            environment["OPHIRA_DESKTOP_DATA_DIR"] = str(root / "data")
            process = subprocess.Popen(
                [str(app / "Contents/MacOS/OPHIRA")],
                env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True,
            )
            lines: list[str] = []

            def read_stderr() -> None:
                assert process.stderr is not None
                for line in process.stderr:
                    lines.append(line.rstrip())

            Thread(target=read_stderr, daemon=True).start()
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and process.poll() is None:
                    if any("NETWORK-SMOKE /diagnostic-pass" in line for line in lines):
                        break
                    time.sleep(0.2)
                time.sleep(0.8)
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            assert any("NETWORK-SMOKE /diagnostic.html" in line for line in lines), lines[-20:]
            assert any("NETWORK-SMOKE /diagnostic-pass" in line for line in lines), lines[-20:]
            assert foreign_requests == [], foreign_requests
            print("WebKit exact-origin resource and local API smoke passed")
        finally:
            foreign.shutdown()
            foreign.server_close()


if __name__ == "__main__":
    main()
