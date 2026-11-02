#!/usr/bin/env python3
"""Exercise real browser/noVNC input against both launch modes; retain failures."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import uuid

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def run(*args: str, timeout: float = 30, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=check, timeout=timeout)


class Probe:
    def __init__(self, engine: str, backend: str, output: Path, port: int, transport: str = "tcp"):
        self.engine, self.backend, self.output, self.port = engine, backend, output, port
        self.transport = transport
        self.name = f"swag-check-{backend}-{uuid.uuid4().hex[:8]}"
        self.results: list[dict] = []
        self.page = None
        self.started = False

    def state(self) -> dict:
        try:
            return json.loads((self.output / "state.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def wait(self, predicate, timeout: float = 4) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if state and predicate(state):
                return state
            time.sleep(0.05)
        raise AssertionError(f"state condition timed out; last state: {self.state()}")

    def save(self):
        (self.output / "results.json").write_text(json.dumps({
            "backend": self.backend, "checks": self.results,
            "scope": "functional rendering only; visual screenshots require review; no WAN/performance claim",
        }, indent=2, ensure_ascii=False) + "\n")

    def check(self, name: str, function):
        try:
            detail = function() or {}
            result = {"check": name, "status": "pass", **detail}
        except Exception as error:
            result = {"check": name, "status": "fail", "error": str(error)}
            (self.output / f"{name}-state.json").write_text(json.dumps(self.state(), indent=2) + "\n")
            try:
                self.screenshot(f"{name}-failed")
                self.page.keyboard.press("Escape")
            except Exception:
                pass
        self.results.append(result)
        self.save()
        print(f"{self.backend}: {name}: {result['status']}", flush=True)

    def canvas_rect(self) -> dict:
        rect = self.page.locator("#screen canvas").bounding_box()
        if not rect:
            raise AssertionError("no visible noVNC canvas")
        return rect

    def clip(self, key: str) -> dict:
        x, y, width, height = self.state()["geometry"][key]
        canvas = self.canvas_rect()
        sx, sy = canvas["width"] / 1280, canvas["height"] / 800
        return {"x": canvas["x"] + x * sx, "y": canvas["y"] + y * sy,
                "width": width * sx, "height": height * sy}

    def click(self, key: str):
        rect = self.clip(key)
        self.page.mouse.click(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)

    def screenshot(self, name: str, key: str | None = None) -> str:
        # Let the RFB update arrive after the application-side semantic assertion.
        self.page.wait_for_timeout(250)
        path = self.output / f"{name}.png"
        data = self.page.screenshot(path=str(path), clip=self.clip(key) if key else self.canvas_rect())
        return hashlib.sha256(data).hexdigest()

    def replace_text(self, text: str):
        self.click("text_edit")
        self.page.keyboard.press("Control+a")
        self.page.keyboard.type(text)
        return self.wait(lambda state: state["editor_text"] == text)

    def start(self):
        self.output.mkdir(parents=True, exist_ok=True)
        # The name is ours even if a detached launch succeeds but its client times out.
        self.started = True
        result = run(sys.executable, "tools/probe.py", "run", "--engine", self.engine,
                     "--backend", self.backend, "--name", self.name, "--port", str(self.port),
                     "--transport", self.transport, "--output", str(self.output), timeout=60)
        (self.output / "launch.json").write_text(result.stdout)
        manifest = json.loads(result.stdout)
        self.url = manifest["url"]
        def ready(state):
            try:
                backend = json.loads((self.output / "backend.json").read_text())
                return (state.get("ready") and state.get("generation") == manifest["generation"]
                        and backend.get("ready") and backend.get("generation") == manifest["generation"])
            except (FileNotFoundError, json.JSONDecodeError):
                return False
        return self.wait(ready, timeout=25)

    def connect(self, context):
        self.page = context.new_page()
        self.page.on("pageerror", lambda error: (self.output / "browser-errors.log").open("a").write(str(error) + "\n"))
        self.page.goto(self.url, wait_until="networkidle")
        self.page.locator("#status").filter(has_text=re.compile(r"^Connected$")).wait_for(timeout=15000)
        self.page.locator("#screen canvas").wait_for()
        self.screenshot("initial")

    def interactions(self):
        def transport():
            backend = json.loads((self.output / "backend.json").read_text())
            if self.backend == "qt-ws":
                assert backend["rfbTransport"] == "websocket"
                assert set(backend["helpers"]) == {"http"}, backend["helpers"]
                processes = run(self.engine, "exec", self.name, "ps", "-eo", "pid,comm,args").stdout
                assert not any(name in processes for name in ("Xvnc", "websockify", "vncconfig", "openbox")), processes
                listeners = run(self.engine, "exec", self.name, "ss", "-ltnp").stdout
                (self.output / "processes.txt").write_text(processes)
                (self.output / "listeners.txt").write_text(listeners)
                assert any(":5900" in line and f'pid={backend["appPid"]},' in line
                           for line in listeners.splitlines()), listeners
                return {"transport": "websocket", "qt_owns_listener": True,
                        "gui_helpers": [], "static_http_helper": True}
            assert backend["rfbTransport"] == self.transport
            if self.transport == "unix":
                path = backend["rfbEndpoint"]
                mode = run(self.engine, "exec", self.name, "stat", "--format=%a", path).stdout.strip()
                assert mode == "600", mode
                listeners = run(self.engine, "exec", self.name, "ss", "-ltn").stdout
                assert ":5900" not in listeners, listeners
                return {"transport": "unix", "socket_mode": mode, "rfb_tcp_disabled": True}
            return {"transport": "tcp"}
        self.check("transport", transport)

        def ascii_edit():
            before = self.screenshot("ascii-before", "text_edit")
            self.replace_text("BrowserDraft42")
            after = self.screenshot("ascii-after", "text_edit")
            assert before != after, "editor canvas did not change"
            return {"value": self.state()["editor_text"], "region_changed": True}
        self.check("ascii_edit", ascii_edit)

        def wheel():
            self.click("grid_viewport")
            self.page.keyboard.press("Control+Home")
            initial = self.wait(lambda state: state["scroll_row"] == 0)
            before = self.screenshot("wheel-before", "grid_viewport")
            rect = self.clip("grid_viewport")
            self.page.mouse.move(rect["x"] + 200, rect["y"] + 100)
            self.page.mouse.wheel(0, 600)
            state = self.wait(lambda value: value["scroll_row"] > initial["scroll_row"])
            after = self.screenshot("wheel-after", "grid_viewport")
            assert before != after, "grid canvas did not scroll"
            return {"scroll_row": state["scroll_row"], "wheel_events": state["wheel_events"]}
        self.check("wheel_scroll", wheel)

        def horizontal_wheel():
            self.click("grid_viewport")
            try:
                self.page.keyboard.press("Control+Home")
                self.wait(lambda state: state["scroll_row"] == 0 and
                          state["current_row"] == 0 and state["current_column"] == 0)
                rect = self.clip("grid_viewport")
                self.page.mouse.move(rect["x"] + 200, rect["y"] + 100)
                before = self.screenshot("horizontal-wheel-before", "grid_viewport")
                initial = self.state()
                viewport = initial["geometry"]["grid_viewport"]
                start_x = initial["geometry"]["grid_current_cell"][0] - viewport[0]

                def cell_x(state):
                    geometry = state["geometry"]
                    if (geometry["grid_viewport"] != viewport or
                            (state["current_row"], state["current_column"]) != (0, 0)):
                        return None
                    return geometry["grid_current_cell"][0] - viewport[0]

                self.page.mouse.wheel(60, 0)
                right = self.wait(lambda state: state["wheel_events"] > initial["wheel_events"] and
                                  cell_x(state) is not None and cell_x(state) < start_x)
                right_x = cell_x(right)
                after = self.screenshot("horizontal-wheel-right", "grid_viewport")
                assert before != after, "grid canvas did not move horizontally"

                self.page.mouse.wheel(-60, 0)
                restored = self.wait(lambda state: state["wheel_events"] > right["wheel_events"] and
                                     cell_x(state) is not None and abs(cell_x(state) - start_x) <= 1)
                self.screenshot("horizontal-wheel-restored", "grid_viewport")
                return {"cell_x_before": start_x, "cell_x_after_right": right_x,
                        "cell_x_after_left": cell_x(restored),
                        "visual_evidence": ["horizontal-wheel-before.png", "horizontal-wheel-right.png",
                                            "horizontal-wheel-restored.png"]}
            finally:
                try:
                    self.page.keyboard.press("Control+End")
                finally:
                    self.page.keyboard.press("Control+Home")
        self.check("horizontal_wheel_scroll", horizontal_wheel)

        def edit_grid():
            self.click("grid_viewport")
            self.page.keyboard.press("Control+Home")
            self.wait(lambda state: state["scroll_row"] == 0)
            self.page.keyboard.press("ArrowRight")
            self.page.keyboard.press("F2")
            self.page.keyboard.press("Control+a")
            self.page.keyboard.type("GridEdited42")
            self.page.keyboard.press("Enter")
            self.wait(lambda state: state["cell_0_1"] == "GridEdited42")
            self.screenshot("grid-committed", "grid_cell_0_1")
            current = self.state()
            identity = (current["current_row"], current["current_column"], current["current_value"])
            self.page.keyboard.press("F2")
            self.page.keyboard.press("Control+a")
            self.page.keyboard.type("CancelledValue")
            self.wait(lambda state: state["grid_editing"])
            self.page.keyboard.press("Escape")
            self.wait(lambda state: not state["grid_editing"] and
                      (state["current_row"], state["current_column"], state["current_value"]) == identity)
            self.page.keyboard.press("ArrowDown")
            self.wait(lambda state: state["current_row"] == identity[0] + 1)
            return {"committed": "GridEdited42", "cancel_preserved": True}
        self.check("grid_edit_cancel_navigation", edit_grid)

        def selection_color():
            self.screenshot("selection-color", "grid_current_cell")
            rect = self.state()["geometry"]["grid_current_cell"]
            # Read rendered canvas pixels only. The probe stylesheet specifies
            # #195fc2; RFB must not exchange the red and blue channels. Tiger's
            # Tight JPEG path is lossy, while qt-ws must retain exact RGB.
            tolerance = 8 if self.backend == "tiger" else 0
            correct = self.page.locator("#screen canvas").evaluate("""
                (canvas, {rect, tolerance}) => {
                    const pixels = canvas.getContext('2d').getImageData(...rect).data;
                    let count = 0;
                    for (let i = 0; i < pixels.length; i += 4)
                        if (Math.abs(pixels[i]-25) <= tolerance &&
                            Math.abs(pixels[i+1]-95) <= tolerance &&
                            Math.abs(pixels[i+2]-194) <= tolerance &&
                            pixels[i+2] > pixels[i]+80)
                            count++;
                    return count;
                }
            """, {"rect": rect, "tolerance": tolerance})
            assert correct > 100, f"expected blue selection pixels, found {correct}"
            return {"selection_rgb": [25, 95, 194], "rgb_tolerance": tolerance,
                    "matching_pixels": correct}
        self.check("selection_color", selection_color)

        def unicode_input():
            self.click("text_edit")
            self.page.keyboard.press("Control+a")
            # Playwright's keyboard.press only maps a fixed key repertoire. CDP
            # dispatches trusted browser keyboard events for non-US layouts;
            # noVNC still performs all RFB key translation.
            session = self.page.context.new_cdp_session(self.page)
            for character in "é€Ж":
                session.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": character,
                             "code": "KeyE", "text": character, "unmodifiedText": character})
                session.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": character, "code": "KeyE"})
            session.detach()
            self.wait(lambda state: state["editor_text"] == "é€Ж")
            self.screenshot("unicode", "text_edit")
            return {"value": self.state()["editor_text"]}
        self.check("unicode_keyboard", unicode_input)

        def meta_modifier():
            self.click("text_edit")
            platform = self.page.evaluate("navigator.platform")
            # Pinned noVNC remaps macOS left Command to Alt and right
            # Command to Super_L. Other desktop platforms send Super_L/R.
            mappings = [("MetaLeft", 0x08000000 if "Mac" in platform else 0x10000000),
                        ("MetaRight", 0x10000000)]
            for key, expected in mappings:
                self.page.keyboard.down(key)
                try:
                    self.page.keyboard.press("k")
                    expected_key = "Alt+K" if expected == 0x08000000 else "Meta+K"
                    self.wait(lambda state: state["last_key"] == expected_key and
                              state["keyboard_modifiers"] == expected)
                finally:
                    self.page.keyboard.up(key)
                # xcb exposes the modifier mask attached to the last key event,
                # so check a subsequent ordinary key rather than idle polling.
                self.page.keyboard.press("j")
                self.wait(lambda state: state["keyboard_modifiers"] == 0 and
                          state["last_key"] == "J")
            return {"platform": platform, "expected_modifier_mappings": mappings, "released_modifiers": 0,
                    "scope": "pinned noVNC platform mapping through installed Chrome; not Windows/RDP interception qualification"}
        self.check("meta_modifier_browser", meta_modifier)

        def clipboard_in():
            expected = "ClipboardFromBrowser42"
            self.page.locator("#clipboard-input").fill(expected)
            self.page.locator("#send-clipboard").click()
            self.wait(lambda state: state["clipboard_text"] == expected)
            self.click("paste_button")
            self.wait(lambda state: state["editor_text"] == expected)
            self.screenshot("clipboard-in", "text_edit")
            self.page.locator("#clipboard-input").fill("")
            self.page.locator("#send-clipboard").click()
            self.wait(lambda state: state["clipboard_text"] == "")
            return {"value": expected, "empty_clipboard_clears": True}
        self.check("clipboard_browser_to_qt", clipboard_in)

        def clipboard_out():
            expected = "ClipboardFromQt42"
            self.replace_text(expected)
            self.click("copy_button")
            self.page.wait_for_function("document.querySelector('#clipboard-output').value === 'ClipboardFromQt42'", timeout=4000)
            return {"value": self.page.locator("#clipboard-output").input_value()}
        self.check("clipboard_qt_to_browser", clipboard_out)

        def combo():
            self.click("combo")
            self.wait(lambda state: state["combo_popup_open"])
            self.screenshot("combo-popup")
            self.page.keyboard.press("ArrowDown")
            self.page.keyboard.press("Enter")
            self.wait(lambda state: state["combo_value"] == "Beta" and not state["combo_popup_open"])
            self.screenshot("combo-selected", "combo")
            return {"selected": "Beta", "visual_evidence": "combo-popup.png"}
        self.check("combo_popup", combo)

        def menu():
            self.click("menu_action")
            self.wait(lambda state: state["menu_open"])
            self.screenshot("menu-popup")
            self.page.keyboard.press("Escape")
            self.wait(lambda state: not state["menu_open"])
            return {"visual_evidence": "menu-popup.png"}
        self.check("menu_popup", menu)

        def dialog():
            self.click("dialog_button")
            self.wait(lambda state: state["dialog_open"])
            assert self.state()["active_modal"], "dialog did not become modal"
            self.click("dialog_editor")
            self.page.keyboard.press("Control+a")
            self.page.keyboard.type("ModalInput42")
            self.wait(lambda state: state["dialog_text"] == "ModalInput42" and
                      state["focus_widget"] == "dialog_editor" and state["active_modal"])
            self.screenshot("dialog-open")
            self.click("dialog_close_button")
            self.wait(lambda state: not state["dialog_open"] and not state["active_modal"] and
                      state["focus_widget"] == "dialog_button")
            self.replace_text("AfterDialog42")
            self.screenshot("dialog-closed")
            return {"main_window_accepts_input": True, "visual_evidence": "dialog-open.png"}
        self.check("modal_dialog", dialog)

        def reconnect():
            expected = self.replace_text("UnsavedReconnect42")
            identity = (expected["pid"], expected["generation"])
            self.page.locator("#disconnect").click()
            self.page.wait_for_timeout(250)
            self.page.reload(wait_until="networkidle")
            self.page.locator("#status").filter(has_text=re.compile(r"^Connected$")).wait_for(timeout=10000)
            actual = self.state()
            assert (actual["pid"], actual["generation"]) == identity
            assert actual["editor_text"] == "UnsavedReconnect42"
            self.screenshot("reconnected", "text_edit")
            return {"same_process_and_generation": True, "unsaved_value": actual["editor_text"]}
        self.check("reconnect_preserves_process_and_draft", reconnect)

        def normal_exit():
            self.click("quit_button")
            result = run(self.engine, "wait", self.name, timeout=15)
            assert result.stdout.strip() == "0", result.stdout
            self.page.wait_for_function("document.querySelector('#status').textContent !== 'Connected'", timeout=5000)
            return {"exit_code": 0, "viewer_disconnected": True}
        self.check("normal_exit", normal_exit)

    def cleanup(self):
        if self.started:
            logs = run(self.engine, "logs", self.name, check=False)
            (self.output / "container.log").write_text(logs.stdout + logs.stderr)
            inspect = run(self.engine, "inspect", self.name, check=False)
            (self.output / "container-inspect.json").write_text(inspect.stdout)
            run(self.engine, "cp", f"{self.name}:/opt/swag/image-manifest.json",
                str(self.output / "image-manifest.json"), check=False)
            run(self.engine, "rm", "--force", self.name, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="podman")
    parser.add_argument("--backend", choices=["qt", "tiger", "qt-ws", "both"], default="both")
    parser.add_argument("--transport", choices=["tcp", "unix", "websocket"])
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / f"render-{time.strftime('%Y%m%d-%H%M%S')}")
    parser.add_argument("--port", type=int, default=6080)
    parser.add_argument("--browser-channel", default="chrome" if sys.platform == "darwin" else None)
    args = parser.parse_args()
    args.transport = args.transport or ("websocket" if args.backend == "qt-ws" else "tcp")
    if (args.backend == "qt-ws") != (args.transport == "websocket"):
        parser.error("qt-ws requires websocket transport, and only qt-ws supports it")
    if args.transport == "unix" and args.backend != "tiger":
        parser.error("Unix transport is supported by the selected TigerVNC backend only")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    all_results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=args.browser_channel, headless=True)
        for backend in (["qt", "tiger"] if args.backend == "both" else [args.backend]):
            probe = Probe(args.engine, backend, output / backend, args.port, args.transport)
            context = browser.new_context(viewport={"width": 1440, "height": 1040}, device_scale_factor=1)
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            try:
                probe.start()
                probe.connect(context)
                probe.interactions()
            except Exception as error:
                probe.results.append({"check": "startup_or_harness", "status": "fail", "error": str(error)})
                print(f"{backend}: startup_or_harness: fail: {error}", flush=True)
            finally:
                probe.save()
                context.tracing.stop(path=str(probe.output / "browser-trace.zip"))
                context.close()
                probe.cleanup()
            # A separate run tests abrupt Qt death without destroying the functional run's evidence.
            crash = Probe(args.engine, backend, output / f"{backend}-crash", args.port, args.transport)
            crash_context = browser.new_context(viewport={"width": 1440, "height": 1040}, device_scale_factor=1)
            try:
                state = crash.start()
                crash.connect(crash_context)
                run(args.engine, "exec", crash.name, "kill", "-KILL", str(state["pid"]))
                stopped = run(args.engine, "wait", crash.name, timeout=15)
                assert stopped.stdout.strip() == "137", stopped.stdout
                crash.page.wait_for_function("document.querySelector('#status').textContent !== 'Connected'", timeout=5000)
                probe.results.append({"check": "qt_sigkill_ends_session", "status": "pass", "exit_code": 137, "viewer_disconnected": True})
            except Exception as error:
                probe.results.append({"check": "qt_sigkill_ends_session", "status": "fail", "error": str(error)})
            finally:
                crash_context.close()
                crash.cleanup()
                probe.save()
            all_results.append({"backend": backend, "checks": probe.results})
        browser.close()
    (output / "results.json").write_text(json.dumps(all_results, indent=2) + "\n")
    print(f"Evidence: {output}")
    return int(any(check["status"] != "pass" for result in all_results for check in result["checks"]))


if __name__ == "__main__":
    raise SystemExit(main())
