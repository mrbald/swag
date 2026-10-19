#!/usr/bin/env python3.12
"""Small, repo-local Podman/Docker driver for the authenticated topology."""
from __future__ import annotations

import argparse
import base64
import json
import ipaddress
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "artifacts" / "stack"
CONFIG = STATE / "config"
APP_IMAGE = "localhost/swag-app:dev"
EDGE_IMAGE = "localhost/swag-edge:dev"
KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:26.8.1@sha256:79c623a22abacf3fe6c00a5fb30f5b7843d717b234033b80995891c06116d244"
OAUTH_IMAGE = "quay.io/oauth2-proxy/oauth2-proxy:v7.15.5@sha256:8498b0d0ef0a7b29686414000a08aee467f02d0299c9ed1e006a8f33fc017916"
NAMES = ("swag-edge", "swag-oauth", "swag-host", "swag-bank", "swag-keycloak")
NETWORK = "swag-net"


def run(engine: str, *args: str, check: bool = True, capture: bool = False):
    result = subprocess.run([engine, *map(str, args)], cwd=ROOT, check=False,
                            text=True, capture_output=capture)
    if check and result.returncode:
        raise SystemExit(result.stderr.strip() if capture else f"{engine} failed ({result.returncode})")
    return result


def write(path: Path, contents: str, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    path.chmod(mode)


def configure(port: int, dns: str = "127.0.0.11"):
    CONFIG.mkdir(parents=True, exist_ok=True)
    CONFIG.chmod(0o700)
    secret_file = CONFIG / "secrets.json"
    if secret_file.exists():
        values = json.loads(secret_file.read_text())
    else:
        values = {name: secrets.token_urlsafe(32) for name in ("proxy", "service", "client", "admin")}
        values["cookie"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        write(secret_file, json.dumps(values))
    origin = f"http://localhost:{port}"
    issuer = f"{origin}/auth/realms/swag"
    internal = "http://swag-keycloak:8080/auth/realms/swag/protocol/openid-connect"
    # Deliberately synthetic local identities. No external IdP or bank connection.
    realm = {
        "realm": "swag", "enabled": True, "sslRequired": "none", "registrationAllowed": False,
        "loginWithEmailAllowed": False, "duplicateEmailsAllowed": False,
        "clients": [{"clientId": "swag", "enabled": True, "protocol": "openid-connect",
                     "publicClient": False, "secret": values["client"], "standardFlowEnabled": True,
                     "directAccessGrantsEnabled": False,
                     "redirectUris": [f"{origin}/oauth2/callback"], "webOrigins": [origin],
                     "defaultClientScopes": ["profile", "email"],
                     "attributes": {"pkce.code.challenge.method": "S256"}}],
        "users": [{"username": user, "enabled": True, "email": f"{user}@example.test",
                   "emailVerified": True, "firstName": user.title(), "lastName": "Demo",
                   "credentials": [{"type": "password", "value": "demo", "temporary": False}]}
                  for user in ("alice", "bob")],
    }
    write(CONFIG / "swag-realm.json", json.dumps(realm, indent=2), 0o644)
    oauth = {
        "provider": "oidc", "provider_display_name": "Local bank identity",
        "http_address": "0.0.0.0:4180", "reverse_proxy": True,
        "client_id": "swag", "client_secret": values["client"],
        "cookie_secret": values["cookie"], "cookie_name": "swag_auth",
        "cookie_secure": False, "cookie_httponly": True, "cookie_samesite": "lax",
        "cookie_expire": "1h", "cookie_refresh": "5m",
        "email_domains": ["example.test"], "scope": "openid profile email",
        "set_xauthrequest": True, "skip_provider_button": True,
        "skip_oidc_discovery": True, "oidc_issuer_url": issuer,
        "login_url": issuer + "/protocol/openid-connect/auth",
        "redeem_url": internal + "/token", "oidc_jwks_url": internal + "/certs",
        "profile_url": internal + "/userinfo", "redirect_url": origin + "/oauth2/callback",
        "code_challenge_method": "S256", "insecure_oidc_skip_nonce": False,
        "upstreams": ["static://202"], "whitelist_domains": [f"localhost:{port}"],
        "request_logging": False,
    }
    write(CONFIG / "oauth.cfg", "\n".join(f"{key} = {json.dumps(value)}" for key, value in oauth.items()) + "\n", 0o644)
    nginx = (ROOT / "containers/nginx.conf.in").read_text()
    for key, value in {"HOST": f"localhost:{port}", "PORT": str(port), "ORIGIN": origin,
                       "PROXY_KEY": values["proxy"], "DNS": dns}.items():
        nginx = nginx.replace(f"@{key}@", value)
    write(CONFIG / "nginx.conf", nginx, 0o644)
    write(CONFIG / "host.env", f"SWAG_PROXY_KEY={values['proxy']}\nSWAG_PUBLIC_ORIGIN={origin}\n"
          f"SWAG_SERVICE_TOKEN={values['service']}\nSWAG_BACKEND_URL=http://swag-bank:8081\n"
          "SWAG_VIEWER_LEASE_SECONDS=30\n")
    write(CONFIG / "bank.env", f"SWAG_SERVICE_TOKEN={values['service']}\nSWAG_DB_PATH=/data/bank.sqlite3\n")
    write(CONFIG / "keycloak.env", f"KC_BOOTSTRAP_ADMIN_USERNAME=admin\nKC_BOOTSTRAP_ADMIN_PASSWORD={values['admin']}\n"
          f"KC_HOSTNAME={origin}/auth\nKC_HTTP_RELATIVE_PATH=/auth\nKC_PROXY_HEADERS=xforwarded\n"
          "JAVA_OPTS_KC_HEAP=-Xms128m -Xmx512m\n")
    for directory in ("workspaces", "data"):
        (STATE / directory).mkdir(parents=True, exist_ok=True)
    write(STATE / "public.json", json.dumps({"origin": origin, "users": ["alice", "bob"]}, indent=2))


def down(engine: str):
    for name in NAMES:
        result = run(engine, "container", "inspect", name, check=False, capture=True)
        if result.returncode == 0:
            run(engine, "rm", "-f", "--time", "10", name, capture=True)


def build(engine: str):
    for target, tag in (("app", APP_IMAGE), ("edge", EDGE_IMAGE)):
        run(engine, "build", "--target", target, "-f", "containers/Containerfile", "-t", tag, ".")


def healthy(engine: str) -> tuple[bool, str]:
    for name in NAMES:
        state = run(engine, "inspect", name, "--format", "{{.State.Running}}", check=False, capture=True)
        if state.returncode or state.stdout.strip() != "true":
            return False, f"stopped container {name}"
    checks = """import sys,urllib.request
for name,url in (('swag-host','http://127.0.0.1:8080/health'),('swag-bank','http://swag-bank:8081/health'),('swag-oauth','http://swag-oauth:4180/ping')):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            assert response.status == 200
    except Exception:
        print(name + ' health endpoint'); sys.exit(1)
"""
    status = run(engine, "exec", "swag-host", "/usr/bin/python3.12", "-c", checks, check=False, capture=True)
    return (True, "") if status.returncode == 0 else (False, status.stdout.strip() or "swag-host health probe")


def up(engine: str, port: int):
    down(engine)
    if run(engine, "network", "inspect", NETWORK, check=False, capture=True).returncode:
        run(engine, "network", "create", "--label", "swag.stack=demo", NETWORK, capture=True)
    network = json.loads(run(engine, "network", "inspect", NETWORK, capture=True).stdout)[0]
    dns = network["subnets"][0]["gateway"] if Path(engine).name == "podman" else "127.0.0.11"
    subnet = network["subnets"][0]["subnet"] if Path(engine).name == "podman" else network["IPAM"]["Config"][0]["Subnet"]
    # A stable service address models the VIP the real shared backend would have.
    # Qt5 caches DNS; a container restart must not strand existing GUI clients.
    bank_address = str(ipaddress.ip_network(subnet)[100])
    configure(port, dns)
    common = ["run", "-d", "--network", NETWORK, "--label", "swag.stack=demo", "--init"]
    keep_id = ["--userns=keep-id:uid=1000,gid=1000"] if Path(engine).name == "podman" else []

    def launch(name: str, image: str, options: list[str], command=()):
        run(engine, *common, "--name", name, *options, image, *command, capture=True)

    launch("swag-keycloak", KEYCLOAK_IMAGE, [*keep_id, "--env-file", CONFIG / "keycloak.env",
           "-v", f"{CONFIG / 'swag-realm.json'}:/opt/keycloak/data/import/swag-realm.json:ro,Z"],
           ["start-dev", "--import-realm"])
    launch("swag-bank", APP_IMAGE, [*keep_id, "--ip", bank_address, "--env-file", CONFIG / "bank.env",
           "-v", f"{STATE / 'data'}:/data:Z", "--entrypoint", "/usr/bin/python3.12"], ["-m", "swag_bank"])
    launch("swag-host", APP_IMAGE, [*keep_id, "--env-file", CONFIG / "host.env",
           "-v", f"{STATE / 'workspaces'}:/artifacts:Z"])
    launch("swag-oauth", OAUTH_IMAGE, ["-v", f"{CONFIG / 'oauth.cfg'}:/etc/swag/oauth.cfg:ro,Z"],
           ["--config=/etc/swag/oauth.cfg"])
    launch("swag-edge", EDGE_IMAGE, ["-p", f"127.0.0.1:{port}:8082",
           "-v", f"{CONFIG / 'nginx.conf'}:/etc/nginx/nginx.conf:ro,Z"])
    origin = f"http://localhost:{port}"
    deadline = time.monotonic() + 150
    last_failure = "identity provider"
    while time.monotonic() < deadline:
        ready, last_failure = healthy(engine)
        if last_failure.startswith("stopped container"):
            raise SystemExit(f"Stack did not become ready: {last_failure}; inspect its container logs")
        try:
            with urllib.request.urlopen(f"{origin}/auth/realms/swag/.well-known/openid-configuration", timeout=2) as response:
                if response.status == 200:
                    if ready:
                        # The protected boundary must reject unauthenticated API requests.
                        try:
                            urllib.request.urlopen(f"{origin}/session", timeout=2)
                        except urllib.error.HTTPError as error:
                            if error.code == 401:
                                break
                        last_failure = "swag-edge authentication boundary"
        except (urllib.error.URLError, TimeoutError):
            last_failure = last_failure or "swag-keycloak discovery endpoint"
        time.sleep(.5)
    else:
        raise SystemExit(f"Stack did not become ready: {last_failure}; inspect its container logs")
    images = {}
    for image in (APP_IMAGE, EDGE_IMAGE, KEYCLOAK_IMAGE, OAUTH_IMAGE):
        images[image] = json.loads(run(engine, "image", "inspect", image, capture=True).stdout)[0]["Id"]
    write(STATE / "images.json", json.dumps(images, indent=2))
    print(f"Workspace: {origin}\nSynthetic logins: alice / demo, bob / demo\nState and logs: {STATE}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "up", "down"))
    parser.add_argument("--engine", default="podman")
    parser.add_argument("--port", default=6081, type=int)
    args = parser.parse_args()
    if args.action == "build":
        build(args.engine)
    elif args.action == "up":
        up(args.engine, args.port)
    else:
        down(args.engine)


if __name__ == "__main__":
    main()
