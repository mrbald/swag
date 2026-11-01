#!/usr/bin/env python3
"""Independent wire-level fault checks for the private vncws probe.

These inspect protocol responses and process survival; GUI acceptance remains
in tests/browser and uses real noVNC mouse/keyboard input.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import struct
import subprocess
import sys
import time
import uuid

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
PIXEL = struct.pack('>BBBBHHHBBBxxx', 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)


def command(*args, check=True):
    return subprocess.run(args, cwd=ROOT, check=check, text=True,
                          capture_output=True, timeout=60)


class Rfb:
    def __init__(self, ws):
        self.ws, self.buffer = ws, bytearray()

    async def read(self, size):
        while len(self.buffer) < size:
            msg = await self.ws.receive(timeout=4)
            if msg.type != aiohttp.WSMsgType.BINARY:
                raise EOFError(f'expected RFB bytes, received {msg.type}')
            self.buffer.extend(msg.data)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    async def init(self, fragmented=False, combined=b'', expected_size=(1280, 800)):
        assert await self.read(12) == b'RFB 003.003\n'
        banner = b'RFB 003.003\n'
        if fragmented:
            for byte in banner:
                await self.ws.send_bytes(bytes([byte]))
                await asyncio.sleep(.005)
        else:
            await self.ws.send_bytes(banner + combined)
        assert await self.read(4) == struct.pack('>I', 1)
        if not combined:
            await self.ws.send_bytes(b'\x01')
        header = await self.read(24)
        width, height = struct.unpack('>HH', header[:4])
        self.size = (width, height)
        if expected_size is not None:
            assert self.size == expected_size, self.size
        name_size = struct.unpack('>I', header[20:24])[0]
        assert name_size < 4096
        await self.read(name_size)

    async def update(self):
        header = await self.read(4)
        assert header[:2] == b'\0\0', header
        count = struct.unpack('>H', header[2:])[0]
        assert count <= 4096, count
        rectangles = []
        self.pixels = bytearray()
        for _ in range(count):
            x, y, w, h, encoding = struct.unpack('>HHHHi', await self.read(12))
            assert encoding == 0, encoding
            assert 0 < w <= 1280 and 0 < h <= 800, (x, y, w, h)
            assert x + w <= 1280 and y + h <= 800, (x, y, w, h)
            self.pixels.extend(await self.read(w * h * 4))
            rectangles.append([x, y, w, h])
        return rectangles

    async def closed(self, timeout=4):
        async with asyncio.timeout(timeout):
            while True:
                message = await self.ws.receive()
                if message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                    aiohttp.WSMsgType.ERROR}:
                    return


def request(x=0, y=0, width=8, height=8):
    return struct.pack('>BBHHHH', 3, 0, x, y, width, height)


async def checks(url, output):
    results = []
    async with aiohttp.ClientSession() as session:
        async def connect():
            return Rfb(await session.ws_connect(url, max_msg_size=16*MIB))

        async def check(name, function):
            try:
                detail = await function() or {}
                results.append(dict(check=name, status='pass', **detail))
            except Exception as error:
                results.append(dict(check=name, status='fail', error=repr(error)))
            print(f'{name}: {results[-1]["status"]}', flush=True)
            (output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')

        async def valid(fragmented=False, combined=False):
            rfb = await connect()
            try:
                packet = b'\x01' + b'\0\0\0\0' + PIXEL + request() if combined else b''
                await rfb.init(fragmented=fragmented, combined=packet)
                if not combined:
                    # No SetPixelFormat: negotiated server defaults must work.
                    await rfb.ws.send_bytes(request())
                rects = await rfb.update()
                assert rects, 'no framebuffer returned'
                return dict(rectangles=rects)
            finally:
                await rfb.ws.close()

        async def clipped(x, y, width, height, expected):
            rfb = await connect()
            try:
                await rfb.init()
                await rfb.ws.send_bytes(request(x, y, width, height))
                actual = await rfb.update()
                assert actual == expected, actual
                return dict(rectangles=actual)
            finally:
                await rfb.ws.close()

        async def rejected(packet, handshake=True, text=False):
            rfb = await connect()
            try:
                if handshake:
                    await rfb.init()
                else:
                    await rfb.read(12)
                if text:
                    await rfb.ws.send_str(packet)
                else:
                    await rfb.ws.send_bytes(packet)
                await rfb.closed()
            finally:
                await rfb.ws.close()

        async def fragmented_body():
            rfb = await connect()
            try:
                await rfb.init()
                packet = b'\0\0\0\0' + PIXEL + struct.pack('>BBHi', 2, 0, 1, 0) + request()
                for byte in packet:
                    await rfb.ws.send_bytes(bytes([byte]))
                    await asyncio.sleep(.005)
                assert await rfb.update()
            finally:
                await rfb.ws.close()

        async def encoding_changes(cursor=False):
            rfb = await connect()
            try:
                await rfb.init()
                if cursor:
                    packet = struct.pack('>BBHi', 2, 0, 1, -239) + struct.pack('>BBHi', 2, 0, 1, 0)
                else:
                    packet = struct.pack('>BBHii', 2, 0, 2, -9999, 0)
                await rfb.ws.send_bytes(packet + request())
                assert await rfb.update()
            finally:
                await rfb.ws.close()

        async def rgb_conversion():
            rfb = await connect()
            try:
                await rfb.init()
                # Alternate grid rows have unequal RGB channels. Compare the
                # same static region in server-native and noVNC byte order.
                area = request(80, 180, 32, 32)
                await rfb.ws.send_bytes(area)
                first_rects = await rfb.update()
                native = bytes(rfb.pixels)
                assert any(a != b for a,b in zip(native[0::4], native[2::4])), 'region lacks color'
                novnc = struct.pack('>BBBBHHHBBBxxx', 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)
                await rfb.ws.send_bytes(b'\0\0\0\0' + novnc + area)
                assert await rfb.update() == first_rects
                converted = bytes(rfb.pixels)
                assert converted[0::4] == native[2::4], 'red byte conversion'
                assert converted[1::4] == native[1::4], 'green byte conversion'
                assert converted[2::4] == native[0::4], 'blue byte conversion'
                return {'pixels_compared':len(native)//4}
            finally:
                await rfb.ws.close()

        async def replacement():
            old = await connect()
            new = None
            try:
                await old.init()
                new = await connect()
                await new.init()
                await old.closed()
                await new.ws.send_bytes(request())
                assert await new.update()
            finally:
                await old.ws.close()
                if new:
                    await new.ws.close()

        async def deadline():
            rfb = await connect()
            try:
                await rfb.read(12)
                start = time.monotonic()
                await rfb.closed(timeout=13)
                elapsed = time.monotonic() - start
                assert 8 <= elapsed <= 13, elapsed
                return dict(seconds=round(elapsed, 2))
            finally:
                await rfb.ws.close()

        await check('fragmented_handshake_default_pixel_format', lambda: valid(fragmented=True))
        await check('coalesced_handshake_and_messages', lambda: valid(combined=True))
        await check('fragmented_message_bodies', fragmented_body)
        await check('partially_outside_request_clipped', lambda: clipped(1278, 798, 20, 20, [[1278, 798, 2, 2]]))
        await check('outside_request_empty', lambda: clipped(65500, 65500, 35, 35, []))
        await check('zero_area_request_empty', lambda: clipped(0, 0, 0, 0, []))
        for name, packet in [('invalid_version', b'garbage!!!!!'), ('unsupported_version', b'RFB 003.008\n')]:
            await check(name, lambda p=packet: rejected(p, handshake=False))
        for name, packet in [('unknown_message', b'\xfa'), ('unsupported_colormap', b'\x01'),
                             ('too_many_encodings', struct.pack('>BBH', 2, 0, 257)),
                             ('oversized_clipboard', struct.pack('>BBBBI', 6, 0, 0, 0, MIB+1))]:
            await check(name, lambda p=packet: rejected(p))
        for name, offset, value in [('bad_bpp', 0, 8), ('bad_depth', 1, 0),
                                    ('big_endian', 2, 1), ('indexed_color', 3, 0), ('noncanonical_true_color', 3, 2),
                                    ('bad_maximum', 5, 0), ('bad_shift', 10, 32)]:
            bad = bytearray(PIXEL)
            bad[offset] = value
            await check(name, lambda p=bytes(bad): rejected(b'\0\0\0\0' + p))
        await check('text_websocket_rejected', lambda: rejected('not binary', text=True))
        await check('unknown_encoding_hint_ignored', encoding_changes)
        await check('cursor_withdrawal_before_update', lambda: encoding_changes(cursor=True))
        await check('novnc_rgb_channel_conversion', rgb_conversion)
        await check('viewer_replacement', replacement)
        await check('rfb_handshake_deadline', deadline)
        await check('healthy_after_faults', valid)
    return results


def main(checks_fn=checks, default_output=ROOT/'artifacts/qt-ws-hardening/protocol'):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', default='podman')
    parser.add_argument('--port', type=int, default=6102)
    parser.add_argument('--output', type=Path, default=default_output)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = 'swag-protocol-' + uuid.uuid4().hex[:8]
    try:
        result = command(sys.executable, 'tools/probe.py', 'run', '--backend', 'qt-ws',
                         '--engine', args.engine, '--name', name, '--port', str(args.port),
                         '--output', str(output))
        (output/'launch.json').write_text(result.stdout)
        deadline = time.monotonic()+25
        while time.monotonic() < deadline:
            try:
                if json.loads((output/'backend.json').read_text())['ready']:
                    break
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(.1)
        else:
            raise TimeoutError('probe readiness')
        results = asyncio.run(checks_fn(f'http://127.0.0.1:{args.port+1}/websockify', output))
        return int(any(r['status'] != 'pass' for r in results))
    finally:
        result = command(args.engine, 'logs', name, check=False)
        (output/'container.log').write_text(result.stdout+result.stderr)
        command(args.engine, 'cp', f'{name}:/opt/swag/image-manifest.json', str(output/'image-manifest.json'), check=False)
        command(args.engine, 'rm', '--force', name, check=False)


if __name__ == '__main__':
    raise SystemExit(main())
