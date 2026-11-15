#!/usr/bin/env python3
"""Wire regression for fixtures/vncws_reentrant.cpp, against an existing fixture.

Requires the project's Python 3.12 environment (aiohttp). Root owns launching
and terminating Qt/containers. Do not attach a browser concurrently: the plugin
allows one viewer. Run each encoding/alpha case against a fresh fixture.

Example (the observation file must be visible on this machine):
  .venv/bin/python tests/vncws_reentrant.py --url ws://127.0.0.1:5901/ \
      --state-file artifacts/reentrant/state.json --cycles 3 --encoding raw
Repeat with --encoding zlib. --cycles 0 checks only ordinary pixels/input.
The framebuffer may exceed the fixed 800x600 fixture (e.g. the standard probe
launcher's 1280x800); sample coordinates stay relative to the fixture at (0,0).
--expect-hang requires a nested-paint stall with a frozen heartbeat, after the
ordinary baseline passed; a healthy fixed plugin deliberately fails that mode.
The parent process enforces --hard-timeout even if the async worker gets stuck.
It kills its worker only; the caller must bound/clean up the Qt process too.

Alpha is optional characterization by default, since upstream QFbScreen uses
CompositionMode_Source. --require-alpha-blend explicitly adds a blending gate;
the opaque modal, stacking, restoration and liveness gates always apply.
No Qt actions are performed by observation: all clicks are real RFB input.
Fixture --dialogs-per-paint 2 is detected automatically. Optional --parent-action
update|resize|both clicks the modal's ordinary buttons (resize needs fixture
--allow-parent-resize). Fixture --maximized keeps sample coordinates unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import time
import traceback


ROOT = Path(__file__).resolve().parents[1]
PARENT = ((36, 90, 156), (38, 142, 82), (224, 178, 48), (128, 80, 180))
DIALOG = (210, 90, 54)
BAND = (40, 120, 220)
CONTROLS = {'arm': (24, 20, 180, 44), 'follow_up': (224, 20, 180, 44),
            'dismiss': (324, 456, 152, 48)}
PARENT_CONTROLS = {'resize_parent': (244, 328, 144, 40),
                   'update_parent': (412, 328, 144, 40)}
SAMPLES = {'parent': (56, 432, 48, 32), 'dialog_body': (244, 260, 48, 32),
           'stacking': (280, 420, 48, 24), 'alpha': (280, 384, 48, 24)}


def load_state(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def rect(value):
    return tuple(value[key] for key in ('x', 'y', 'width', 'height'))


def pointer(mask, x, y):
    return struct.pack('>BBHH', 5, mask, x, y)


def blend(color, background):
    return tuple((front * 128 + back * 127 + 127) // 255
                 for front, back in zip(color, background))


class Frame:
    """Reassemble independently decoded rectangles; ignore the unused X byte."""
    def __init__(self, size):
        self.width, self.height = size
        self.data = bytearray(self.width * self.height * 4)
        self.updates = 0

    def apply(self, rectangles):
        assert isinstance(rectangles, list), rectangles
        for item in rectangles:
            x, y, width, height = item['rect']
            assert item['encoding'] in (0, 6), item['encoding']
            assert 0 <= x < x + width <= self.width
            assert 0 <= y < y + height <= self.height
            pixels = item['pixels']
            assert len(pixels) == width * height * 4
            for row in range(height):
                start = ((y + row) * self.width + x) * 4
                self.data[start:start + width * 4] = pixels[row * width * 4:(row + 1) * width * 4]
        self.updates += 1

    def colors(self, area):
        x, y, width, height = area
        assert 0 <= x < x + width <= self.width
        assert 0 <= y < y + height <= self.height
        return {tuple(self.data[offset:offset + 3][::-1])
                for row in range(y, y + height)
                for col in range(x, x + width)
                for offset in [(row * self.width + col) * 4]}

    def rgb(self, area):
        x, y, width, height = area
        assert 0 <= x < x + width <= self.width
        assert 0 <= y < y + height <= self.height
        rows = b''.join(self.data[((y + row) * self.width + x) * 4:
                                 ((y + row) * self.width + x + width) * 4]
                        for row in range(height))
        rgb = bytearray(width * height * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = rows[2::4], rows[1::4], rows[0::4]
        return bytes(rgb)

    def differences(self, expectations):
        differences = {}
        for name, (expected, tolerance) in expectations.items():
            colors = self.colors(SAMPLES[name])
            if any(any(abs(a - b) > tolerance for a, b in zip(actual, expected))
                   for actual in colors):
                differences[name] = {'expected': expected, 'tolerance': tolerance,
                                     'observed_rgb': sorted(colors)[:16]}
        return differences

    def save(self, path):
        # Portable PPM avoids Pillow/Playwright and retains exact decoded RGB.
        rgb = bytearray(self.width * self.height * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = self.data[2::4], self.data[1::4], self.data[0::4]
        path.write_bytes(f'P6\n{self.width} {self.height}\n255\n'.encode() + rgb)


async def checks(args, report):
    # Reuse the existing independent handshake, pixel format, request packing
    # and persistent Raw/zlib decoder, without invoking their container launchers.
    import aiohttp
    import vncws_protocol as protocol
    from vncws_compression import CompressedRfb, encodings, pixel_format

    output = args.output
    frame = None
    identity = None
    baseline_passed = False
    last_mismatch = None
    table_cell = None
    cell_reference = None

    def checkpoint():
        report['last_state'] = load_state(args.state_file)
        write_json(output / 'results.json', report)

    def phase(name):
        report['phase'] = name
        checkpoint()
        print(name, flush=True)

    def state():
        value = load_state(args.state_file)
        if value is not None:
            assert value['schema_version'] == 1 and value['fixture'] == 'vncws_reentrant', value
            if identity is not None:
                assert value['pid'] == identity, 'fixture process identity changed'
        return value

    async def observed(predicate):
        async def wait():
            while True:
                value = state()
                if value is not None and predicate(value):
                    return value
                await asyncio.sleep(.025)
        return await asyncio.wait_for(wait(), args.timeout)

    def passed(name, **detail):
        report['checks'].append({'check': name, 'status': 'pass', **detail})
        checkpoint()

    async def click(rfb, name):
        # Independent constants catch geometry contract drift in the observer.
        x, y, width, height = (CONTROLS | PARENT_CONTROLS)[name]
        x, y = x + width // 2, y + height // 2
        await rfb.ws.send_bytes(pointer(0, x, y) + pointer(1, x, y) + pointer(0, x, y))

    async def pixels(rfb, name, expectations, compare_cell=False):
        nonlocal last_mismatch
        async def wait():
            nonlocal last_mismatch
            while True:
                await rfb.ws.send_bytes(protocol.request(0, 0, *rfb.size))
                rectangles = await rfb.update()
                frame.apply(rectangles)
                expected_encoding = 6 if args.encoding == 'zlib' else 0
                assert all(item['encoding'] == expected_encoding for item in rectangles)
                last_mismatch = frame.differences(expectations)
                if compare_cell and frame.rgb(table_cell) != cell_reference:
                    last_mismatch['table_cell'] = {
                        'expected_sha256': hashlib.sha256(cell_reference).hexdigest(),
                        'observed_sha256': hashlib.sha256(frame.rgb(table_cell)).hexdigest()}
                if not last_mismatch:
                    frame.save(output / f'{name}.ppm')
                    return
                await asyncio.sleep(.025)
        await asyncio.wait_for(wait(), args.timeout)

    try:
        phase('fixture_ready')
        initial = await observed(lambda s: s['ready'])
        identity = initial['pid']
        assert not initial['armed'] and not initial['modal_active'], initial
        assert initial['arm_count'] == initial['modal_entered'] == initial['modal_exited'] == 0
        assert initial['follow_up_count'] == initial['parent_revision'] == 0, 'use a fresh fixture'
        x, y, root_width, root_height = rect(initial['geometry']['root'])
        assert (x, y) == (0, 0) and root_width >= 800 and root_height >= 600
        for group, expected in (('controls', CONTROLS), ('samples', SAMPLES)):
            assert {name: rect(initial[group][name]) for name in expected} == expected
        if args.require_alpha_blend:
            assert initial['alpha_dialog'], '--require-alpha-blend needs fixture --alpha-dialog'
        sequence_length = initial.get('dialogs_per_paint', 1)
        assert sequence_length in (1, 2), sequence_length
        if args.parent_action != 'none':
            assert {name: rect(initial['controls'][name]) for name in PARENT_CONTROLS} == PARENT_CONTROLS
        if args.parent_action in ('resize', 'both'):
            assert initial.get('allow_parent_resize'), 'resize action needs fixture --allow-parent-resize'
        write_json(output / 'initial-state.json', initial)

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=args.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            ws = await asyncio.wait_for(session.ws_connect(
                args.url, max_msg_size=32 * protocol.MIB), args.timeout)
            rfb = CompressedRfb(ws)
            try:
                phase('normal_pixel_baseline')
                await asyncio.wait_for(rfb.init(expected_size=None), args.timeout)
                width, height = rfb.size
                assert 800 <= width <= 4096 and 600 <= height <= 4096, rfb.size
                assert width * height <= 4_953_600, rfb.size
                report['framebuffer_size'] = rfb.size
                frame = Frame(rfb.size)
                await ws.send_bytes(pixel_format() + encodings(
                    *((6, 0) if args.encoding == 'zlib' else (0,))))
                restored = {name: (PARENT[0], 0) for name in ('parent', 'stacking', 'alpha')}
                await pixels(rfb, 'baseline', restored)
                passed('normal_pixel_baseline', encoding=args.encoding)

                phase('normal_follow_up_input')
                await click(rfb, 'follow_up')
                current = await observed(lambda s: s['follow_up_count'] == 1 and s['parent_revision'] == 1)
                await pixels(rfb, 'baseline-follow-up', {
                    name: (PARENT[1], 0) for name in ('parent', 'stacking', 'alpha')})
                assert current['modal_entered'] == 0, current
                passed('normal_follow_up_input')
                table_cell = rect(current['samples']['table_cell'])
                assert len(frame.colors(table_cell)) > 1, 'table reference must include cell text'
                cell_reference = frame.rgb(table_cell)
                passed('table_cell_reference', rect=table_cell,
                       sha256=hashlib.sha256(cell_reference).hexdigest())
                baseline_passed = True

                for cycle in range(1, args.cycles + 1):
                    before = state()
                    phase(f'cycle_{cycle}_modal_entry')
                    await click(rfb, 'arm')
                    paint_id = None
                    for dialog_index in range(1, sequence_length + 1):
                        target = before['modal_entered'] + dialog_index
                        label = f'cycle-{cycle}-dialog-{dialog_index}'
                        phase(f'{label}_entry')
                        opened = await observed(lambda s: s['modal_active'] and s['modal_entered'] == target)
                        assert opened['arm_count'] == cycle, opened
                        assert opened['armed'] == (dialog_index < sequence_length), opened
                        assert opened['modal_exited'] == target - 1, opened
                        assert opened['modal_trigger_paint_depth'] > 0 and opened['table_paint_depth'] > 0, opened
                        assert opened['modal_trigger_thread_matches_gui'], opened
                        if sequence_length > 1:
                            if paint_id is None:
                                paint_id = opened['modal_trigger_paint_id']
                            assert paint_id == opened['modal_trigger_paint_id'], 'dialogs escaped one outer paint'
                            assert opened['dialogs_this_arm'] == dialog_index, opened
                        write_json(output / f'{label}-entered-state.json', opened)

                        phase(f'{label}_pixels')
                        parent = PARENT[opened['parent_revision'] % len(PARENT)]
                        expected = {'dialog_body': (DIALOG, 0), 'stacking': (DIALOG, 0)}
                        # A second exec can observe unfinished parent pixels;
                        # visual recovery is asserted after the outer paint.
                        if dialog_index == 1:
                            expected['parent'] = (parent, 0)
                        if not opened['alpha_dialog']:
                            expected['alpha'] = (BAND, 0)
                        elif args.require_alpha_blend:
                            expected['alpha'] = (blend(BAND, parent), 1)
                        await pixels(rfb, f'{label}-modal', expected)
                        painted = await observed(lambda s: s['dialog_paints'] > before['dialog_paints'])
                        alive = await observed(lambda s: s['heartbeat'] >= painted['heartbeat'] + 2)
                        assert alive['modal_active'] and alive['table_paint_depth'] > 0, alive
                        alpha = {'mode': 'opaque' if not opened['alpha_dialog'] else 'characterization',
                                 'observed_rgb': sorted(frame.colors(SAMPLES['alpha'])),
                                 'expected_blended_rgb': blend(BAND, parent)}
                        passed(f'{label}_same_thread_modal_pixels_and_heartbeat',
                               modal_trigger_paint_depth=opened['modal_trigger_paint_depth'],
                               modal_trigger_paint_id=opened.get('modal_trigger_paint_id'), alpha=alpha)

                        actions = {'none': (), 'update': ('update_parent',),
                                   'resize': ('resize_parent',),
                                   'both': ('update_parent', 'resize_parent')}[args.parent_action]
                        for action in actions:
                            phase(f'{label}_{action}')
                            prior = state()
                            counter = 'update_requests' if action == 'update_parent' else 'resize_requests'
                            await click(rfb, action)
                            changed = await observed(lambda s: s[counter] == prior[counter] + 1)
                            await observed(lambda s: s['heartbeat'] > changed['heartbeat'])
                            if action == 'resize_parent':
                                await observed(lambda s: s['parent_resize_events'] > prior['parent_resize_events'])
                            await pixels(rfb, f'{label}-{action}', {
                                'dialog_body': (DIALOG, 0), 'stacking': (DIALOG, 0)})
                            passed(f'{label}_{action}_liveness')

                        phase(f'{label}_dismiss')
                        await click(rfb, 'dismiss')
                        await observed(lambda s: s['modal_exited'] >= target)
                    phase(f'cycle_{cycle}_dismiss_and_parent_recovery')
                    closed = await observed(lambda s: not s['modal_active'] and
                                            s['modal_exited'] == cycle * sequence_length and
                                            s['table_paint_depth'] == 0)
                    assert closed['modal_entered'] == cycle * sequence_length and closed['arm_count'] == cycle, closed
                    assert closed['parent_revision'] == before['parent_revision'] + sequence_length, closed
                    assert closed['table_paint_exited'] > before['table_paint_exited'], closed
                    parent = PARENT[closed['parent_revision'] % len(PARENT)]
                    await pixels(rfb, f'cycle-{cycle}-resumed', {
                        name: (parent, 0) for name in ('parent', 'stacking', 'alpha')},
                        compare_cell=args.parent_action == 'none')
                    await observed(lambda s: s['parent_paints'] > before['parent_paints'] and
                                   s['heartbeat'] > closed['heartbeat'])
                    passed(f'cycle_{cycle}_parent_recovery')

                    phase(f'cycle_{cycle}_follow_up_input')
                    await click(rfb, 'follow_up')
                    current = await observed(lambda s: s['follow_up_count'] == cycle + 1 and
                                             s['parent_revision'] == closed['parent_revision'] + 1)
                    parent = PARENT[current['parent_revision'] % len(PARENT)]
                    await pixels(rfb, f'cycle-{cycle}-follow-up', {
                        name: (parent, 0) for name in ('parent', 'stacking', 'alpha')}, compare_cell=True)
                    assert current['modal_entered'] == current['modal_exited'] == cycle * sequence_length, current
                    passed(f'cycle_{cycle}_follow_up_input')
                    write_json(output / f'cycle-{cycle}-completed-state.json', state())
                assert not args.expect_hang, 'expected baseline hang, but all requested cycles recovered'
                report['status'] = 'pass'
            finally:
                # Closing a stalled connection must not eat the external deadline.
                try:
                    await asyncio.wait_for(ws.close(), 1)
                except TimeoutError:
                    pass
    except Exception as error:
        first = load_state(args.state_file)
        await asyncio.sleep(.35)
        last = load_state(args.state_file)
        nested = bool(last and last.get('modal_active') and
                      last.get('table_paint_depth', 0) > 0 and
                      last.get('modal_trigger_paint_depth', 0) > 0 and
                      last.get('modal_trigger_thread_matches_gui') and
                      last.get('modal_entered') == last.get('modal_exited', 0) + 1)
        frozen = bool(first and last and first['pid'] == last['pid'] and
                      first['heartbeat'] == last['heartbeat'])
        expected_hang = (args.expect_hang and baseline_passed and
                         isinstance(error, TimeoutError) and nested and frozen)
        report.update(status='expected_hang' if expected_hang else 'fail',
                      error=repr(error), nested_paint_observed=nested,
                      heartbeat_frozen=frozen, pixel_mismatch=last_mismatch)
        write_json(output / 'failure-state-first.json', first)
        write_json(output / 'failure-state-last.json', last)
        (output / 'failure.log').write_text(traceback.format_exc())
        if frame:
            frame.save(output / 'last-decoded-frame.ppm')
    finally:
        checkpoint()
    return 0 if report['status'] in ('pass', 'expected_hang') else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--url', default='ws://127.0.0.1:5901/')
    parser.add_argument('--state-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/vncws-reentrant/wire')
    parser.add_argument('--cycles', type=int, default=3)
    parser.add_argument('--encoding', choices=('raw', 'zlib'), default='raw')
    parser.add_argument('--timeout', type=float, default=8, help='per-phase deadline in seconds')
    parser.add_argument('--hard-timeout', type=float, default=90, help='external worker deadline in seconds')
    parser.add_argument('--expect-hang', action='store_true')
    parser.add_argument('--require-alpha-blend', action='store_true')
    parser.add_argument('--parent-action', choices=('none', 'update', 'resize', 'both'), default='none')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.cycles < 0 or not (0 < args.timeout < float('inf')) or not (0 < args.hard_timeout < float('inf')):
        parser.error('cycles must be nonnegative and timeouts must be positive and finite')
    if args.expect_hang and args.cycles == 0:
        parser.error('--expect-hang requires at least one modal cycle')
    args.output = args.output.resolve()
    args.state_file = args.state_file.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {'status': 'running', 'checks': [], 'phase': 'starting',
              'encoding': args.encoding, 'cycles': args.cycles,
              'state_file': str(args.state_file), 'url': args.url}
    if args.worker:
        try:
            return asyncio.run(checks(args, report))
        except Exception as error:
            report.update(status='fail', error=repr(error))
            write_json(args.output / 'results.json', report)
            (args.output / 'failure.log').write_text(traceback.format_exc())
            return 1

    # This parent does no asyncio/network work and needs no third-party imports.
    # subprocess.run kills/reaps the worker on timeout, even if Qt/RFB is wedged.
    started = time.monotonic()
    write_json(args.output / 'results.json', report)
    try:
        completed = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                    *sys.argv[1:], '--worker'], capture_output=True,
                                   text=True, timeout=args.hard_timeout, check=False)
        (args.output / 'worker.log').write_text(completed.stdout + completed.stderr)
        print(completed.stdout, end='', flush=True)
        if completed.stderr:
            print(completed.stderr, end='', file=sys.stderr)
        return completed.returncode
    except subprocess.TimeoutExpired as error:
        previous = load_state(args.output / 'results.json') or report
        previous.update(status='fail', error='external worker deadline exceeded',
                        elapsed_seconds=round(time.monotonic() - started, 3),
                        last_state=load_state(args.state_file))
        write_json(args.output / 'results.json', previous)
        write_json(args.output / 'watchdog.json', previous)
        log = (error.stdout or b'') + (error.stderr or b'')
        (args.output / 'worker.log').write_bytes(log)
        print('external worker deadline exceeded', file=sys.stderr)
        return 124


if __name__ == '__main__':
    raise SystemExit(main())
