#!/usr/bin/env python3
"""Real noVNC checks against the already running clean-runtime fixture."""
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright
import smoke


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:6297/probe.html?wsPort=6296')
    parser.add_argument('--output', type=Path, required=True,
                        help='The runtime container mounted observation directory')
    args = parser.parse_args()
    probe = smoke.Probe('podman', 'qt-ws', args.output.resolve(), 6297, 'websocket')
    probe.url = args.url
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        try:
            probe.connect(browser.new_context(viewport={'width': 1440, 'height': 1040}))
            page = probe.page

            def initial():
                state = probe.wait(lambda s: s.get('ready'))
                assert state['platform'] == 'vncws'
                assert state['editor_text'] == 'Draft: café 東京'
                assert probe.canvas_rect()['width'] == 1280
                return {'platform': state['platform'], 'pid': state['pid']}

            probe.check('first_frame', initial)
            def edit():
                probe.replace_text('runtime edit')

            probe.check('real_keyboard_edit', edit)

            def clipboard():
                text = 'Runtime € 東京'
                page.locator('#clipboard-input').fill(text)
                page.locator('#send-clipboard').click()
                probe.wait(lambda s: s['clipboard_text'] == text)
                probe.click('text_edit')
                page.keyboard.press('Control+a')
                page.keyboard.press('Control+v')
                probe.wait(lambda s: s['editor_text'] == text)
                page.keyboard.press('Control+a')
                page.keyboard.press('Control+c')
                page.wait_for_function('text => document.querySelector("#clipboard-output").value === text', arg=text)
                return {'text': text, 'both_directions': True}

            probe.check('clipboard_roundtrip', clipboard)

            def context_menu():
                probe.click('text_edit')
                page.keyboard.press('ContextMenu')
                state = probe.wait(lambda s: bool(s['popup_actions']))
                assert any('Copy' in action for action in state['popup_actions'])
                probe.screenshot('context-menu')
                page.keyboard.press('Escape')
                probe.wait(lambda s: not s['popup_actions'])
                probe.replace_text('after menu')
                return {'actions': state['popup_actions']}

            probe.check('context_menu_and_recovery', context_menu)
            probe.screenshot('final')
        finally:
            browser.close()
            probe.save()
    return int(any(row['status'] != 'pass' for row in probe.results))


if __name__ == '__main__':
    raise SystemExit(main())
