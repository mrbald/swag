#!/usr/bin/env python3
"""Independent wire checks for tests/fixtures/vncws_entropy.cpp (see its README)."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import struct
import zlib

import aiohttp
import vncws_protocol as protocol


WIDTH, HEIGHT = 3440, 1440
PIXEL_BYTES = WIDTH * HEIGHT * 4
MIB = 1024 * 1024
CURSOR_SIZE = (495, 499)
CURSOR_PIXELS = bytes.fromhex('563412ff') * (CURSOR_SIZE[0] * CURSOR_SIZE[1])
CURSOR_MASK = b'\xff' * (((CURSOR_SIZE[0] + 7) // 8) * CURSOR_SIZE[1])


def encodings(*values):
    return struct.pack('>BBH', 2, 0, len(values)) + b''.join(
        struct.pack('>i', value) for value in values)


class FrameRfb(protocol.Rfb):
    def __init__(self, ws):
        super().__init__(ws)
        self.inflater = zlib.decompressobj()

    async def read(self, size):
        while len(self.buffer) < size:
            try:
                msg = await self.ws.receive(timeout=15)
            except TimeoutError as error:
                raise AssertionError(
                    f'RFB read stalled: need {size}, buffered {len(self.buffer)}'
                ) from error
            if msg.type != aiohttp.WSMsgType.BINARY:
                raise EOFError(f'expected RFB bytes, received {msg.type}')
            self.buffer.extend(msg.data)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    async def update(self):
        header = await self.read(4)
        assert header[:2] == b'\0\0', header
        count = struct.unpack('>H', header[2:])[0]
        assert 0 < count <= 4096, count
        rectangles = []
        for _ in range(count):
            x, y, width, height, encoding = struct.unpack('>HHHHi', await self.read(12))
            if encoding == -239:
                pixels = await self.read(width * height * 4)
                mask = await self.read(((width + 7) // 8) * height)
                rectangles.append({'encoding': encoding, 'size': (width, height),
                                   'pixels': pixels, 'mask': mask})
                continue
            assert encoding in (0, 6), encoding
            assert 0 < width <= self.size[0] and 0 < height <= self.size[1]
            assert x + width <= self.size[0] and y + height <= self.size[1]
            pixel_bytes = width * height * 4
            compressed_length = 0
            if encoding == 6:
                compressed_length = struct.unpack('>I', await self.read(4))[0]
                # A bounded allowance over the pixel count, independent of
                # the plugin's packet/queue limit constants.
                assert 0 < compressed_length < pixel_bytes + pixel_bytes // 50 + 65536
                pixels = self.inflater.decompress(
                    await self.read(compressed_length), pixel_bytes + 1)
                assert len(pixels) == pixel_bytes and not self.inflater.eof
                assert not self.inflater.unconsumed_tail and not self.inflater.unused_data
            else:
                pixels = await self.read(pixel_bytes)
            rectangles.append({'encoding': encoding, 'rect': (x, y, width, height),
                               'pixels': pixels, 'compressed_length': compressed_length})
        return rectangles


async def checks(url, qt_log=None):
    results = []
    reference = None
    async with aiohttp.ClientSession() as session:
        async def connect():
            rfb = FrameRfb(await session.ws_connect(url, max_msg_size=32 * MIB))
            await rfb.init(expected_size=(WIDTH, HEIGHT))
            return rfb

        async def check(name, body):
            try:
                detail = await body() or {}
                results.append({'check': name, 'status': 'pass', **detail})
            except Exception as error:
                results.append({'check': name, 'status': 'fail', 'error': repr(error)})
            print(f'{name}: {results[-1]["status"]}', flush=True)

        def verify_cursor(cursor):
            assert cursor['encoding'] == -239
            assert cursor['size'] == CURSOR_SIZE, cursor['size']
            assert cursor['pixels'] == CURSOR_PIXELS, 'cursor pixels differ from fixture'
            assert cursor['mask'] == CURSOR_MASK, 'cursor mask differs from fixture'

        async def raw_full_frame():
            nonlocal reference
            rfb = await connect()
            try:
                await rfb.ws.send_bytes(encodings(0, -239)
                                        + protocol.request(0, 0, WIDTH, HEIGHT))
                update = await rfb.update()
                assert [item['encoding'] for item in update] == [0, -239]
                pixels, cursor = update
                assert pixels['rect'] == (0, 0, WIDTH, HEIGHT)
                verify_cursor(cursor)
                reference = pixels['pixels']
                assert len(reference) == PIXEL_BYTES
                assert len(set(reference[:4096])) > 200, 'fixture is not high entropy'
                return {'size': (WIDTH, HEIGHT), 'raw_bytes': len(reference),
                        'sha256': hashlib.sha256(reference).hexdigest(),
                        'cursor_wire_bytes': 12 + len(cursor['pixels']) + len(cursor['mask'])}
            finally:
                await rfb.ws.close()

        async def zlib_cursor_full_frame():
            assert reference is not None
            rfb = await connect()
            try:
                await rfb.ws.send_bytes(encodings(6, -239, 0)
                                        + protocol.request(0, 0, WIDTH, HEIGHT))
                update = await rfb.update()
                assert [item['encoding'] for item in update] == [6, -239]
                pixels, cursor = update
                assert pixels['rect'] == (0, 0, WIDTH, HEIGHT)
                assert pixels['pixels'] == reference, 'zlib pixels differ from Raw'
                verify_cursor(cursor)
                # This frame crosses the old 12 MiB stage limit.
                assert pixels['compressed_length'] > 12 * MIB
                return {'compressed_bytes': pixels['compressed_length'],
                        'pixels_compared': len(reference), 'cursor_size': cursor['size'],
                        'cursor_pixels_compared': len(cursor['pixels'])}
            finally:
                await rfb.ws.close()

        async def paused_reader():
            assert reference is not None
            area = (100, 100, 32, 32)
            expected = b''.join(reference[((area[1] + row) * WIDTH + area[0]) * 4:
                                          ((area[1] + row) * WIDTH + area[0] + area[2]) * 4]
                                for row in range(area[3]))
            log_start = qt_log.stat().st_size if qt_log else None
            rfb = await connect()
            try:
                await rfb.ws.send_bytes(encodings(0))
                transport = rfb.ws._response.connection.transport
                transport.pause_reading()
                try:
                    for _ in range(8):
                        await rfb.ws.send_bytes(protocol.request(0, 0, WIDTH, HEIGHT))
                        await asyncio.sleep(.05)
                    await rfb.ws.send_bytes(encodings(-239, 0) + protocol.request(*area))
                    await asyncio.sleep(.1)
                finally:
                    transport.resume_reading()
                frames = 0
                async with asyncio.timeout(25):
                    while True:
                        update = await rfb.update()
                        frames += 1
                        if [item['encoding'] for item in update] == [0, -239] and \
                                update[0]['rect'] == area:
                            assert update[0]['pixels'] == expected
                            verify_cursor(update[1])
                            break
                        assert frames < 8, 'full requests did not coalesce'
                if qt_log:
                    with qt_log.open('rb') as log:
                        log.seek(log_start)
                        messages = log.read().decode(errors='replace')
                    assert 'framebuffer deferred' in messages
                    assert 'framebuffer resumed' in messages
                return {'updates_received': frames, 'small_pixels_compared': len(expected),
                        'queue_gate_observed': bool(qt_log)}
            finally:
                await rfb.ws.close()

        await check('ultrawide_startup_full_raw_and_cursor', raw_full_frame)
        await check('incompressible_zlib_cursor_matches_raw', zlib_cursor_full_frame)
        await check('ultrawide_paused_reader_recovers', paused_reader)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--qt-log', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = asyncio.run(checks(args.url, args.qt_log))
    (args.output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    return int(any(row['status'] != 'pass' for row in results))


if __name__ == '__main__':
    raise SystemExit(main())
