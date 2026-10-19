#!/usr/bin/env python3.12
"""Isolated static-identity fixture: a committed create loses its delayed response.

Requires the existing swag-bank on swag-net and freshly built app/edge images.
This fixture deliberately bypasses OIDC; it tests Qt write reconciliation only.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.request
import uuid

_source = Path(__file__).resolve()
ROOT = _source.parents[2] if len(_source.parents) > 2 else Path("/")
OUT = ROOT / "artifacts/uncertain-write-check"
NAMES = ("swag-uncertain-edge", "swag-uncertain-host", "swag-uncertain-proxy")
ORIGIN = "http://localhost:6083"


def evidence():
    try:
        return json.loads((OUT / "proxy.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


async def proxy():
    from aiohttp import ClientSession, web
    path = Path("/evidence/proxy.json")
    stats = {"post_count": 0, "committed_items": [], "response_delay_seconds": 20}

    async def forward(request):
        headers = {name: request.headers[name] for name in
                   ("X-Swag-Service-Token", "X-Swag-User", "Content-Type") if name in request.headers}
        async with ClientSession() as client:
            async with client.request(request.method, "http://swag-bank:8081" + str(request.rel_url),
                                      headers=headers, data=await request.read()) as response:
                data, status, content_type = await response.read(), response.status, response.content_type
        if request.method == "POST" and request.path == "/api/instruments":
            stats["post_count"] += 1
            if status == 201:
                stats["committed_items"].append(json.loads(data)["item"])
            stats["committed_at"] = time.time()
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(stats, indent=2) + "\n"); temporary.replace(path)
            await asyncio.sleep(20)  # Qt aborts at 15s, after the bank committed.
        return web.Response(status=status, body=data, content_type=content_type)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", forward)
    return app


async def cleanup_rows():
    from aiohttp import ClientSession
    path = Path("/evidence/proxy.json")
    if not path.exists():
        return
    headers = {"X-Swag-Service-Token": os.environ["SWAG_SERVICE_TOKEN"], "X-Swag-User": "alice"}
    async with ClientSession(headers=headers) as client:
        for item in json.loads(path.read_text())["committed_items"]:
            async with client.delete(f"http://swag-bank:8081/api/instruments/{item['id']}",
                                     json={"version": item["version"]}) as response:
                assert response.status in (204, 404), f"fixture cleanup failed: HTTP {response.status}"


def run(engine):
    from playwright.sync_api import sync_playwright
    import workspace
    workspace.STACK = workspace.OUTPUT = OUT
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "workspaces").mkdir(exist_ok=True)
    # Keep credentials in a private env file, never command arguments or results.
    token = json.loads((ROOT / "artifacts/stack/config/secrets.json").read_text())["service"]
    key = secrets.token_hex(24)
    env = OUT / "fixture.env"
    env.write_text(f"SWAG_SERVICE_TOKEN={token}\nSWAG_PROXY_KEY={key}\nSWAG_PUBLIC_ORIGIN={ORIGIN}\n"
                   "SWAG_BACKEND_URL=http://swag-uncertain-proxy:8090\nSWAG_VIEWER_LEASE_SECONDS=30\n")
    env.chmod(0o600)
    nginx = OUT / "nginx.conf"
    nginx.write_text('''worker_processes 1;
error_log /dev/stderr warn; pid /tmp/nginx.pid;
events { worker_connections 128; }
http {
 include /etc/nginx/mime.types;
 map $http_upgrade $connection_upgrade { default upgrade; '' close; }
 server {
  listen 8082; root /opt/novnc; index index.html;
  location ~ ^/(session(?:/lease)?|viewer/[^/]+/websockify)$ {
   proxy_pass http://swag-uncertain-host:8080;
   proxy_http_version 1.1;
   proxy_set_header Upgrade $http_upgrade;
   proxy_set_header Connection $connection_upgrade;
   proxy_set_header X-Swag-User alice;
   proxy_set_header X-Swag-Proxy-Key ''' + key + ''';
   proxy_buffering off; proxy_read_timeout 75s;
  }
  location / { try_files $uri $uri/ =404; }
 }
}
''')

    def command(*args, check=True):
        return subprocess.run([engine, *map(str, args)], cwd=ROOT, text=True,
                              capture_output=True, check=check, timeout=45)

    def stop():
        for name in NAMES:
            command("rm", "-f", "--time", "5", name, check=False)

    stop()
    (OUT / "proxy.json").unlink(missing_ok=True)
    common = ["run", "-d", "--network", "swag-net", "--init"]
    keep = ["--userns=keep-id:uid=1000,gid=1000"] if Path(engine).name == "podman" else []
    app_image, edge_image = "localhost/swag-app:dev", "localhost/swag-edge:dev"
    actor = None
    result = {"fixture": "static alice identity; OIDC intentionally bypassed", "status": "fail"}
    try:
        command(*common, "--name", NAMES[2], *keep, "--env-file", env,
                "-v", f"{Path(__file__).resolve()}:/fixture.py:ro,z", "-v", f"{OUT}:/evidence:z",
                "--entrypoint", "/usr/bin/python3.12", app_image, "/fixture.py", "--proxy")
        command(*common, "--name", NAMES[1], *keep, "--env-file", env,
                "-v", f"{OUT / 'workspaces'}:/artifacts:z", app_image)
        command(*common, "--name", NAMES[0], "-p", "127.0.0.1:6083:8082",
                "-v", f"{nginx}:/etc/nginx/nginx.conf:ro,z", edge_image)

        def available():
            try:
                with urllib.request.urlopen(ORIGIN + "/session", timeout=2) as response:
                    return response.status == 200
            except Exception:
                return False
        workspace.wait_for(available, 30)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                actor = workspace.Actor(browser, "alice", ORIGIN)
                actor.page.goto(ORIGIN, wait_until="domcontentloaded")
                actor.session = actor.api()["body"]
                actor.page.locator("#start").click()
                workspace.wait_for(lambda: actor.api()["body"]["state"] == "running", 25)
                actor.connected()
                actor.click("entity_instruments")
                actor.wait(lambda s: s.get("entity") == "instruments" and not s.get("loading"))
                actor.click("new_button")
                symbol, draft = "UWN" + uuid.uuid4().hex[:8].upper(), "Retained uncertain note42"
                actor.edit("draft", draft)
                actor.edit("field_symbol", symbol); actor.edit("field_name", "Uncertain create fixture")
                before = actor.wait(lambda s: s.get("form_values", {}).get("symbol") == symbol)
                actor.click("form_save_button")
                workspace.wait_for(lambda: evidence().get("post_count") == 1, 8)
                state = actor.wait(lambda s: s.get("uncertain_write"), 20)
                assert state["form_values"] == before["form_values"] and state["draft"] == draft
                actor.click("form_save_button"); actor.page.keyboard.press("Enter")
                actor.click("save_button"); actor.page.wait_for_timeout(650)
                assert evidence()["post_count"] == 1
                assert actor.state()["uncertain_write"] and actor.state()["form_values"] == before["form_values"]
                actor.screenshot("retained-before-reconciliation")
                actor.click("reload_button"); actor.click("modal_button_4194304")  # Cancel prompt.
                actor.wait(lambda s: s.get("uncertain_write") and s.get("form_values") == before["form_values"])
                actor.click("reload_button"); actor.click("modal_button_8388608")  # Explicit Discard.
                state = actor.wait(lambda s: not s.get("uncertain_write") and not s.get("loading"))
                assert len([r for r in state["items"] if r.get("symbol") == symbol]) == 1
                assert state["draft"] == draft and evidence()["post_count"] == 1
                actor.screenshot("reconciled-single-row")
                result.update(status="pass", symbol=symbol, committed_posts=1, retained_until_discard=True,
                              retry_blocked=True, reconciled_rows=1)
            except Exception:
                if actor:
                    actor.screenshot("failure")
                    (OUT / "failure-state.json").write_text(json.dumps(actor.state(), indent=2))
                raise
            finally:
                browser.close()
    finally:
        cleanup = command("exec", NAMES[2], "python3.12", "/fixture.py", "--cleanup", check=False)
        for name in NAMES:
            logs = command("logs", name, check=False)
            (OUT / f"{name}.log").write_text(logs.stdout + logs.stderr)
        stop()
        env.unlink(missing_ok=True)
        if cleanup.returncode and evidence().get("committed_items"):
            result.update(status="fail", cleanup="failed")
        (OUT / "results.json").write_text(json.dumps(result, indent=2) + "\n")
        assert not (cleanup.returncode and evidence().get("committed_items")), "Fixture row cleanup failed"
    print("uncertain-write: committed create, blocked retry, explicit reconciliation: pass")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="podman")
    parser.add_argument("--proxy", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    options = parser.parse_args()
    if options.proxy:
        from aiohttp import web
        web.run_app(proxy(), host="0.0.0.0", port=8090, access_log=None)
    elif options.cleanup:
        asyncio.run(cleanup_rows())
    else:
        run(options.engine)
