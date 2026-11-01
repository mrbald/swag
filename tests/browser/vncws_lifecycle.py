#!/usr/bin/env python3
"""Exercise viewer replacement and abrupt disconnect using real noVNC input."""
import argparse
import json
from pathlib import Path
import time

from playwright.sync_api import sync_playwright
from smoke import Probe, ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', default='podman')
    parser.add_argument('--port', type=int, default=6104)
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts/qt-ws-hardening/lifecycle')
    args = parser.parse_args()
    output = args.output.resolve()
    probe = Probe(args.engine, 'qt-ws', output, args.port, 'websocket')
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        contexts = []
        def context():
            c = browser.new_context(viewport={'width':1440, 'height':1040}, device_scale_factor=1)
            contexts.append(c)
            return c
        try:
            initial = probe.start()
            identity = (initial['pid'], initial['generation'])
            probe.connect(context())

            def takeover():
                probe.replace_text('Before')
                probe.page.keyboard.down('Control')
                probe.wait(lambda s: s['keyboard_modifiers'] != 0)
                previous = probe.page
                # A different browser context connects to the existing Qt process.
                probe.connect(context())
                previous.wait_for_function("document.querySelector('#status').textContent !== 'Connected'")
                probe.wait(lambda s: s['keyboard_modifiers'] == 0 and s['mouse_buttons'] == 0)
                probe.click('text_edit')
                probe.page.keyboard.press('End')
                probe.page.keyboard.type('after')
                state = probe.wait(lambda s: s['editor_text'] == 'Beforeafter')
                assert (state['pid'], state['generation']) == identity
                probe.screenshot('takeover-input')
                return {'same_process':True, 'old_viewer_disconnected':True,
                        'input_without_modifier_reset':state['editor_text']}
            probe.check('takeover_releases_held_control', takeover)

            def disconnect():
                probe.replace_text('Disconnect')
                probe.page.keyboard.down('Control')
                probe.wait(lambda s: s['keyboard_modifiers'] != 0)
                probe.page.close()
                probe.wait(lambda s: s['keyboard_modifiers'] == 0 and s['mouse_buttons'] == 0)
                probe.connect(context())
                probe.click('text_edit')
                probe.page.keyboard.press('End')
                probe.page.keyboard.type('again')
                state = probe.wait(lambda s: s['editor_text'] == 'Disconnectagain')
                assert (state['pid'], state['generation']) == identity
                probe.screenshot('disconnect-input')
                return {'same_process':True, 'text':state['editor_text']}
            probe.check('disconnect_releases_held_control', disconnect)

            def drag_disconnect():
                probe.click('grid_viewport')
                rect = probe.clip('grid_viewport')
                probe.page.mouse.move(rect['x']+20, rect['y']+20)
                probe.page.mouse.down()
                probe.page.mouse.move(rect['x']+200, rect['y']+100)
                probe.wait(lambda s: s['mouse_buttons'] != 0)
                probe.page.close()
                probe.wait(lambda s: s['keyboard_modifiers'] == 0 and s['mouse_buttons'] == 0)
                probe.connect(context())
                probe.replace_text('AfterDrag')
                probe.click('dialog_button')
                probe.wait(lambda s: s['dialog_open'])
                probe.click('dialog_close_button')
                state = probe.wait(lambda s: not s['dialog_open'])
                assert (state['pid'], state['generation']) == identity
                return {'input_and_dialog_work':True}
            probe.check('disconnect_during_drag_recovers', drag_disconnect)
        except Exception as error:
            probe.results.append({'check':'startup_or_harness', 'status':'fail', 'error':repr(error)})
        finally:
            probe.save()
            for c in contexts:
                c.close()
            browser.close()
            probe.cleanup()
    print(f'Evidence: {output}')
    return int(any(r['status'] != 'pass' for r in probe.results))


if __name__ == '__main__':
    raise SystemExit(main())
