#!/usr/bin/env python3
"""Check browser clipboard rejection and recovery on isolated live Qt fixtures.

The caller starts the authenticated workspace and probe fixtures. This script
uses installed Chrome and real browser/noVNC input; it does not start servers.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from playwright.sync_api import sync_playwright
import workspace


MIB = 1024 * 1024


def wait_for_state(path: Path, expected: str, timeout: float = 12) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if last and last.get('clipboard_text') == expected:
            return last
        time.sleep(.08)
    raise AssertionError(f'Qt clipboard did not become {expected!r}; last state: {last}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace-origin', default='http://localhost:6081')
    parser.add_argument('--probe-url', required=True)
    parser.add_argument('--probe-state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    workspace.OUTPUT = output
    results = []
    current = 'start'

    def passed(**detail):
        results.append({'check': current, 'status': 'pass', **detail})
        (output / 'results.json').write_text(json.dumps({'checks': results}, indent=2) + '\n')
        print(f'clipboard preflight: {current}: pass', flush=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=True)
        actor = workspace.Actor(browser, 'alice', args.workspace_origin)
        probe = browser.new_context().new_page()
        try:
            current = 'workspace_connected'
            actor.login()
            if actor.session['state'] != 'running':
                actor.page.locator('#start').click()
                workspace.wait_for(lambda: actor.api()['body']['state'] == 'running')
            actor.connected()
            identity = (actor.api()['body']['pid'], actor.api()['body']['generation'])
            passed(identity=list(identity))

            def workspace_send(value: str) -> None:
                actor.open_clipboard()
                actor.page.locator('#clipboard').fill(value)
                actor.page.locator('#paste').click()

            def paste_into_draft(expected: str) -> None:
                actor.click('draft')
                actor.page.keyboard.press('Control+a')
                actor.page.keyboard.press('Backspace')
                actor.page.keyboard.press('Control+v')
                actor.wait(lambda state: state.get('draft') == expected)

            current = 'workspace_rejection_preserves_viewer_and_clipboard'
            workspace_send('preflight workspace seed')
            paste_into_draft('preflight workspace seed')
            oversized = 'A' * MIB
            workspace_send(oversized)
            assert actor.page.locator('#clipboard-error').is_visible()
            assert 'too large' in actor.page.locator('#clipboard-error').inner_text()
            assert actor.page.locator('#clipboard').input_value() == oversized
            assert actor.page.locator('#clipboard').get_attribute('aria-invalid') == 'true'
            assert actor.page.evaluate("document.activeElement.id") == 'clipboard'
            assert actor.page.locator('#clipboard-panel').is_visible()
            assert actor.page.locator('#status').inner_text() == 'Connected'
            session = actor.api()['body']
            assert (session['pid'], session['generation']) == identity
            actor.page.locator('#clipboard-close').click()
            paste_into_draft('preflight workspace seed')
            passed(no_disconnect=True, same_process=True, old_clipboard_usable=True)

            current = 'workspace_valid_after_rejection_and_empty_clear'
            workspace_send('preflight workspace recovery €')
            assert actor.page.locator('#clipboard-error').is_hidden()
            paste_into_draft('preflight workspace recovery €')
            workspace_send('')
            paste_into_draft('')
            actor.page.wait_for_timeout(300)
            actor.page.keyboard.press('Control+v')
            actor.page.wait_for_timeout(300)
            assert actor.state().get('draft') == ''
            passed()

            current = 'probe_connected'
            probe.goto(args.probe_url, wait_until='domcontentloaded')
            probe.locator('#status').filter(has_text='Connected').wait_for()
            passed()

            def probe_send(value: str) -> None:
                probe.locator('#clipboard-input').fill(value)
                probe.locator('#send-clipboard').click()

            current = 'probe_rejection_preserves_viewer_and_qt_clipboard'
            probe_send('preflight probe seed')
            wait_for_state(args.probe_state, 'preflight probe seed')
            # 1,048,064 input bytes expand to 1,048,576 after LF->CRLF.
            # Keep line count bounded so this tests wire normalization rather
            # than Chrome's layout of half a million empty textarea lines.
            newline_heavy = ('A' * 2046 + '\n') * 512
            probe_send(newline_heavy)
            assert probe.locator('#clipboard-error').is_visible()
            assert 'too large' in probe.locator('#clipboard-error').inner_text()
            assert probe.locator('#clipboard-input').input_value() == newline_heavy
            assert probe.locator('#clipboard-input').get_attribute('aria-invalid') == 'true'
            assert probe.evaluate("document.activeElement.id") == 'clipboard-input'
            assert probe.locator('#status').inner_text() == 'Connected'
            assert json.loads(args.probe_state.read_text())['clipboard_text'] == 'preflight probe seed'
            passed(no_disconnect=True, qt_clipboard_unchanged=True)

            current = 'probe_valid_after_rejection_and_empty_clear'
            probe_send('preflight probe recovery €')
            wait_for_state(args.probe_state, 'preflight probe recovery €')
            assert probe.locator('#clipboard-error').is_hidden()
            probe_send('')
            wait_for_state(args.probe_state, '')
            assert probe.locator('#status').inner_text() == 'Connected'
            passed()
        except Exception as error:
            results.append({'check': current, 'status': 'fail', 'error': str(error)})
            (output / 'results.json').write_text(json.dumps({'checks': results}, indent=2) + '\n')
            for name, page in [('workspace', actor.page), ('probe', probe)]:
                try:
                    page.screenshot(path=str(output / f'{name}-failure.png'))
                except Exception:
                    pass
            raise
        finally:
            browser.close()


if __name__ == '__main__':
    main()
