#!/usr/bin/env python3.12
"""Measure compact chrome and exercise browser fullscreen through real controls."""
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
    output = ROOT / "artifacts/presentation-check"
    output.mkdir(parents=True, exist_ok=True)
    workspace.OUTPUT = output
    checks, current = [], "compact_chrome"
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        actor = workspace.Actor(browser, "alice", args.origin)
        try:
            actor.login(); actor.api("DELETE"); actor.api("POST")
            actor.page.reload(wait_until="domcontentloaded"); actor.connected()
            identity = (actor.session["pid"], actor.session["generation"])

            def passed(**details):
                checks.append({"check": current, "status": "pass", **details})
                (output / "results.json").write_text(json.dumps({"checks": checks}, indent=2)+"\n")
                print(f"presentation: {current}: pass", flush=True)

            def fitted():
                def matches():
                    size = actor.page.locator("#screen").evaluate("e => [Math.floor(e.clientWidth),Math.floor(e.clientHeight)]")
                    state = actor.state()
                    return size if (actor.framebuffer_size() == size and state.get("framebuffer_size") == size
                                    and state["geometry"]["window"] == [0, 0, *size]) else False
                return workspace.wait_for(matches, 15)

            def same_workspace():
                session = actor.api()["body"]
                assert (session["pid"], session["generation"]) == identity
                assert actor.state()["form_values"] == retained_form

            def enter_fullscreen():
                actor.page.locator("#fullscreen").click()
                actor.page.wait_for_function("document.fullscreenElement?.id === 'workspace'")
                actor.page.wait_for_function("document.querySelector('#fullscreen').textContent === 'Exit full screen'")
                fitted()
                actor.page.mouse.move(actor.page.evaluate("innerWidth/2"), actor.page.evaluate("innerHeight/2"))

            def reveal():
                actor.page.mouse.move(actor.page.evaluate("innerWidth/2"), 5)
                actor.page.wait_for_function("document.querySelector('#workspace').classList.contains('controls-visible')")

            size = fitted()
            viewport = actor.page.evaluate("[innerWidth,innerHeight]")
            assert size == [viewport[0], viewport[1]-36], (size, viewport)
            assert actor.page.locator("#start").is_hidden()
            assert actor.page.locator("#clipboard").is_hidden() and actor.page.locator("#signout").is_hidden()
            state = actor.state()
            assert state["geometry"]["draft"][1] < 12
            if state["ui_mode"] == "mdi":
                x, y, w, h = state["geometry"]["mdi_viewport"]
                assert y <= 48 and size[1]-(y+h) <= 10
            actor.screenshot("compact")
            passed(viewport=viewport, framebuffer=size, browser_overhead_pixels=36)

            current = "clipboard_focus_and_round_trip"
            actor.edit("field_name", "Retained fullscreen edit")
            actor.wait(lambda s: s.get("dirty") and s["form_values"].get("name") == "Retained fullscreen edit")
            retained_form = actor.state()["form_values"]
            actor.edit("draft", "Qt note remains while typing in browser")
            actor.open_clipboard()
            actor.page.locator("#clipboard").fill("Clipboard local input")
            assert actor.state()["draft"] == "Qt note remains while typing in browser"
            actor.page.locator("#paste").click()
            actor.page.keyboard.press("Control+a"); actor.page.keyboard.press("Control+v")
            actor.wait(lambda s: s.get("draft") == "Clipboard local input")
            actor.edit("draft", "Qt clipboard outbound")
            actor.page.keyboard.press("Control+a"); actor.page.keyboard.press("Control+c")
            actor.page.wait_for_function("document.querySelector('#clipboard').value === 'Qt clipboard outbound'")
            same_workspace()
            passed(dom_typing_did_not_reach_qt=True, both_directions=True, canvas_focus_after_send=True)

            current = "fullscreen_canvas_fills_viewport"
            enter_fullscreen()
            size = fitted()
            assert size == actor.page.evaluate("[innerWidth,innerHeight]")
            same_workspace()
            passed(framebuffer=size, fullscreen_element="workspace", browser_overhead_pixels=0)

            current = "controls_reveal_without_resizing"
            actor.page.wait_for_function("!document.querySelector('#workspace').classList.contains('controls-visible')")
            actor.screenshot("fullscreen")
            before = actor.framebuffer_size()
            reveal()
            actor.open_clipboard()
            actor.page.locator("#clipboard").fill("Fullscreen browser input")
            actor.page.wait_for_timeout(2000)
            assert actor.page.locator("#clipboard").is_visible()
            assert actor.framebuffer_size() == before
            assert actor.state()["draft"] == "Qt clipboard outbound"
            actor.page.locator("#clipboard-close").click()
            actor.page.mouse.move(400, 300)
            actor.page.wait_for_function("!document.querySelector('#workspace').classList.contains('controls-visible')")
            assert fitted() == before
            same_workspace()
            passed(popover_pins_controls=True, reveal_does_not_resize=True, same_process=True)

            current = "browser_escape_restores_normal_view"
            actor.page.keyboard.press("Escape")
            actor.page.wait_for_function("document.fullscreenElement === null")
            actor.page.wait_for_function("document.querySelector('#fullscreen').textContent === 'Full screen'")
            size = fitted(); viewport = actor.page.evaluate("[innerWidth,innerHeight]")
            assert size == [viewport[0], viewport[1]-36]
            same_workspace()
            passed(edits_retained=True, same_process=True)

            current = "fullscreen_rejection_and_retry"
            # Fault injection exercises rejection; the control still supplies the user gesture.
            actor.page.evaluate("""() => { window.savedFullscreen=Element.prototype.requestFullscreen;
              Element.prototype.requestFullscreen=()=>Promise.reject(new DOMException('Fixture rejection','NotAllowedError')); }""")
            actor.page.locator("#fullscreen").click()
            actor.page.locator("#status").filter(has_text="Full screen is unavailable").wait_for()
            assert actor.page.evaluate("document.fullscreenElement === null")
            same_workspace()
            actor.page.evaluate("() => { Element.prototype.requestFullscreen=window.savedFullscreen; delete window.savedFullscreen; }")
            enter_fullscreen()
            passed(rejection_visible=True, retry_succeeds=True, workspace_unchanged=True)

            current = "fullscreen_recovery_controls"
            actor.api("DELETE")
            actor.page.locator("#status").filter(has_text="Workspace ended").wait_for(timeout=15000)
            assert actor.page.evaluate("document.fullscreenElement?.id === 'workspace'")
            assert actor.page.locator("#start").is_visible() and actor.page.locator("#start").is_enabled()
            bar = actor.page.locator("#bar").bounding_box()
            assert bar["y"] == 0
            actor.page.locator("#start").click()
            actor.connected()
            assert actor.api()["body"]["generation"] != identity[1]
            passed(ended_workspace_controls_visible=True, explicit_restart_in_fullscreen=True)
            actor.page.keyboard.press("Escape")
            actor.page.wait_for_function("document.fullscreenElement === null")
            actor.api("DELETE")
        except Exception as error:
            print(f"presentation: {current}: fail: {error}", flush=True)
            checks.append({"check": current, "status": "fail", "error": str(error)})
            (output / "results.json").write_text(json.dumps({"checks": checks}, indent=2)+"\n")
            actor.page.screenshot(path=str(output / "failure.png"))
            (output / "failure-state.json").write_text(json.dumps(actor.state(), indent=2)+"\n")
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
