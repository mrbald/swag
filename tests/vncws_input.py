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
    return results


if __name__ == '__main__':
    raise SystemExit(protocol.main(checks, ROOT / 'artifacts/qt-ws-input'))
