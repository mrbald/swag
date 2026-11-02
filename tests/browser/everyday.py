#!/usr/bin/env python3
"""Real noVNC keypad, item drag-and-drop and multiline clipboard checks.

--workspace-origin requires the probe executable installed as the workspace
application in a test image. It exercises the actual authenticated HTML shell.
Application observation supplies geometry/state only, never performs actions.
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright
import smoke
import workspace


class FixtureActor(workspace.Actor):
    def connected(self):
        self.page.locator('#status').filter(has_text='Connected').wait_for()
        self.page.locator('#screen canvas').wait_for()
        self.wait(lambda state: state.get('ready'))
        self.page.wait_for_timeout(350)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('qt-ws', 'tiger'), default='qt-ws')
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
                if actor.session['state'] != 'running':
                    actor.page.locator('#start').click()
                    workspace.wait_for(lambda: actor.api()['body']['state'] == 'running')
                actor.connected()
            else:
                probe = smoke.Probe(args.engine, args.backend, output, args.port,
                                    'unix' if args.backend == 'tiger' else 'websocket')
                probe.start()
                probe.connect(browser.new_context(viewport={'width': 1440, 'height': 1040}))
                actor = probe
            page = actor.page

            def save():
                (output/'results.json').write_text(json.dumps({'backend': args.backend,
                    'workspace_shell': bool(args.workspace_origin), 'checks': results}, indent=2)+'\n')

            def passed(**details):
                results.append({'check': current, 'status': 'pass', **details})
                save()
                print(f'everyday {args.backend}: {current}: pass', flush=True)

            def point(key, fy=.5):
                state = actor.wait(lambda s: key in s.get('geometry', {}))
                rectangle = state['geometry'][key]
                if args.workspace_origin:
                    return actor.canvas_point(rectangle, fy=fy)
                rect = actor.clip(key)
                return rect['x']+rect['width']/2, rect['y']+rect['height']*fy

            def click(key):
                page.mouse.click(*point(key))
                page.wait_for_timeout(160)

            current = 'open_interactions'
            click('menu_action')
            # QMenu::aboutToShow precedes its final placement. Let the passive
            # observation timer capture laid-out global action geometry.
            page.wait_for_timeout(300)
            click('interactions_menu_action')
            actor.wait(lambda s: s.get('interactions_open'))
            passed()

            if not args.workspace_origin:
                current = 'nonrepeating_shortcut_held_key'
                click('key_input')
                before = actor.state()
                page.keyboard.down('F6')
                try:
                    state = actor.wait(lambda s: s['f6_nonrepeat_actions'] == before['f6_nonrepeat_actions']+1)
                    repeat_start = state['raw_key_events']
                    page.keyboard.down('F6')
                    page.keyboard.down('F6')
                    state = actor.wait(lambda s: s['raw_key_events'] >= repeat_start+2)
                    assert state['f6_nonrepeat_actions'] == before['f6_nonrepeat_actions']+1, state
                    assert state['last_key_auto_repeat'] is True, state
                finally:
                    page.keyboard.up('F6')
                page.keyboard.press('F6')
                state = actor.wait(lambda s: s['f6_nonrepeat_actions'] == before['f6_nonrepeat_actions']+2)
                assert state['last_key_auto_repeat'] is False, state
                passed(held_key_actions=1, new_press_actions=1)

                current = 'repeating_shortcut_held_key'
                before = actor.state()
                page.keyboard.down('F7')
                try:
                    page.keyboard.down('F7')
                    page.keyboard.down('F7')
                    state = actor.wait(lambda s: s['f7_repeat_actions'] == before['f7_repeat_actions']+3)
                    assert state['last_key_auto_repeat'] is True, state
                finally:
                    page.keyboard.up('F7')
                passed(held_key_actions=3)

                current = 'ordinary_text_still_repeats'
                click('multiline_editor')
                page.keyboard.press('Control+a')
                page.keyboard.press('Backspace')
                page.keyboard.down('x')
                try:
                    page.keyboard.down('x')
                    page.keyboard.down('x')
                    state = actor.wait(lambda s: s['multiline_text'] == 'xxx')
                    assert state['last_key_auto_repeat'] is True, state
                finally:
                    page.keyboard.up('x')
                passed(text=state['multiline_text'])

                current = 'ordinary_return_action'
                click('key_input')
                before = actor.state()
                page.keyboard.press('Enter')
                state = actor.wait(lambda s: s['return_shortcut_actions'] == before['return_shortcut_actions']+1)
                assert state['enter_shortcut_actions'] == before['enter_shortcut_actions']
                passed(return_actions=state['return_shortcut_actions'])

                current = 'keypad_enter_action'
                before = actor.state()
                page.keyboard.press('NumpadEnter')
                state = actor.wait(lambda s: s['enter_shortcut_actions'] == before['enter_shortcut_actions']+1)
                assert state['return_shortcut_actions'] == before['return_shortcut_actions']
                passed(enter_actions=state['enter_shortcut_actions'])

                current = 'keypad_metadata_does_not_latch'
                click('multiline_editor')
                page.keyboard.press('NumpadEnter')
                state = actor.wait(lambda s: s['last_key_code'] == 0x01000005)
                assert state['last_key_modifiers'] & 0x20000000, state
                page.keyboard.press('j')
                state = actor.wait(lambda s: s['last_key_code'] == ord('J'))
                assert state['last_key_modifiers'] == 0, state
                passed(keypad_enter_code=0x01000005, next_key_modifiers=0)

                current = 'qt_item_drag_and_drop'
                assert actor.state()['reorder_order'] == ['Alpha', 'Beta', 'Gamma', 'Delta']
                source = point('reorder_item_0')
                target = point('reorder_item_2', .9)
                page.mouse.move(*source)
                page.mouse.down()
                page.mouse.move(source[0]+15, source[1]+5, steps=5)
                page.wait_for_timeout(200)
                page.mouse.move(*target, steps=20)
                page.wait_for_timeout(200)
                page.mouse.up()
                state = actor.wait(lambda s: s['reorder_order'] == ['Beta', 'Gamma', 'Alpha', 'Delta'])
                passed(order=state['reorder_order'])

            current = 'multiline_tabbed_clipboard_roundtrip'
            expected = 'row1\t100\nrow2\t200\n'
            if args.workspace_origin:
                actor.open_clipboard()
                control = page.locator('#clipboard')
                control.fill(expected)
                assert control.input_value() == expected
                page.locator('#paste').click()
            else:
                control = page.locator('#clipboard-input')
                control.fill(expected)
                page.locator('#send-clipboard').click()
            actor.wait(lambda s: s['clipboard_text'] == expected)
            click('multiline_editor')
            page.keyboard.press('Control+a')
            page.keyboard.press('Control+v')
            actor.wait(lambda s: s['multiline_text'] == expected)
            # Replace the browser value to ensure an actual outbound event is
            # required; never accept the previous inbound value as evidence.
            if args.workspace_origin:
                actor.open_clipboard()
                control.fill('waiting for Qt copy')
                page.locator('#clipboard-close').click()
            else:
                control.fill('waiting for Qt copy')
            click('multiline_editor')
            page.keyboard.press('Control+a')
            page.keyboard.press('Control+c')
            if args.workspace_origin:
                page.wait_for_function('text => document.querySelector("#clipboard").value === text', arg=expected)
            else:
                page.wait_for_function('text => document.querySelector("#clipboard-output").value === text', arg=expected)
            passed(text=expected, both_directions=True)
            page.screenshot(path=str(output/'interactions.png'))
            click('interactions_close_button')
            actor.wait(lambda s: not s['interactions_open'])
        except Exception as error:
            results.append({'check': current, 'status': 'fail', 'error': str(error)})
            (output/'results.json').write_text(json.dumps({'checks': results}, indent=2)+'\n')
            if 'actor' in locals():
                (output/'failure-state.json').write_text(json.dumps(actor.state(), indent=2)+'\n')
                actor.page.screenshot(path=str(output/'failure.png'))
            raise
        finally:
            browser.close()
            if probe and probe.started:
                (output/'container.log').write_text(smoke.run(args.engine, 'logs', probe.name, check=False).stdout)
                smoke.run(args.engine, 'rm', '-f', probe.name, check=False)


if __name__ == '__main__':
    main()
