"""Read-only loopback report browser. Never exposes source checkouts or arbitrary files."""

from __future__ import annotations

import html
import http.server
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from divergence.artifacts import MIB


def make_server(root: Path, port: int = 8741) -> http.server.ThreadingHTTPServer:
    root = root.resolve()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            host = self.headers.get("Host", "").split(":")[0]
            if host not in {"localhost", "127.0.0.1"}:
                self.send_error(403, "Loopback host required")
                return
            route = unquote(urlsplit(self.path).path)
            if route == "/":
                rows = []
                for manifest in sorted(
                    root.glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True
                ):
                    if not manifest.resolve().is_relative_to(root):
                        continue
                    try:
                        if manifest.stat().st_size > 64 * MIB:
                            continue
                        run = json.loads(manifest.read_text(encoding="utf-8"))
                        scenario_name = run["scenario"]["name"]
                        status, created_at = run["status"], run["created_at"]
                        if not all(
                            isinstance(item, str) for item in (scenario_name, status, created_at)
                        ):
                            continue
                    except (ValueError, OSError, KeyError, TypeError):
                        continue
                    if not (manifest.parent / "report.html").exists():
                        continue
                    name = manifest.parent.name
                    rows.append(
                        f'<a href="/{html.escape(name, quote=True)}/report.html"><b>{html.escape(scenario_name)}</b><span>{html.escape(status)} · {html.escape(created_at[:16])}</span></a>'
                    )
                body = (
                    '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Divergence · Investigations</title>'
                    "<style>body{font:15px/1.6 system-ui;background:#f4f7f6;color:#19372d;max-width:900px;margin:70px auto;padding:20px}h1{letter-spacing:-1px}p{color:#61746e}a{display:flex;justify-content:space-between;gap:20px;padding:22px;border:1px solid #d5e0db;border-radius:8px;background:white;margin:12px 0;text-decoration:none;color:inherit}a:hover{border-color:#478771}span{font-size:12px;color:#687970}</style>"
                    "<h1>Divergence</h1><p>Your local regression investigations. Reports contain captured evidence, not live application state.</p>"
                    + (
                        "".join(rows)
                        or "<p>No reports yet. Run <code>divergence demo --run</code> to create your first investigation.</p>"
                    )
                    + "</html>"
                ).encode()
            else:
                parts = route.strip("/").split("/")
                if len(parts) != 2 or parts[1] != "report.html" or parts[0] in {".", ".."}:
                    self.send_error(404)
                    return
                path = (root / parts[0] / "report.html").resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    self.send_error(404)
                    return
                body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
