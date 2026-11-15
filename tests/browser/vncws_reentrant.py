#!/usr/bin/env python3
"""Real noVNC input and canvas pixels for the paint-time modal-dialog fixture.

Run against an already started fixture and probe.html. Observation JSON is read
only; all interactions travel through browser mouse input. An unpatched plugin
should fail at dialog visibility with a bounded timeout.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright

PARENT_COLORS = [(36, 90, 156), (38, 142, 82), (224, 178, 48), (128, 80, 180)]
DIALOG_COLOR = (210, 90, 54)
BAND_COLOR = (40, 120, 220)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--state-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=8)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []

    def state():
        try:
            return json.loads(args.state_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1040})
        page.set_default_timeout(int(args.timeout * 1000))
        canvas = page.locator('#screen canvas')

        def wait(predicate, description):
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                current = state()
                if current and predicate(current):
                    return current
                page.wait_for_timeout(40)
            raise AssertionError(f'{description}: timed out; state={state()}')

        def click(control):
            rect = state()['controls'][control]
            bounds = canvas.bounding_box()
            assert bounds, 'canvas is not visible'
            size = canvas.evaluate('(c) => [c.width, c.height]')
            page.mouse.click(bounds['x'] + (rect['x'] + rect['width']/2) * bounds['width']/size[0],
                             bounds['y'] + (rect['y'] + rect['height']/2) * bounds['height']/size[1])

        def pixels(sample, expected):
            rect = state()['samples'][sample]
            def match(_):
                actual = canvas.evaluate("""(c,r) => {
                    const d=c.getContext('2d').getImageData(r.x,r.y,r.width,r.height).data;
                    return Array.from(d);
                }""", rect)
                rgba = [*expected, 255]
                return all(actual[i:i+4] == rgba for i in range(0, len(actual), 4))
            wait(match, f'{sample} pixels != {expected}')

        def table_hash():
            return canvas.evaluate("""async (c,r) => {
                const pixels=c.getContext('2d').getImageData(r.x,r.y,r.width,r.height).data;
                const digest=await crypto.subtle.digest('SHA-256', pixels);
                return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2,'0')).join('');
            }""", state()['geometry']['table'])

        def record(name, **detail):
            results.append({'check': name, 'status': 'pass', **detail})
            print(name + ': pass', flush=True)

        try:
            page.goto(args.url, wait_until='domcontentloaded')
            page.locator('#status').filter(has_text='Connected').wait_for()
            canvas.wait_for()
            initial = wait(lambda s: s.get('ready'), 'initial paint')
            assert not initial['alpha_dialog'], 'this gate expects the opaque dialog fixture'
            pid = initial['pid']
            pixels('parent', PARENT_COLORS[initial['parent_revision'] % 4])
            record('initial_pixels', pid=pid)
            for cycle in range(2):
                before = state()
                reference_table = table_hash()
                click('arm')
                active = wait(lambda s: s['modal_active'] and
                              s['modal_entered'] == before['modal_entered'] + 1,
                              'modal entry')
                assert active['modal_trigger_paint_depth'] > 0
                assert active['modal_trigger_thread_matches_gui']
                pixels('dialog_body', DIALOG_COLOR)
                pixels('stacking', DIALOG_COLOR)
                pixels('alpha', BAND_COLOR)
                heartbeat = state()['heartbeat']
                wait(lambda s: s['heartbeat'] > heartbeat, 'nested event loop heartbeat')
                canvas.screenshot(path=str(args.output/f'modal-{cycle}.png'))
                record('modal_visible_and_alive', cycle=cycle)
                click('dismiss')
                after = wait(lambda s: not s['modal_active'] and s['table_paint_depth'] == 0
                             and s['modal_exited'] == before['modal_exited'] + 1,
                             'modal dismissal and original paint completion')
                assert after['pid'] == pid
                pixels('parent', PARENT_COLORS[after['parent_revision'] % 4])
                pixels('stacking', PARENT_COLORS[after['parent_revision'] % 4])
                wait(lambda s: table_hash() == reference_table, 'table pixels restored')
                record('parent_paint_resumed', cycle=cycle, table_sha256=reference_table)
                click('follow_up')
                after = wait(lambda s: s['follow_up_count'] == before['follow_up_count'] + 1,
                             'follow-up input')
                pixels('parent', PARENT_COLORS[after['parent_revision'] % 4])
                record('continued_input_and_redraw', cycle=cycle)
            canvas.screenshot(path=str(args.output/'final.png'))
        except Exception as error:
            results.append({'check': 'browser_reentrancy', 'status': 'fail', 'error': str(error)})
            print(f'browser_reentrancy: fail: {error}', flush=True)
            try:
                page.screenshot(path=str(args.output/'failure.png'))
            except Exception:
                pass
        finally:
            (args.output/'state.json').write_text(json.dumps(state(), indent=2)+'\n')
            (args.output/'results.json').write_text(json.dumps(results, indent=2)+'\n')
            browser.close()
    return int(any(r['status'] != 'pass' for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
