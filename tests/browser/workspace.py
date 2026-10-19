#!/usr/bin/env python3.12
"""Real OIDC/browser/canvas integration checks against `tools/app.py up`."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
STACK = ROOT / "artifacts/stack"
OUTPUT = ROOT / "artifacts/workspace-check"


def wait_for(function, timeout=12):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = function()
        if last:
            return last
        time.sleep(.08)
    raise AssertionError(f"Condition timed out; last result: {last}")


class Actor:
    def __init__(self, browser, user, origin):
        self.user, self.origin, self.session = user, origin, None
        self.context = browser.new_context(viewport={"width": 1440, "height": 1040})
        self.page = self.context.new_page()
        self.page.set_default_timeout(15000)

    def login(self):
        self.page.goto(self.origin, wait_until="domcontentloaded")
        self.page.locator("#username").fill(self.user)
        self.page.locator("#password").fill("demo")
        self.page.locator("#kc-login").click()
        self.page.wait_for_url(self.origin + "/", wait_until="domcontentloaded")
        self.page.locator("#user").filter(has_text=self.user).wait_for()
        self.session = self.api()["body"]
        assert self.session["user"] == self.user

    def api(self, method="GET", path="/session", body=None, headers=None):
        result = self.page.evaluate("""async ({method,path,body,headers}) => {
          const r=await fetch(path,{method,headers,body:method==='GET'?undefined:JSON.stringify(body)});
          const text=await r.text();let parsed;try{parsed=JSON.parse(text)}catch{parsed=text}
          return {status:r.status,body:parsed};
        }""", {"method": method, "path": path, "body": body, "headers": headers or {
            "Content-Type": "application/json", "X-Swag-CSRF": (self.session or {}).get("csrf_token", "")}})
        if path == "/session" and result["status"] == 200:
            self.session = result["body"]
        return result

    def state(self):
        if not self.session or not self.session.get("generation"):
            return {}
        path = STACK / "workspaces/users" / self.user / self.session["generation"] / "state.json"
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def wait(self, predicate, timeout=12):
        return wait_for(lambda: (state if predicate(state := self.state()) else None), timeout)

    def connected(self):
        self.page.locator("#status").filter(has_text="Connected").wait_for(timeout=20000)
        self.page.locator("#screen canvas").wait_for()
        self.wait(lambda s: s.get("entity") == "portfolios" and len(s.get("items", [])) >= 2)
        self.page.wait_for_timeout(350)

    def click(self, name):
        state = self.wait(lambda s: name in s.get("geometry", {}))
        x, y, width, height = state["geometry"][name]
        canvas = self.page.locator("#screen canvas").bounding_box()
        assert canvas and width > 0 and height > 0, name
        self.page.mouse.click(canvas["x"] + (x + width/2)*canvas["width"]/1280,
                              canvas["y"] + (y + height/2)*canvas["height"]/800)
        # A click may need an X11 window-manager focus round trip before typing.
        self.page.wait_for_timeout(160)

    def edit(self, field, value):
        self.click(field)
        self.page.keyboard.press("Control+a")
        self.page.wait_for_timeout(60)
        self.page.keyboard.type(value,delay=12)

    def screenshot(self, name):
        self.page.wait_for_timeout(200)
        self.page.screenshot(path=str(OUTPUT / f"{name}.png"))


class Suite:
    def __init__(self, engine, origin):
        self.engine, self.origin, self.results = engine, origin, []

    def command(self, *args, check=True):
        return subprocess.run([self.engine, *args], cwd=ROOT, text=True, capture_output=True, check=check, timeout=45)

    def check(self, name, function):
        detail = function() or {}
        self.results.append({"check": name, "status": "pass", **detail})
        (OUTPUT / "results.json").write_text(json.dumps({"checks": self.results}, indent=2) + "\n")
        print(f"workspace: {name}: pass", flush=True)

    def processes(self):
        code = """import json,pathlib
result={}
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdecimal(): continue
 try:
  if (p/'comm').read_text().strip() != 'swag-desktop': continue
  env=dict(x.split(b'=',1) for x in (p/'environ').read_bytes().split(b'\\0') if b'=' in x)
  user=env.get(b'SWAG_USER',b'').decode()
  uid=(p/'status').read_text().split('Uid:')[1].split()[0]
  result.setdefault(user,[]).append({'pid':int(p.name),'uid':int(uid)})
 except (FileNotFoundError,ProcessLookupError): pass
