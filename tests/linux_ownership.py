#!/usr/bin/env python3.12
"""Destructive workspace-lifecycle checks for an idle `tools/app.py up` stack.

Use after browser checks: this kills launchers and the session manager, restarts
only swag-host, and finishes with the host running and no Qt workspace. The bank
container is observed, never restarted or mutated. Credentials travel on stdin
to a private API client inside swag-host and never enter logs or command lines.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "artifacts" / "stack"
OUTPUT = ROOT / "artifacts" / "linux-ownership"

API_CLIENT = r'''
import json, sys, urllib.error, urllib.request
p = json.load(sys.stdin)
headers = {'X-Swag-Proxy-Key': p['key'], 'X-Swag-User': p['user'],
           'Origin': p['origin'], 'Content-Type': 'application/json'}
if p.get('csrf'):
    headers['X-Swag-CSRF'] = p['csrf']
if p.get('upgrade'):
    headers.update({'Connection': 'Upgrade', 'Upgrade': 'websocket',
                    'Sec-WebSocket-Key': 'MDEyMzQ1Njc4OWFiY2RlZg==',
                    'Sec-WebSocket-Version': '13'})
data = json.dumps(p.get('body', {})).encode() if p['method'] != 'GET' else None
request = urllib.request.Request('http://127.0.0.1:8080' + p['path'], data=data,
                                 headers=headers, method=p['method'])
try:
    response = urllib.request.urlopen(request, timeout=8)
except urllib.error.HTTPError as error:
    response = error
with response:
    raw = response.read().decode()
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    print(json.dumps({'status': response.status, 'body': body}))
'''

PROCESS_RECORDS = r'''
import json, pathlib
records = []
modules = {'swag_session', 'swag_session.launcher', 'swag_session.processes'}
names = {'swag-desktop', 'Xvnc', 'openbox', 'vncconfig'}
for p in pathlib.Path('/proc').iterdir():
    if not p.name.isdecimal():
        continue
    try:
        name = (p / 'comm').read_text().strip()
        argv = [x.decode(errors='replace') for x in (p / 'cmdline').read_bytes().split(b'\0') if x]
        module = next((argv[i + 1] for i, value in enumerate(argv[:-1])
                       if value == '-m' and argv[i + 1] in modules), None)
        if name not in names and module is None:
            continue
        env = dict(x.split(b'=', 1) for x in (p / 'environ').read_bytes().split(b'\0') if b'=' in x)
        fields = (p / 'stat').read_text().rsplit(')', 1)[1].split()
        uid = int((p / 'status').read_text().split('Uid:')[1].split()[0])
        records.append({'pid': int(p.name), 'ppid': int(fields[1]), 'uid': uid,
                        'start_ticks': int(fields[19]), 'state': fields[0],
                        'name': name, 'module': module,
                        'user': env.get(b'SWAG_USER', b'').decode(),
                        'generation': env.get(b'SWAG_GENERATION', b'').decode(),
                        'manager_pid': int(env.get(b'SWAG_MANAGER_PID', b'0'))})
    except (FileNotFoundError, ProcessLookupError):
        pass
print(json.dumps(records))
'''


class Ownership:
    def __init__(self, engine: str):
        self.engine = engine
        self.key = json.loads((STACK / "config" / "secrets.json").read_text())["proxy"]
        self.origin = json.loads((STACK / "public.json").read_text())["origin"]
        self.sessions: dict[str, dict] = {}
        self.results: list[dict] = []
        self.started = time.monotonic()
        self.log = None

    def command(self, *args: str, data: str | None = None, check=True):
        result = subprocess.run([self.engine, *args], cwd=ROOT, text=True,
                                input=data, capture_output=True, timeout=15)
        if check and result.returncode:
            # Captured output may contain an API CSRF token: keep failure text
            # bounded to the operation and exit status, never dump it.
            raise RuntimeError(f"container {args[0]} failed with exit {result.returncode}")
        return result

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def note(self, message: str) -> None:
        line = f"linux-ownership: {message}"
        print(line, flush=True)
        if self.log:
            self.log.write(line + "\n")
            self.log.flush()

    def save(self, status: str, error: str | None = None) -> None:
        result = {"status": status, "checks": self.results,
                  "elapsed_seconds": round(time.monotonic() - self.started, 3)}
        if error:
            result["error"] = error
        (OUTPUT / "results.json").write_text(json.dumps(result, indent=2) + "\n")

    def check(self, name: str, detail: dict | None = None) -> None:
        self.results.append({"check": name, "status": "pass", **(detail or {})})
        self.save("running")
        self.note(f"{name}: pass")

    def inspect(self, name="swag-host") -> dict:
        return json.loads(self.command("inspect", name).stdout)[0]

    def records(self) -> list[dict]:
        return json.loads(self.command("exec", "swag-host", "/usr/bin/python3.12",
                                       "-c", PROCESS_RECORDS).stdout)

    def api(self, user="alice", method="GET", path="/session", body=None, upgrade=False):
        payload = {"key": self.key, "user": user, "origin": self.origin,
                   "method": method, "path": path, "body": body or {}, "upgrade": upgrade,
                   "csrf": self.sessions.get(user, {}).get("csrf_token")}
        response = json.loads(self.command("exec", "-i", "swag-host", "/usr/bin/python3.12",
                                           "-c", API_CLIENT, data=json.dumps(payload)).stdout)
        if response["status"] == 200 and path == "/session":
            self.sessions[user] = response["body"]
        return response

    def session(self, user="alice") -> dict:
        response = self.api(user)
        self.require(response["status"] == 200, f"{user} session read failed")
        return response["body"]

    def start(self, user="alice") -> dict:
        self.session(user)
        response = self.api(user, "POST")
        self.require(response["status"] == 200, f"{user} explicit start failed")
        self.require(response["body"]["state"] == "running", f"{user} did not become running")
        return response["body"]

    def end(self, user="alice") -> None:
        self.session(user)
        response = self.api(user, "DELETE")
        self.require(response["status"] == 200, f"{user} terminate failed")
        self.require(response["body"]["state"] == "ended", f"{user} did not end")

    def wait(self, function, description: str, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = function()
            if value:
                return value
            time.sleep(0.1)
        raise AssertionError(description)

    def workspace_records(self, session: dict) -> list[dict]:
        return [p for p in self.records() if p["generation"] == session["generation"]
                and p["user"] == session["user"]]

    def desktops(self) -> list[dict]:
        return [p for p in self.records() if p["name"] == "swag-desktop"]

    def kill(self, pid: int) -> None:
        self.command("exec", "swag-host", "/usr/bin/python3.12", "-c",
                     f"import os,signal; os.kill({pid},signal.SIGKILL)", check=False)

    def bank_identity(self) -> dict:
        bank = self.inspect("swag-bank")
        return {"id": bank["Id"], "pid": bank["State"]["Pid"],
                "started_at": bank["State"]["StartedAt"], "running": bank["State"]["Running"]}

    def bank_healthy(self) -> bool:
        code = ("import urllib.request; "
                "r=urllib.request.urlopen('http://swag-bank:8081/health',timeout=3); "
                "assert r.status==200")
        return self.command("exec", "swag-host", "/usr/bin/python3.12", "-c", code,
                            check=False).returncode == 0

    def ensure_host(self) -> None:
        if not self.inspect()["State"]["Running"]:
            self.command("start", "swag-host")
        self.sessions.clear()

        def healthy():
            result = self.command("exec", "-i", "swag-host", "/usr/bin/python3.12", "-c",
                                  API_CLIENT, data=json.dumps({"key": self.key, "user": "alice",
                                  "origin": self.origin, "method": "GET", "path": "/health"}), check=False)
            return result.returncode == 0 and json.loads(result.stdout)["status"] == 200

        self.wait(healthy, "host did not become healthy after restart", timeout=10)

    def run_checks(self) -> None:
        bank = self.bank_identity()
        self.require(bank["running"] and self.bank_healthy(), "bank must be healthy before checks")
        for user in ("alice", "bob"):
            self.end(user)
        self.require(not self.desktops(), "baseline must have zero desktops")
        alice, bob = self.start("alice"), self.start("bob")
        desktops = self.desktops()
        self.require(len(desktops) == 2, "expected exactly two desktops")
        self.require({p["user"] for p in desktops} == {"alice", "bob"}, "desktop ownership mismatch")
        self.require(all(p["uid"] == 1000 for p in desktops), "desktops must share service UID1000")
        self.require({p["pid"] for p in desktops} == {alice["pid"], bob["pid"]}, "API PID mismatch")
        self.check("two_users_one_process_each_shared_uid", {"processes": desktops})

        duplicate = self.command("exec", "--env", "SWAG_USER=alice", "--env",
            "SWAG_SESSION_LOCK_FILE=/run/swag/users/alice/session.lock", "swag-host",
            "/opt/swag/bin/swag-desktop", check=False)
        self.require(duplicate.returncode == 73, "second direct Qt invocation must exit73")
        self.require(len(self.desktops()) == 2, "duplicate invocation changed live desktop count")
        self.check("qt_lifetime_lock_rejects_duplicate", {"exit_code": duplicate.returncode})

        owned = self.workspace_records(alice)
        launcher = [p for p in owned if p["module"] == "swag_session.launcher"]
        self.require(len(launcher) == 1, "alice must have one launcher")
        self.require(all(p["uid"] == 1000 for p in owned), "workspace helpers must share UID1000")
        expected = {"swag-desktop", "Xvnc", "openbox", "vncconfig"}
        self.require(expected <= {p["name"] for p in owned}, "workspace helper inventory incomplete")
        old_identities = {(p["pid"], p["start_ticks"]) for p in owned}
        self.kill(launcher[0]["pid"])
        self.wait(lambda: not old_identities.intersection(
            (p["pid"], p["start_ticks"]) for p in self.records()), "launcher death left owned processes")
        ended = self.wait(lambda: s if (s := self.session("alice"))["state"] == "ended" else None,
                          "launcher death did not end generation")
        self.require(ended["generation"] == alice["generation"] and ended["pid"] is None,
                     "ended generation identity changed")
        bob_after = self.session("bob")
        self.require((bob_after["generation"], bob_after["pid"]) == (bob["generation"], bob["pid"]),
                     "alice launcher death affected bob")
        self.require(self.api("alice", path=f"/viewer/{alice['generation']}/websockify",
                              upgrade=True)["status"] == 404, "dead viewer unexpectedly usable")
        self.require(self.session("alice")["state"] == "ended", "viewer request relaunched app")
        self.check("launcher_sigkill_reaps_qt_and_helpers", {"ended_generation": alice["generation"],
                                                           "terminated_processes": owned,
                                                           "bob_pid_preserved": bob["pid"]})

        restarted = self.start("alice")
        self.require(restarted["generation"] != alice["generation"], "restart reused generation")
        self.require(len(self.desktops()) == 2, "explicit restart violated desktop count")
        self.check("explicit_restart_after_launcher_death", {"generation": restarted["generation"]})

        launcher = next(p for p in self.workspace_records(restarted)
                        if p["module"] == "swag_session.launcher")
        manager_pid = launcher["manager_pid"]
        managers = [p for p in self.records() if p["pid"] == manager_pid
                    and p["module"] == "swag_session"]
        self.require(len(managers) == 1, "could not establish launcher owning manager")
        self.kill(manager_pid)
        stopped = self.wait(lambda: s if not (s := self.inspect())["State"]["Running"] else None,
                            "manager SIGKILL did not stop app-host container")
        self.require(stopped["State"]["Pid"] == 0, "stopped app-host still reports a live init")
        self.check("manager_sigkill_stops_container", {"manager_pid": manager_pid,
                                                       "container_running": False,
                                                       "container_init_pid": 0})

        self.ensure_host()
        for user in ("alice", "bob"):
            session = self.session(user)
            self.require(session["state"] == "absent" and session["pid"] is None
                         and session["generation"] is None, "host restart silently restored workspace")
        self.require(not self.desktops(), "host restart launched Qt without explicit start")
        self.require(not any(p["module"] == "swag_session.launcher" for p in self.records()),
                     "host restart retained old launchers")
        self.check("host_restart_is_absent_without_autolaunch")

        last = self.start("alice")
        desktops = self.desktops()
        self.require(len(desktops) == 1 and desktops[0]["pid"] == last["pid"]
                     and desktops[0]["user"] == "alice", "post-restart start must create one desktop")
        self.require(self.session("bob")["state"] == "absent", "alice restart launched bob")
        self.check("explicit_start_after_manager_restart", {"process": desktops[0]})
        for user in ("alice", "bob"):
            self.end(user)
        self.require(not self.desktops(), "final cleanup left Qt running")
        self.require(self.bank_identity() == bank and self.bank_healthy(), "bank changed or became unhealthy")
        self.check("final_host_running_no_qt_bank_unchanged", {"host_running": True, "qt_count": 0,
                                                             "bank_process_unchanged": True})

    def run(self) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        error = None
        with (OUTPUT / "run.log").open("w") as self.log:
            try:
                self.run_checks()
            except Exception as failure:
                error = f"{type(failure).__name__}: {failure}"
                self.note(error)
            finally:
                try:
                    self.ensure_host()
                    for user in ("alice", "bob"):
                        self.end(user)
                    self.require(not self.desktops(), "cleanup left Qt running")
                except Exception as failure:
                    cleanup_error = f"cleanup failed: {type(failure).__name__}: {failure}"
                    error = f"{error}; {cleanup_error}" if error else cleanup_error
                    self.note(cleanup_error)
                self.save("fail" if error else "pass", error)
        if error:
            raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="podman")
    args = parser.parse_args()
    Ownership(args.engine).run()


if __name__ == "__main__":
    main()
