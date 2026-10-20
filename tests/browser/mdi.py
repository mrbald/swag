#!/usr/bin/env python3.12
"""Exercise independent Qt MDI frames through OIDC/noVNC, without Openbox."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

import workspace

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "artifacts/mdi-check"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="podman")
    parser.add_argument("--origin", default="http://localhost:6081")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    workspace.OUTPUT = OUTPUT
    results = []
    current = "start"
    suite = workspace.Suite(args.engine, args.origin)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        actor = workspace.Actor(browser, "alice", args.origin)
        try:
            actor.login()
            actor.api("DELETE")  # A repeat run starts with a fresh test workspace.
            actor.api("POST")
            actor.page.reload(wait_until="domcontentloaded")
            actor.connected()

            def windows():
                return {w["window_id"]: w for w in actor.state().get("windows", [])}

            def active():
                return actor.state().get("active_window_id")

            def activate(identifier):
                if active() != identifier:
                    # A browser reload leaves HTML focus outside noVNC's canvas.
                    actor.click("draft")
                for _ in range(len(windows()) + 1):
                    if active() == identifier:
                        return
                    before = active()
                    actor.page.keyboard.press("Control+Tab")
                    actor.wait(lambda s: s.get("active_window_id") != before)
                    # Qt publishes activation before its raise/focus events finish.
                    actor.page.wait_for_timeout(250)
                raise AssertionError(f"Cannot activate frame {identifier}")

            def point(rect, fx=.5, fy=.5):
                return actor.canvas_point(rect, fx, fy)

            def drag(rect, delta):
                x, y = point(rect)
                canvas = actor.page.locator("#screen canvas").bounding_box()
                remote_width, remote_height = actor.framebuffer_size()
                actor.page.mouse.move(x, y)
                actor.page.mouse.down()
                actor.page.mouse.move(x+delta[0]*canvas["width"]/remote_width,
                                      y+delta[1]*canvas["height"]/remote_height, steps=12)
                actor.page.mouse.up()
                actor.page.wait_for_timeout(300)

            def passed(**details):
                results.append({"check": current, "status": "pass", **details})
                (OUTPUT/"results.json").write_text(json.dumps({"checks": results}, indent=2)+"\n")
                print(f"mdi: {current}: pass", flush=True)

            current = "mdi_without_window_manager"
            assert actor.state()["ui_mode"] == "mdi"
            paths = ROOT / "artifacts/stack/workspaces/users/alice" / actor.session["generation"]
            launcher = json.loads((paths / "launcher.json").read_text())
            assert launcher["window_manager"] == "none" and launcher["ui_mode"] == "mdi"
            assert "openbox" not in launcher["helpers"]
            processes = suite.command("exec", "swag-host", "ps", "-eo", "comm").stdout.splitlines()
            assert "openbox" not in [s.strip() for s in processes]
            owned = suite.processes()["alice"]
            assert len(owned) == 1 and owned[0]["uid"] == 1000 and owned[0]["pid"] == actor.session["pid"]
            assert len(windows()) == 1
            portfolio = active()
            passed(helpers=list(launcher["helpers"]), one_qt_process=True)

            current = "open_multiple_and_duplicate_entity_frames"
            actor.click("entity_positions")
            actor.wait(lambda s: s.get("entity") == "positions" and bool(s.get("items")))
            first = active()
            actor.click("entity_instruments")
            actor.wait(lambda s: s.get("entity") == "instruments" and bool(s.get("items")))
            actor.click("entity_positions")
            actor.wait(lambda s: s.get("entity") == "positions")
            before = set(windows())
            actor.click("mdi_open_button")
            actor.wait(lambda s: len(s.get("windows", [])) == len(before)+1 and s.get("items"))
            second = active()
            assert second != first and windows()[second]["entity"] == "positions"
            identifiers = [w["window_id"] for w in actor.state()["windows"]]
            assert len(set(identifiers)) == len(identifiers) >= 4
            passed(frame_count=len(windows()), duplicate_entity="positions")

            current = "independent_unsaved_forms"
            actor.edit("field_quantity", "123.456789")
            actor.wait(lambda s: s.get("dirty") and s.get("form_values", {}).get("quantity") == "123.456789")
            activate(first)
            assert not windows()[first]["dirty"]
            actor.edit("field_quantity", "999.000001")
            actor.wait(lambda s: s.get("dirty") and s.get("form_values", {}).get("quantity") == "999.000001")
            assert windows()[second]["form_values"]["quantity"] == "123.456789"
            assert all("*" in windows()[i]["title"] for i in (first, second))
            passed(independent_frames=[first, second], no_backend_write=True)

            current = "tile_cascade_move_resize_maximize_restore"
            actor.click("mdi_tile_button")
            actor.page.wait_for_timeout(300)
            tiled = {i: w["geometry"]["window"] for i, w in windows().items()}
            actor.screenshot("tiled")
            vx, vy, vw, vh = actor.state()["geometry"]["mdi_viewport"]
            for identifier, (x, y, w, h) in tiled.items():
                assert x >= vx-1 and y >= vy-1 and x+w <= vx+vw+1 and y+h <= vy+vh+1, (identifier, tiled, [vx, vy, vw, vh])
                child = windows()[identifier]["geometry"]
                _, gy, _, gh = child["grid_viewport"]
                _, ry, _, rh = child["grid_row_0"]
                assert rh >= 20 and ry >= gy and ry+rh <= gy+gh, child
            rectangles = list(tiled.values())
            for index, (x, y, w, h) in enumerate(rectangles):
                for ox, oy, ow, oh in rectangles[index+1:]:
                    assert min(x+w, ox+ow) <= max(x, ox) or min(y+h, oy+oh) <= max(y, oy), tiled
            actor.click("mdi_cascade_button")
            actor.wait(lambda s: any(w["geometry"]["window"] != tiled[w["window_id"]] for w in s["windows"]))
            identifier = active()
            before = windows()[identifier]["geometry"]["window"]
            drag(actor.state()["geometry"]["window_title_"+identifier], (25, 20))
            actor.wait(lambda s: next(w for w in s["windows"] if w["window_id"] == identifier)["geometry"]["window"][:2] != before[:2])
            before = windows()[identifier]["geometry"]["window"]
            # Resize using the Qt child frame's lower-right border.
            x, y, width, height = before
            drag([x+width-4, y+height-4, 2, 2], (-55, -45))
            actor.wait(lambda s: next(w for w in s["windows"] if w["window_id"] == identifier)["geometry"]["window"][2:] != before[2:])
            actor.click("mdi_maximize_button")
            actor.wait(lambda s: next(w for w in s["windows"] if w["window_id"] == identifier)["maximized"])
            actor.click("mdi_maximize_button")
            actor.wait(lambda s: not next(w for w in s["windows"] if w["window_id"] == identifier)["maximized"])
            actor.screenshot("cascade")
            passed(native_child_operations=True)

            current = "child_close_cancel_then_discard"
            activate(first)
            actor.click("mdi_maximize_button")
            actor.wait(lambda s: next(w for w in s["windows"] if w["window_id"] == first)["maximized"])
            before = set(windows())
            actor.click("field_quantity")
            actor.page.keyboard.press("Control+w")
            actor.wait(lambda s: "modal" in s["geometry"])
            actor.page.keyboard.press("Escape")
            actor.wait(lambda s: "modal" not in s["geometry"])
            assert set(windows()) == before and windows()[first]["dirty"]
            actor.page.keyboard.press("Control+a")
            actor.page.keyboard.type("998.000002")
            actor.wait(lambda s: s.get("form_values", {}).get("quantity") == "998.000002")
            actor.click("mdi_close_button")
            actor.click("modal_button_8388608")
            actor.wait(lambda s: first not in {w["window_id"] for w in s["windows"]})
            assert windows()[second]["form_values"]["quantity"] == "123.456789"
            passed(cancel_preserved_frame=True, keyboard_focus_restored=True, discard_closed_only_selected_frame=True)

            current = "quit_cancel_preserves_every_frame"
            before = set(windows())
            actor.click("quit_button")
            actor.wait(lambda s: "modal" in s["geometry"])
            actor.page.keyboard.press("Escape")
            actor.wait(lambda s: "modal" not in s["geometry"])
            assert set(windows()) == before and windows()[second]["dirty"]
            passed(frame_count=len(before))

            current = "popup_focus_and_clipboard_without_window_manager"
            activate(portfolio)
            actor.click("mdi_maximize_button")
            actor.wait(lambda s: next(w for w in s["windows"] if w["window_id"] == portfolio)["maximized"])
            actor.click("field_base_currency")
            actor.page.keyboard.press("ArrowDown")
            actor.page.keyboard.press("Enter")
            actor.wait(lambda s: s.get("dirty"))
            actor.click("form_cancel_button")
            actor.open_clipboard()
            actor.page.locator("#clipboard").fill("MDI clipboard inbound")
            actor.page.locator("#paste").click()
            actor.click("draft")
            actor.page.keyboard.press("Control+a")
            actor.page.keyboard.press("Control+v")
            actor.wait(lambda s: s.get("draft") == "MDI clipboard inbound")
            actor.edit("draft", "MDI clipboard outbound")
            actor.page.keyboard.press("Control+a")
            actor.page.keyboard.press("Control+c")
            actor.page.wait_for_function("document.querySelector('#clipboard').value === 'MDI clipboard outbound'")
            passed(combo_keyboard=True, clipboard_both_directions=True)

            current = "reconnect_preserves_all_frame_state"
            before = windows()
            identity = (actor.session["pid"], actor.session["generation"])
            actor.page.reload(wait_until="domcontentloaded")
            actor.page.locator("#status").filter(has_text="Connected").wait_for(timeout=20000)
            actor.session = actor.api()["body"]
            assert identity == (actor.session["pid"], actor.session["generation"])
            assert set(windows()) == set(before)
            for identifier in before:
                assert windows()[identifier]["form_values"] == before[identifier]["form_values"]
                assert windows()[identifier]["geometry"]["window"] == before[identifier]["geometry"]["window"]
            assert actor.state()["draft"] == "MDI clipboard outbound"
            actor.screenshot("reconnected")
            passed(same_process=True, same_frame_ids_and_layout=True, unsaved_forms_preserved=True)

            current = "last_child_close_keeps_shell_alive"
            # Explicitly discard the remaining dirty child before closing clean frames.
            activate(second)
            actor.click("mdi_close_button"); actor.click("modal_button_8388608")
            actor.wait(lambda s: second not in {w["window_id"] for w in s["windows"]})
            while windows():
                count = len(windows())
                actor.click("mdi_close_button")
                actor.wait(lambda s: len(s["windows"]) == count-1)
            assert actor.api()["body"]["state"] == "running"
            actor.click("entity_portfolios")
            actor.wait(lambda s: len(s["windows"]) == 1 and bool(s.get("items")))
            passed(empty_shell_running=True, entity_can_be_reopened=True)

            current = "quit_ends_workspace"
            actor.click("quit_button")
            workspace.wait_for(lambda: actor.api()["body"]["state"] == "ended")
            passed()
        except Exception as error:
            print(f"mdi: {current}: fail: {error}", flush=True)
            results.append({"check": current, "status": "fail", "error": str(error)})
            actor.page.screenshot(path=str(OUTPUT / "failure.png"))
            (OUTPUT / "failure-state.json").write_text(json.dumps(actor.state(), indent=2))
            raise
        finally:
            browser.close()
            (OUTPUT / "results.json").write_text(json.dumps({"checks": results}, indent=2)+"\n")


if __name__ == "__main__":
    main()
