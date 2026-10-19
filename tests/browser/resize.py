#!/usr/bin/env python3.12
"""Check browser-sized Xvnc/Qt geometry without losing live desktop state."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

import workspace

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://localhost:6081")
    args = parser.parse_args()
    results, current = [], "initial_connect_fills_canvas"
    output = ROOT / "artifacts/resize-check"
    output.mkdir(parents=True, exist_ok=True)
    workspace.OUTPUT = output
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        actor = workspace.Actor(browser, "alice", args.origin)
        try:
            actor.login()
            actor.api("DELETE")
            actor.api("POST")
            actor.page.reload(wait_until="domcontentloaded")
            actor.connected()
            mode = actor.state()["ui_mode"]
            output = ROOT / f"artifacts/resize-{mode}-check"
            output.mkdir(parents=True, exist_ok=True)
            workspace.OUTPUT = output
            identity = (actor.session["pid"], actor.session["generation"])

            def save():
                (output / "results.json").write_text(json.dumps({"ui_mode": mode, "checks": results}, indent=2)+"\n")

            def passed(**details):
                results.append({"check": current, "status": "pass", **details})
                save()
                print(f"resize ({mode}): {current}: pass", flush=True)

            def fitted():
                def matches():
                    state = actor.state()
                    size = actor.page.locator("#screen").evaluate("e => [Math.floor(e.clientWidth), Math.floor(e.clientHeight)]")
                    if actor.framebuffer_size() != size or state.get("framebuffer_size") != size:
                        return False
                    if state.get("geometry", {}).get("window") != [0, 0, *size]:
                        return False
                    canvas = actor.page.locator("#screen canvas").bounding_box()
                    return size if all(abs(canvas[key]-value) <= 1 for key, value in zip(("width", "height"), size)) else False
                size = workspace.wait_for(matches, 15)
                assert actor.page.evaluate("document.documentElement.scrollHeight <= innerHeight+1")
                return size

            def forms():
                state = actor.state()
                panes = state["windows"] if mode == "mdi" else [{**state, "window_id": "single"}]
                return {w["window_id"]: {k: w[k] for k in ("entity", "form_values", "dirty", "selected_id")}
                        for w in panes}

            def retained():
                state = actor.state()
                assert state["draft"] == "Browser resize note"
                assert forms() == original_forms
                session = actor.api()["body"]
                assert (session["pid"], session["generation"]) == identity

            passed(framebuffer=fitted())
            actor.edit("field_name", "Unsaved browser resize edit")
            actor.wait(lambda s: s.get("dirty") and s.get("form_values", {}).get("name") == "Unsaved browser resize edit")
            actor.edit("draft", "Browser resize note")
            if mode == "mdi":
                actor.click("entity_positions")
                actor.wait(lambda s: s.get("entity") == "positions" and not s.get("loading"))
                actor.click("entity_instruments")
                actor.wait(lambda s: s.get("entity") == "instruments" and not s.get("loading"))
                actor.click("entity_portfolios")
                actor.wait(lambda s: s.get("entity") == "portfolios")
            original_forms = forms()

            current = "browser_resize_preserves_forms"
            if mode == "mdi":
                actor.click("mdi_maximize_button")
                actor.wait(lambda s: any(w["active"] and w["maximized"] for w in s["windows"]))
            measured = []
            for width, height in ((1680, 1100), (1000, 800), (1440, 1040)):
                actor.page.set_viewport_size({"width": width, "height": height})
                measured.append({"browser": [width, height], "framebuffer": fitted()})
                retained()
                if mode == "mdi":
                    state = actor.state()
                    child = next(w for w in state["windows"] if w["active"])
                    assert child["maximized"]
                    x, y, w, h = child["geometry"]["window"]
                    vx, vy, vw, vh = state["geometry"]["mdi_viewport"]
                    assert x <= vx and y <= vy and x+w >= vx+vw and y+h >= vy+vh
            actor.screenshot("resized-desktop")
            passed(sizes=measured, frame_count=len(original_forms), same_process=True)

            current = "small_browser_scales_full_desktop"
            previous_size = actor.framebuffer_size()

            def scaled():
                canvas = actor.page.locator("#screen canvas").bounding_box()
                screen = actor.page.locator("#screen").bounding_box()
                state = actor.state()
                return (actor.framebuffer_size() == previous_size
                        and state["geometry"]["window"] == [0, 0, *previous_size]
                        and canvas["x"] >= screen["x"]-1 and canvas["y"] >= screen["y"]-1
                        and canvas["x"]+canvas["width"] <= screen["x"]+screen["width"]+1
                        and canvas["y"]+canvas["height"] <= screen["y"]+screen["height"]+1
                        and min(abs(canvas["width"]-screen["width"]), abs(canvas["height"]-screen["height"])) <= 1)

            for width, height in ((800, 600), (640, 480)):
                actor.page.set_viewport_size({"width": width, "height": height})
                workspace.wait_for(scaled, 10)
                actor.page.wait_for_timeout(300)  # Include noVNC's delayed resize callback.
                assert scaled()
                retained()
            actor.page.goto("about:blank")
            actor.page.goto(args.origin, wait_until="domcontentloaded")
            actor.page.locator("#status").filter(has_text="Connected").wait_for(timeout=20000)
            workspace.wait_for(scaled, 10)
            retained()
            actor.screenshot("small-browser-whole-desktop")
            actor.page.set_viewport_size({"width": 1440, "height": 1040})
            fitted()
            passed(browser_sizes=[[800, 600], [640, 480]], small_reconnect=True,
                   full_framebuffer_retained=True, remote_resize_resumed=True)

            current = "resize_with_open_dialog"
            actor.click("mdi_close_button" if mode == "mdi" else "quit_button")
            actor.wait(lambda s: "modal" in s["geometry"])
            actor.page.set_viewport_size({"width": 1180, "height": 900})
            width, height = fitted()
            state = actor.wait(lambda s: "modal" in s["geometry"])
            x, y, w, h = state["geometry"]["modal"]
            assert 0 <= x and 0 <= y and x+w <= width and y+h <= height
            actor.page.keyboard.press("Escape")
            actor.wait(lambda s: "modal" not in s["geometry"])
            retained()
            passed(dialog_visible=True, cancel_retained_edits=True)

            current = "reconnect_at_different_size"
            actor.page.goto("about:blank")
            actor.page.set_viewport_size({"width": 1680, "height": 1100})
            actor.page.goto(args.origin, wait_until="domcontentloaded")
            actor.page.locator("#status").filter(has_text="Connected").wait_for(timeout=20000)
            size = fitted()
            retained()
            passed(framebuffer=size, same_process=True, all_forms_retained=True)

            current = "viewer_takeover_owns_size"
            old_page = actor.page
            actor.page = actor.context.new_page()
            actor.page.set_viewport_size({"width": 1320, "height": 960})
            actor.page.goto(args.origin, wait_until="domcontentloaded")
            actor.page.locator("#status").filter(has_text="Connected").wait_for(timeout=20000)
            old_page.locator("#status").filter(has_text="Viewer disconnected").wait_for()
            size = fitted()
            retained()
            old_page.close()
            actor.screenshot("takeover-sized-desktop")
            passed(framebuffer=size, same_process=True, previous_viewer_disconnected=True)
            actor.api("DELETE")
        except Exception as error:
            print(f"resize: {current}: fail: {error}", flush=True)
            results.append({"check": current, "status": "fail", "error": str(error)})
            (output / "results.json").write_text(json.dumps({"checks": results}, indent=2)+"\n")
            actor.page.screenshot(path=str(output / "failure.png"))
            (output / "failure-state.json").write_text(json.dumps(actor.state(), indent=2)+"\n")
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
