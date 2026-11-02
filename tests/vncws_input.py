#!/usr/bin/env python3
"""Wire input checks with passive Qt event observation from swag-probe."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import struct
import time

import aiohttp

import vncws_protocol as protocol


ROOT = Path(__file__).resolve().parents[1]
SHIFT = 0x02000000
CONTROL = 0x04000000
META = 0x10000000
KEYPAD = 0x20000000
KEY_RETURN = 0x01000004
KEY_ENTER = 0x01000005


def key(down: bool, keysym: int) -> bytes:
    return struct.pack('>BBHI', 4, int(down), 0, keysym)


def pointer(mask: int = 0, x: int = 300, y: int = 250) -> bytes:
    return struct.pack('>BBHH', 5, mask, x, y)


async def observed(output: Path, predicate, timeout: float = 4):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = json.loads((output / 'state.json').read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        else:
            if predicate(last):
                return last
        await asyncio.sleep(.025)
    raise AssertionError(f'Qt observation timed out: {last}')


async def checks(url: str, output: Path):
    results = []
    async with aiohttp.ClientSession() as session:
        async def run(name, body):
            rfb = protocol.Rfb(await session.ws_connect(url))
            try:
                await rfb.init()
                detail = await body(rfb) or {}
                results.append({'check': name, 'status': 'pass', **detail})
            except Exception as error:
                results.append({'check': name, 'status': 'fail', 'error': repr(error)})
            finally:
                await rfb.ws.close()
            print(f'{name}: {results[-1]["status"]}', flush=True)
            (output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')

        async def release_then_pointer(rfb, keysym, modifier):
            await rfb.ws.send_bytes(key(True, keysym))
            await observed(output, lambda s: s['keyboard_modifiers'] == modifier)
            # Both messages in one WebSocket frame exercise parser ordering
            # before Qt has processed the queued key-release event.
            await rfb.ws.send_bytes(key(False, keysym) + pointer(1))
            await observed(output, lambda s: s['mouse_buttons'] == 1 and
                           s['keyboard_modifiers'] == 0)
            await rfb.ws.send_bytes(pointer(0, 301, 250))
            later = await observed(output, lambda s: s['mouse_buttons'] == 0 and
                                   s['keyboard_modifiers'] == 0)
            return {'after_later_pointer_modifiers': later['keyboard_modifiers']}

        async def overlapping_sides(rfb, left, right, modifier):
            await rfb.ws.send_bytes(key(True, left) + key(True, right))
            await observed(output, lambda s: s['keyboard_modifiers'] == modifier)
            await rfb.ws.send_bytes(key(False, left) + pointer(1))
            await observed(output, lambda s: s['mouse_buttons'] == 1 and
                           s['keyboard_modifiers'] == modifier)
            await rfb.ws.send_bytes(key(False, right) + pointer(0, 301, 250))
            await observed(output, lambda s: s['mouse_buttons'] == 0 and
                           s['keyboard_modifiers'] == 0)

        async def wheel_modifiers(rfb, keysym, modifier):
            await rfb.ws.send_bytes(key(True, keysym))
            held = await observed(output, lambda s: s['keyboard_modifiers'] == modifier)
            before = held['wheel_events']
            await rfb.ws.send_bytes(pointer(0x08) + pointer())
            wheel = await observed(output, lambda s: s['wheel_events'] > before)
            assert wheel['last_wheel_modifiers'] == modifier, wheel
            before = wheel['wheel_events']
            await rfb.ws.send_bytes(key(False, keysym) + pointer(0x10) + pointer())
            wheel = await observed(output, lambda s: s['wheel_events'] > before)
            assert wheel['last_wheel_modifiers'] == 0, wheel

        async def horizontal_wheel(rfb):
            before = (await observed(output, lambda s: s['ready']))['wheel_events']
            for mask, expected in ((0x20, 'wheel:120,0'),
                                   (0x40, 'wheel:-120,0')):
                await rfb.ws.send_bytes(pointer(mask) + pointer())
                wheel = await observed(output, lambda s: s['wheel_events'] > before)
                assert wheel['last_event'] == expected, wheel
                assert wheel['last_wheel_modifiers'] == 0, wheel
                before = wheel['wheel_events']

        async def meta_shortcut(rfb, keysym):
            await rfb.ws.send_bytes(key(True, keysym))
            await observed(output, lambda s: s['keyboard_modifiers'] == META, timeout=1)
            before = (await observed(output, lambda s: s['ready']))['key_events']
            await rfb.ws.send_bytes(key(True, ord('c')))
            shortcut = await observed(output, lambda s: s['key_events'] > before and
                                      s['last_key'] == 'Meta+C')
            assert shortcut['keyboard_modifiers'] == META, shortcut
            await rfb.ws.send_bytes(key(False, ord('c')) + key(False, keysym))
            await observed(output, lambda s: s['keyboard_modifiers'] == 0)
            return {'shortcut': shortcut['last_key']}

        async def button_transitions(rfb, masks, expected):
            observed_buttons = []
            for mask, buttons in zip(masks, expected):
                await rfb.ws.send_bytes(pointer(mask))
                state = await observed(output, lambda s, b=buttons:
                                       s['mouse_buttons'] == b, timeout=1)
                observed_buttons.append(state['mouse_buttons'])
            return {'rfb_masks': masks, 'qt_buttons': observed_buttons}

        async def disconnect_releases_input(rfb, keysym):
            await rfb.ws.send_bytes(key(True, keysym) + pointer(1) + pointer(3))
            await observed(output, lambda s: s['keyboard_modifiers'] == META and
                           s['mouse_buttons'] == 5)
            await rfb.ws.close()
            released = await observed(output, lambda s: s['keyboard_modifiers'] == 0 and
                                      s['mouse_buttons'] == 0)
            return {'after_disconnect_modifiers': released['keyboard_modifiers'],
                    'after_disconnect_buttons': released['mouse_buttons']}

        async def keypad_identity_and_modifiers(rfb):
            async def press(keysym, keycode, modifiers):
                before = (await observed(output, lambda s: s['ready']))['raw_key_events']
                await rfb.ws.send_bytes(key(True, keysym))
                event = await observed(output, lambda s:
                    s['raw_key_events'] > before and s['last_key_code'] == keycode and
                    s['last_key_modifiers'] == modifiers)
                await rfb.ws.send_bytes(key(False, keysym))
                return event

            await press(0xff0d, KEY_RETURN, 0)
            await press(0xff8d, KEY_ENTER, KEYPAD)
            await press(ord('1'), ord('1'), 0)
            await press(0xffb1, ord('1'), KEYPAD)
            await press(0xff95, 0x01000010, KEYPAD)  # keypad Home
            await rfb.ws.send_bytes(key(True, 0xffe3))  # held Control
            await observed(output, lambda s: s['keyboard_modifiers'] == CONTROL)
            await press(0xff8d, KEY_ENTER, CONTROL | KEYPAD)
            await press(ord('a'), ord('A'), CONTROL)
            await rfb.ws.send_bytes(key(False, 0xffe3))
            await observed(output, lambda s: s['keyboard_modifiers'] == 0)
            return {'return_keycode': KEY_RETURN, 'keypad_enter_keycode': KEY_ENTER,
                    'keypad_modifier': KEYPAD, 'ordinary_after_keypad': CONTROL}

        async def context_menu_key(rfb):
            before = (await observed(output, lambda s: s['ready']))['raw_key_events']
            await rfb.ws.send_bytes(key(True, 0xff67) + key(False, 0xff67))
            state = await observed(output, lambda s: s['raw_key_events'] > before)
            assert state['last_key_code'] == 0x01000055, state
            assert state['last_key_modifiers'] == 0, state
            # Dismiss any widget context menu before subsequent checks.
            await rfb.ws.send_bytes(key(True, 0xff1b) + key(False, 0xff1b))
            await observed(output, lambda s: not s['popup_actions'])
            return {'keycode': 0x01000055}

        async def repeat_flags(rfb):
            async def down(keysym, code, repeat, modifiers=0):
                before = (await observed(output, lambda s: s['ready']))['raw_key_events']
                await rfb.ws.send_bytes(key(True, keysym))
                state = await observed(output, lambda s: s['raw_key_events'] > before)
                assert state['last_key_code'] == code, state
                assert state['last_key_auto_repeat'] is repeat, state
                assert state['last_key_modifiers'] == modifiers, state
            await down(0xffc2, 0x01000034, False)  # F5
            await down(0xffc2, 0x01000034, True)
            await rfb.ws.send_bytes(key(False, 0xffc2))
            await down(0xffc2, 0x01000034, False)
            await rfb.ws.send_bytes(key(False, 0xffc2) + key(True, 0xffe3))
            await observed(output, lambda s: s['keyboard_modifiers'] == CONTROL)
            await down(0xffb1, ord('1'), False, CONTROL | KEYPAD)
            await down(0xffb1, ord('1'), True, CONTROL | KEYPAD)
            await rfb.ws.send_bytes(key(False, 0xffb1) + key(False, 0xffe3))
            return {'first_repeat_repress': [False, True, False],
                    'repeated_keypad_modifiers': CONTROL | KEYPAD}

        async def repeat_reset(rfb, replacement):
            before = (await observed(output, lambda s: s['ready']))['raw_key_events']
            await rfb.ws.send_bytes(key(True, 0xffc2))
            await observed(output, lambda s: s['raw_key_events'] > before and
                           s['last_key_code'] == 0x01000034)
            if not replacement:
                await rfb.ws.close()
            fresh = protocol.Rfb(await session.ws_connect(url))
            try:
                await fresh.init()
                before = (await observed(output, lambda s: s['ready']))['raw_key_events']
                await fresh.ws.send_bytes(key(True, 0xffc2))
                state = await observed(output, lambda s: s['raw_key_events'] > before)
                assert state['last_key_code'] == 0x01000034, state
                assert state['last_key_auto_repeat'] is False, state
                return {'first_press_after_replacement' if replacement else 'first_press_after_disconnect': False}
            finally:
                await fresh.ws.close()

        await run('ctrl_release_then_pointer',
                  lambda r: release_then_pointer(r, 0xffe3, CONTROL))
        await run('shift_release_then_pointer',
                  lambda r: release_then_pointer(r, 0xffe1, SHIFT))
        await run('ctrl_left_right_overlap',
                  lambda r: overlapping_sides(r, 0xffe3, 0xffe4, CONTROL))
        await run('shift_left_right_overlap',
                  lambda r: overlapping_sides(r, 0xffe1, 0xffe2, SHIFT))
        await run('ctrl_wheel_modifiers',
                  lambda r: wheel_modifiers(r, 0xffe3, CONTROL))
        await run('shift_wheel_modifiers',
                  lambda r: wheel_modifiers(r, 0xffe1, SHIFT))
        await run('horizontal_wheel_signs', horizontal_wheel)
        await run('super_left_meta_shortcut',
                  lambda r: meta_shortcut(r, 0xffeb))
        await run('super_right_meta_shortcut',
                  lambda r: meta_shortcut(r, 0xffec))
        await run('legacy_meta_shortcut',
                  lambda r: meta_shortcut(r, 0xffe7))
        await run('super_left_right_overlap',
                  lambda r: overlapping_sides(r, 0xffeb, 0xffec, META))
        await run('meta_super_mixed_overlap',
                  lambda r: overlapping_sides(r, 0xffe7, 0xffec, META))
        await run('left_to_middle_with_intermediate_release',
                  lambda r: button_transitions(r, (1, 2, 0), (1, 4, 0)))
        await run('left_to_right_with_intermediate_release',
                  lambda r: button_transitions(r, (1, 4, 0), (1, 2, 0)))
        await run('simultaneous_press_and_all_release',
                  lambda r: button_transitions(r, (3, 0), (5, 0)))
        await run('disconnect_releases_meta_and_multiple_buttons',
                  lambda r: disconnect_releases_input(r, 0xffeb))
        await run('legacy_meta_disconnect_releases_multiple_buttons',
                  lambda r: disconnect_releases_input(r, 0xffe7))
        await run('keypad_enter_identity_and_event_local_modifier',
                  keypad_identity_and_modifiers)
        await run('autorepeat_flags_release_and_keypad', repeat_flags)
        await run('autorepeat_resets_on_disconnect', lambda r: repeat_reset(r, False))
        await run('autorepeat_resets_on_replacement', lambda r: repeat_reset(r, True))
        await run('context_menu_key_mapping', context_menu_key)
    return results


if __name__ == '__main__':
    raise SystemExit(protocol.main(checks, ROOT / 'artifacts/qt-ws-input'))