print(json.dumps(result))
"""
        return json.loads(self.command("exec", "swag-host", "python3.12", "-c", code).stdout)

    def run(self):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            alice, bob = Actor(browser, "alice", self.origin), Actor(browser, "bob", self.origin)
            self.actors = (alice, bob)
            try:
                def login():
                    assert browser.new_context().request.get(self.origin + "/session").status == 401
                    alice.login(); bob.login()
                    return {"users": [a.session["user"] for a in self.actors], "oidc": "Keycloak via OAuth2 Proxy"}
                self.check("oidc_two_users", login)

                def starts():
                    # Explicit API starts stress the same authenticated operation as the Start button.
                    for actor in self.actors:
                        responses = actor.page.evaluate("""async csrf => Promise.all(Array.from({length:12},()=>
                          fetch('/session',{method:'POST',headers:{'X-Swag-CSRF':csrf,'Content-Type':'application/json'},body:'{}'})
                            .then(async r=>({status:r.status,body:await r.json()}))))""", actor.session["csrf_token"])
                        assert all(r["status"] == 200 for r in responses), responses
                        identities = {(r["body"]["generation"], r["body"]["pid"]) for r in responses}
                        assert len(identities) == 1, identities
                        actor.session = responses[0]["body"]
                        actor.page.reload(wait_until="domcontentloaded"); actor.connected()
                    processes = self.processes()
                    assert set(processes) == {"alice", "bob"}, processes
                    assert all(len(v) == 1 and v[0]["uid"] == 1000 for v in processes.values()), processes
                    return {"concurrent_starts_per_user": 12, "processes": processes}
                self.check("one_instance_per_user_shared_uid", starts)

                def boundaries():
                    spoof = alice.api(headers={"X-Swag-User": "bob", "X-Swag-Proxy-Key": "forged"})
                    assert spoof["body"]["user"] == "alice", spoof
                    assert alice.api("POST", headers={"Content-Type":"application/json"})["status"] == 403
                    csrf = alice.session["csrf_token"]
                    wrong_origin = alice.context.request.post(self.origin + "/session", headers={"Origin":"https://evil.example","X-Swag-CSRF":csrf}, data="{}")
                    assert wrong_origin.status == 403
                    headers = {"Connection":"Upgrade","Upgrade":"websocket","Sec-WebSocket-Key":"MDEyMzQ1Njc4OWFiY2RlZg==","Sec-WebSocket-Version":"13","Origin":self.origin}
                    cross = alice.context.request.get(self.origin + f"/viewer/{bob.session['generation']}/websockify", headers=headers)
                    assert cross.status == 404, cross.status
                    headers["Origin"] = "https://evil.example"
                    bad_origin = alice.context.request.get(self.origin + f"/viewer/{alice.session['generation']}/websockify", headers=headers)
                    assert bad_origin.status == 403
                    listeners = self.command("exec","swag-host","ss","-ltn").stdout
                    assert ":5900" not in listeners and ":6000" not in listeners, listeners
                    return {"spoofed_identity_ignored":True,"csrf":403,"origin":403,"cross_user_generation":404,"rfb_tcp_disabled":True}
                self.check("proxy_and_viewer_boundaries", boundaries)

                def entitlements():
                    a, b = alice.state()["items"], bob.state()["items"]
                    ai, bi = {r["id"] for r in a}, {r["id"] for r in b}
                    assert len(ai & bi) == 1 and len(ai - bi) == 1 and len(bi - ai) == 1, (a,b)
                    self.shared_id = next(iter(ai & bi))
                    for actor in self.actors:
                        row = next(i for i,r in enumerate(actor.state()["items"]) if r["id"] == self.shared_id)
                        actor.click(f"grid_row_{row}")
                        actor.wait(lambda s: s.get("selected_id") == self.shared_id)
                    alice.screenshot("alice-initial"); bob.screenshot("bob-initial")
                    return {"alice_ids":sorted(ai),"bob_ids":sorted(bi)}
                self.check("generated_grids_private_and_shared_books", entitlements)

                def conflict():
                    name = "Shared browser review " + uuid.uuid4().hex[:6]
                    alice.edit("draft", "Alice unsaved note42")
                    alice.edit("field_name", name)
                    alice.wait(lambda s: s.get("dirty") and s.get("form_values",{}).get("name") == name)
                    bob.edit("field_name", "Bob competing draft42")
                    bob.wait(lambda s: s.get("dirty"))
                    alice.click("form_save_button")
                    alice.wait(lambda s: not s.get("dirty") and any(r.get("name") == name for r in s.get("items",[])))
                    bob.click("form_save_button")
                    state = bob.wait(lambda s: s.get("dirty") and s.get("error_text", "").startswith("Conflict:"))
                    assert state["form_values"]["name"] == "Bob competing draft42", state
                    bob.screenshot("conflict-retains-draft")
                    bob.click("form_cancel_button"); bob.click("reload_button")
                    bob.wait(lambda s: any(r.get("name") == name for r in s.get("items",[])))
                    self.saved_name = name
                    return {"saved_name":name,"stale_save":409,"draft_retained":True}
                self.check("shared_edit_and_conflict", conflict)

                def reconnect():
                    before = dict(alice.session)
                    alice.page.reload(wait_until="domcontentloaded"); alice.connected()
                    after = alice.api()["body"]
                    assert (before["generation"],before["pid"]) == (after["generation"],after["pid"])
                    assert alice.state()["draft"] == "Alice unsaved note42"
                    # Another authenticated tab takes the viewer, retaining the same Qt process.
                    other = alice.context.new_page(); other.goto(self.origin,wait_until="domcontentloaded")
                    other.locator("#status").filter(has_text="Connected").wait_for()
                    alice.page.locator("#status").filter(has_text="Viewer disconnected").wait_for()
                    other.close()
                    alice.page.locator("#start").click(); alice.connected()
                    assert alice.api()["body"]["pid"] == before["pid"]
                    return {"same_pid":before["pid"],"same_generation":before["generation"],"draft_preserved":True,"takeover":True}
                self.check("reconnect_and_takeover", reconnect)

                def crud():
                    # Exercise New/Save/Delete through generated Qt form and browser canvas.
                    alice.click("entity_instruments")
                    alice.wait(lambda s: s.get("entity") == "instruments" and s.get("items"))
                    alice.click("new_button")
                    symbol = "TEST" + uuid.uuid4().hex[:6].upper()
                    alice.edit("field_symbol",symbol); alice.edit("field_name","Synthetic browser instrument")
                    alice.click("form_save_button")
                    state = alice.wait(lambda s: not s.get("dirty") and any(r.get("symbol")==symbol for r in s.get("items",[])))
                    item = next(r for r in state["items"] if r.get("symbol")==symbol)
                    assert item["created_by"] == "alice" and item["version"] == 1
                    alice.screenshot("generated-create")
                    alice.click("delete_button")
                    alice.click("modal_button_16384")
                    alice.wait(lambda s: all(r.get("symbol")!=symbol for r in s.get("items",[])))
                    return {"created_and_deleted":symbol,"actor":"alice"}
                self.check("generated_crud", crud)

                def bank_restart():
                    self.command("restart","--time","10","swag-bank")
                    time.sleep(1)
                    alice.click("entity_portfolios")
                    alice.wait(lambda s: s.get("entity")=="portfolios" and any(r.get("name")==self.saved_name for r in s.get("items",[])))
                    return {"shared_edit_survived_service_restart":True}
                self.check("sqlite_persistence", bank_restart)

                def app_death():
                    old = dict(alice.api()["body"])
                    self.command("exec","swag-host","kill","-KILL",str(old["pid"]))
                    wait_for(lambda: alice.api()["body"]["state"] == "ended")
                    alice.page.locator("#status").filter(has_text="Workspace ended").wait_for()
                    time.sleep(.5)
                    assert "alice" not in self.processes()
                    headers={"Connection":"Upgrade","Upgrade":"websocket","Sec-WebSocket-Key":"MDEyMzQ1Njc4OWFiY2RlZg==","Sec-WebSocket-Version":"13","Origin":self.origin}
                    assert alice.context.request.get(self.origin+f"/viewer/{old['generation']}/websockify",headers=headers).status==404
                    assert alice.api()["body"]["state"]=="ended"
                    alice.page.locator("#start").click(); alice.connected()
                    fresh=alice.api()["body"]
                    assert fresh["generation"]!=old["generation"] and fresh["pid"]!=old["pid"]
                    return {"old_generation_rejected":True,"no_implicit_restart":True,"explicit_restart_new_generation":True}
                self.check("app_death_revokes_generation", app_death)

                def normal_quit():
                    bob.click("quit_button")
                    wait_for(lambda: bob.api()["body"]["state"]=="ended")
                    wait_for(lambda: "bob" not in self.processes())
                    assert alice.api()["body"]["state"]=="running"
                    return {"bob_ended":True,"alice_unaffected":True}
                self.check("normal_quit", normal_quit)

                def logout():
                    alice.page.locator("#signout").click()
                    alice.page.wait_for_url(self.origin+"/signed-out")
                    assert alice.context.request.get(self.origin+"/session").status==401
                    wait_for(lambda: not self.processes())
                    return {"browser_auth_removed":True,"all_qt_processes_stopped":True}
                self.check("end_session_signout", logout)
            except Exception as error:
                for actor in self.actors:
                    try:
                        actor.screenshot(actor.user+"-failure")
                        (OUTPUT/(actor.user+"-failure-state.json")).write_text(json.dumps(actor.state(),indent=2))
                    except Exception:
                        pass
                self.results.append({"status":"fail","error":str(error)})
                (OUTPUT/"results.json").write_text(json.dumps({"checks":self.results},indent=2)+"\n")
                raise
            finally:
                browser.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine",default="podman")
    parser.add_argument("--origin",default="http://localhost:6081")
    args=parser.parse_args()
    Suite(args.engine,args.origin).run()


if __name__=="__main__":
    main()
