#!/usr/bin/env python3
"""Real noVNC small/oversized/recovered Qt copies; no injected Qt actions."""
import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
import smoke
from everyday import FixtureActor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('qt-ws', 'qt-unix'), default='qt-ws')
    parser.add_argument('--engine', default='podman')
    parser.add_argument('--port', type=int, default=6290)
    parser.add_argument('--workspace-origin')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    probe = None
    current = 'connect'
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        try:
            if args.workspace_origin:
                actor = FixtureActor(browser, 'alice', args.workspace_origin)
                actor.login()
                actor.api('DELETE')
                actor.api('POST')
                actor.page.reload(wait_until='domcontentloaded')
                actor.connected()
            else:
                probe = smoke.Probe(args.engine, args.backend, output, args.port,
                                    'unix' if args.backend == 'qt-unix' else 'websocket')
                probe.start()
                probe.connect(browser.new_context(viewport={'width': 1440, 'height': 1040}))
                actor = probe
            page = actor.page
            received = page.locator('#clipboard' if args.workspace_origin else '#clipboard-output')
            error = page.locator('#clipboard-error')

            def wait(predicate):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    state = actor.state()
                    if state and predicate(state):
                        return state
                    page.wait_for_timeout(100)
                raise AssertionError('Qt state timeout: clipboard=%d, editor=%d' %
                                     (len(state.get('clipboard_text', '')),
                                      len(state.get('multiline_text', ''))))

            def passed(**details):
                results.append(dict(check=current, status='pass', **details))
                print(f'outbound clipboard: {current}: pass', flush=True)

            def click(name):
                if args.workspace_origin:
                    actor.click(name)
                else:
                    probe.click(name)
                page.wait_for_timeout(250)

            click('menu_action')
            click('interactions_menu_action')
            wait(lambda s: s.get('interactions_open'))
            # Seed below the wire limit, then enlarge it using genuine Qt
            # copy/paste. The oversized value never crosses inbound transport.
            current = 'small_copy'
            seed = ('x' * 99 + '\n') * 100
            if args.workspace_origin:
                actor.open_clipboard()
            page.locator('#clipboard' if args.workspace_origin else '#clipboard-input').fill(seed)
            page.locator('#paste' if args.workspace_origin else '#send-clipboard').click()
            wait(lambda s: len(s['clipboard_text']) == len(seed))
            click('multiline_editor')
            page.keyboard.press('Control+v')
            wait(lambda s: len(s['multiline_text']) == len(seed))
            if args.workspace_origin:
                actor.open_clipboard()
                received.fill('waiting')
                page.locator('#clipboard-close').click()
            click('multiline_editor')
            page.keyboard.press('Control+a')
            page.keyboard.press('Control+c')
            page.wait_for_function('(id) => document.querySelector(id).value.length === 10000',
                                   arg='#clipboard' if args.workspace_origin else '#clipboard-output')
            passed(bytes=len(seed))

            current = 'oversized_copy_is_unavailable'
            size = len(seed)
            while size < 1_048_576:
                page.keyboard.press('Control+a')
                page.keyboard.press('Control+c')
                wait(lambda s: len(s['clipboard_text']) == size)
                page.keyboard.press('Control+End')
                page.keyboard.press('Control+v')
                size *= 2
                wait(lambda s: len(s['multiline_text']) == size)
            page.keyboard.press('Control+a')
            page.keyboard.press('Control+c')
            wait(lambda s: len(s['clipboard_text']) == size)
            if args.workspace_origin:
                actor.open_clipboard()
            error.wait_for(state='visible', timeout=5000)
            assert received.input_value() == ''
            assert 'unavailable' in error.inner_text().lower()
            page.screenshot(path=str(output/'unavailable.png'))
            passed(bytes=size, stale_value_cleared=True)

            current = 'small_copy_recovers'
            click('multiline_editor')
            page.keyboard.press('Control+a')
            page.keyboard.type('recovered copy')
            page.keyboard.press('Control+a')
            page.keyboard.press('Control+c')
            page.wait_for_function('(id) => document.querySelector(id).value === "recovered copy"',
                                   arg='#clipboard' if args.workspace_origin else '#clipboard-output')
            assert error.is_hidden()
            wait(lambda s: s['clipboard_text'] == 'recovered copy')
            passed()
            page.screenshot(path=str(output/'recovered.png'))
        except Exception as exc:
            results.append(dict(check=current, status='fail', error=str(exc)[:2000]))
            if 'actor' in locals():
                actor.page.screenshot(path=str(output/'failure.png'))
                state = actor.state()
                # Keep failure evidence bounded; these fields may be megabytes.
                for name in ('clipboard_text', 'multiline_text'):
                    state[name+'_length'] = len(state.pop(name, ''))
                (output/'failure-state.json').write_text(json.dumps(state, indent=2)+'\n')
            raise RuntimeError(f'{current}: {str(exc)[:2000]}') from None
        finally:
            (output/'results.json').write_text(json.dumps({'checks': results}, indent=2)+'\n')
            browser.close()
            if probe and probe.started:
                (output/'container.log').write_text(smoke.run(args.engine, 'logs', probe.name, check=False).stdout)
                smoke.run(args.engine, 'rm', '-f', probe.name, check=False)


if __name__ == '__main__':
    main()
