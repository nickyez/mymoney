#!/usr/bin/env python3
"""Run a Docker Compose clean-install, onboarding, and persistence smoke test."""
from __future__ import annotations

import http.cookiejar
import json
import os
import pathlib
import secrets
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECT = "mymoney-ci"
SESSION_SECRET = "ci-session-secret-that-is-long-enough-12345"
PASSWORD = "ci-test-password-123"


def wait_for_health(opener: urllib.request.OpenerDirector, base: str) -> None:
    for _ in range(60):
        try:
            with opener.open(base + "/health", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(2)
    raise RuntimeError("backend did not become healthy")


def main() -> None:
    env = os.environ.copy()
    env["MYMONEY_SESSION_SECRET"] = SESSION_SECRET
    env["MYMONEY_CI_PORT"] = str(18000 + secrets.randbelow(1000))
    env["COMPOSE_FILE"] = "docker-compose.yml:scripts/ci-compose.override.yml"
    backend_url = f"http://127.0.0.1:{env['MYMONEY_CI_PORT']}"
    compose = ["docker", "compose", "-p", PROJECT]
    proxy: ThreadingHTTPServer | None = None
    if shutil.which("docker") is None:
        raise SystemExit("Docker is required for this clean-install test")
    try:
        subprocess.run(compose + ["up", "-d", "--build", "backend"], cwd=ROOT, env=env, check=True)
        with tempfile.TemporaryDirectory(prefix="mymoney-ci-") as temp_dir:
            temp = pathlib.Path(temp_dir)
            key = temp / "server.key"
            cert = temp / "server.crt"
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cert), "-days", "1",
                "-subj", "/CN=localhost",
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            class ProxyHandler(BaseHTTPRequestHandler):
                def do_GET(self): self.forward()
                def do_POST(self): self.forward()
                def do_PUT(self): self.forward()
                def do_DELETE(self): self.forward()

                def forward(self) -> None:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = self.rfile.read(length) if length else None
                    headers = {key: value for key, value in self.headers.items()
                               if key.lower() not in {"host", "connection"}}
                    request = urllib.request.Request(
                        backend_url + self.path,
                        data=body,
                        headers=headers,
                        method=self.command,
                    )
                    try:
                        with urllib.request.urlopen(request, timeout=10) as response:
                            self.reply(response.status, response.headers, response.read())
                    except urllib.error.HTTPError as error:
                        self.reply(error.code, error.headers, error.read())

                def reply(self, status: int, headers, payload: bytes) -> None:
                    self.send_response(status)
                    for key, value in headers.items():
                        if key.lower() not in {"transfer-encoding", "connection", "content-length"}:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def log_message(self, format: str, *args) -> None:
                    pass

            proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            proxy.socket = context.wrap_socket(proxy.socket, server_side=True)
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            base = f"https://127.0.0.1:{proxy.server_port}/api"
            cookies = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
                urllib.request.HTTPCookieProcessor(cookies),
            )
            wait_for_health(opener, base)

            with opener.open(base + "/auth/status") as response:
                status = json.load(response)
            assert status == {"authenticated": False, "setup_required": True}, status

            setup = urllib.request.Request(
                base + "/auth/setup",
                data=json.dumps({"password": PASSWORD}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(setup) as response:
                result = json.load(response)
            assert result["setup_complete"] is True, result
            assert any(cookie.name == "mymoney_session" and cookie.secure for cookie in cookies)

            transaction = {
                "date": "2026-01-02", "kind": "expense", "amount": 12.5,
                "description": "CI persistence fixture", "category": "food",
                "payment_method": "Cash", "notes": "",
            }
            create = urllib.request.Request(
                base + "/transactions",
                data=json.dumps(transaction).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(create) as response:
                assert response.status == 201

            subprocess.run(compose + ["up", "-d", "--force-recreate", "backend"],
                           cwd=ROOT, env=env, check=True)
            wait_for_health(opener, base)
            cookies.clear()
            login = urllib.request.Request(
                base + "/auth/login",
                data=json.dumps({"password": PASSWORD}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(login) as response:
                assert response.status == 200
            with opener.open(base + "/transactions?start=2026-01-01&end=2026-01-31&limit=10") as response:
                rows = json.load(response)["items"]
            assert any(row["description"] == transaction["description"] for row in rows), rows
            print("Docker clean install, password setup, manual transaction, and volume persistence passed.")
    finally:
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()
        try:
            subprocess.run(compose + ["down", "-v", "--remove-orphans"], cwd=ROOT, env=env, check=False)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
