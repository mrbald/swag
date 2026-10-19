#!/usr/bin/env python3.12
"""Logout/relogin regressions against the running OIDC demo; uses Bob only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from workspace import Actor

OUTPUT = Path(__file__).resolve().parents[2] / "artifacts/login-state"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://localhost:6081")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    results = []
    current_check = "initial_login"
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True,
                                    ignore_default_args=["--disable-back-forward-cache"])
        actor = Actor(browser, "bob", args.origin)
        try:
            actor.login()

            def passed(name, **details):
                results.append({"check": name, "status": "pass", **details})
                print(f"login-state: {name}: pass", flush=True)

            def ready(page):
                page.wait_for_url(args.origin + "/", wait_until="domcontentloaded")
                page.locator("#user").filter(has_text="bob").wait_for()
                page.wait_for_function("document.querySelector('#start').textContent !== 'Sign in'")
                assert "Login expired" not in page.locator("#status").inner_text()

            def reauthenticate(page):
                link = page.get_by_role("link", name="Sign in again")
                assert link.get_attribute("href").startswith("/oauth2/start")
                link.click()
                ready(page)  # Keycloak SSO remains active after app logout.

            def start():
                actor.session = actor.api()["body"]
                if actor.session["state"] != "running":
                    actor.page.locator("#start").click()
                actor.page.locator("#status").filter(has_text="Connected").wait_for(timeout=20000)
                actor.session = actor.api()["body"]
                actor.connected()
                return dict(actor.session)

            current_check = "cache_headers"
            for path in ("/", "/index.html", "/session"):
                response = actor.context.request.get(args.origin + path)
                assert response.status == 200, (path, response.status)
                assert "no-store" in response.headers.get("cache-control", ""), path
            unauthenticated = browser.new_context()
            response = unauthenticated.request.get(args.origin + "/session")
            assert response.status == 401
            assert "no-store" in response.headers.get("cache-control", "")
            unauthenticated.close()
            passed(current_check, protected_html_and_api=True, unauthenticated_api=True)

            current_check = "repeated_logout_login"
            original = start()
            for _ in range(6):
                actor.page.locator("#signout").click()
                actor.page.wait_for_url(args.origin + "/signed-out")
                assert actor.context.request.get(args.origin + "/session").status == 401
                reauthenticate(actor.page)
                actor.session = actor.api()["body"]
                assert actor.session["state"] == "ended" and actor.session["pid"] is None
                assert actor.session["generation"] == original["generation"]
                assert actor.page.locator("#start").inner_text() == "Start workspace"
                assert actor.page.locator("#start").is_enabled()
            passed(current_check, cycles=6, no_implicit_desktop_restart=True)

            current_check = "expired_tab_recovers_after_login_elsewhere"
            original = start()
            actor.edit("draft", "Reauthentication retains this draft")
            actor.wait(lambda state: state.get("draft") == "Reauthentication retains this draft")
            second = actor.context.new_page()
            second.goto(args.origin + "/oauth2/sign_out?rd=/signed-out")
            actor.page.locator("#start").filter(has_text="Sign in").wait_for(timeout=12000)
            reauthenticate(second)
            second.locator("#status").filter(has_text="Connected").wait_for()
            actor.page.bring_to_front()
            actor.page.wait_for_function("document.querySelector('#start').textContent === 'Rejoin workspace'")
            assert "Login expired" not in actor.page.locator("#status").inner_text()
            # Recovery must not automatically take the viewer away from the other tab.
            actor.page.wait_for_timeout(3300)
            assert second.locator("#status").inner_text() == "Connected"
            actor.page.locator("#start").click()
            actor.connected()
            second.locator("#status").filter(has_text="Viewer disconnected").wait_for()
            after = actor.api()["body"]
            assert (after["pid"], after["generation"]) == (original["pid"], original["generation"])
            assert actor.state()["draft"] == "Reauthentication retains this draft"
            actor.page.screenshot(path=str(OUTPUT / "recovered-tab.png"))
            second.close()
            passed(current_check, same_process_and_draft=True, no_automatic_viewer_takeover=True)

            current_check = "obsolete_poll_and_lease_during_logout"
            pending = {}

            def hold(route):
                method = route.request.method
                key = "lease" if route.request.url.endswith("/lease") else method
                if key in pending:
                    route.continue_()
                else:
                    pending[key] = (route, route.fetch())

            actor.page.route("**/session", hold)
            actor.page.route("**/session/lease", hold)
            # Hold actual periodic GET/lease responses after the server processes them.
            for _ in range(24):
                if "GET" in pending and "lease" in pending:
                    break
                actor.page.wait_for_timeout(500)
            assert "GET" in pending and "lease" in pending, list(pending)
            actor.page.locator("#signout").click()
            for _ in range(20):
                if "DELETE" in pending:
                    break
                actor.page.wait_for_timeout(100)
            assert "DELETE" in pending
            # Inject old unauthorized completions while the newer DELETE is pending.
            for key in ("GET", "lease"):
                pending[key][0].fulfill(status=401, headers={"Cache-Control": "no-store"}, body="expired")
            actor.page.wait_for_timeout(300)
            assert actor.page.locator("#status").inner_text() == "Signing out…"
            assert actor.page.locator("#start").inner_text() != "Sign in"
            pending["DELETE"][0].fulfill(response=pending["DELETE"][1])
            actor.page.wait_for_url(args.origin + "/signed-out")
            actor.page.unroute_all(behavior="ignoreErrors")
            reauthenticate(actor.page)
            passed(current_check, obsolete_unauthorized_responses_ignored=True)

            current_check = "history_and_restored_page_revalidation"
            actor.page.goto(args.origin + "/signed-out", wait_until="domcontentloaded")
            actor.page.go_back(wait_until="domcontentloaded")
            ready(actor.page)
            # Exercise the persisted pageshow path even if Chrome declines BFCache.
            with actor.page.expect_navigation(wait_until="domcontentloaded"):
                actor.page.evaluate("dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))")
            ready(actor.page)
            passed(current_check, history_navigation=True, persisted_event_forces_revalidation=True)

            actor.page.locator("#signout").click()
            actor.page.wait_for_url(args.origin + "/signed-out")
        except Exception as error:
            results.append({"check": current_check, "status": "fail", "error": str(error)})
            try:
                actor.page.screenshot(path=str(OUTPUT / "regression-failure.png"))
            except Exception:
                pass
            raise
        finally:
            browser.close()
            (OUTPUT / "results.json").write_text(json.dumps({"checks": results}, indent=2) + "\n")


if __name__ == "__main__":
    main()
