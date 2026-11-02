#!/usr/bin/env python3
"""Independent Cursor plus pixel FramebufferUpdate checks on a running probe."""
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


AREA = (80, 180, 96, 64)  # Static color grid, away from the initial pointer.


def encodings(*values):
    return (struct.pack('>BBH', 2, 0, len(values))
            + b''.join(struct.pack('>i', value) for value in values))


def request(*area, incremental=False):
    return struct.pack('>BBHHHH', 3, int(incremental), *area)


class CursorRfb(protocol.Rfb):
    def __init__(self, ws):
        super().__init__(ws)
        self.inflater = zlib.decompressobj()

    async def update(self):
        header = await self.read(4)
        assert header[:2] == b'\0\0', header
        count = struct.unpack('>H', header[2:])[0]
        assert count <= 4096, count
        rectangles = []
        for _ in range(count):
            x, y, width, height, encoding = struct.unpack('>HHHHi', await self.read(12))
            if encoding == -239:
                # The Cursor rectangle uses hotspot coordinates, not screen position.
                pixels = await self.read(width * height * 4)
                mask = await self.read(((width + 7) // 8) * height)
                rectangles.append(dict(encoding=encoding, hotspot=(x, y),
                                       size=(width, height), pixels=pixels, mask=mask))
                continue
            assert encoding in (0, 6), encoding
            assert width and height and x + width <= self.size[0]
            assert y + height <= self.size[1]
            pixel_count = width * height * 4
            if encoding == 6:
                length = struct.unpack('>I', await self.read(4))[0]
                assert 0 < length < 9 * protocol.MIB, length
                pixels = self.inflater.decompress(await self.read(length), pixel_count + 1)
                assert len(pixels) == pixel_count
                assert not self.inflater.eof
                assert not self.inflater.unconsumed_tail and not self.inflater.unused_data
            else:
                pixels = await self.read(pixel_count)
            rectangles.append(dict(encoding=encoding, rect=(x, y, width, height),
                                   pixels=pixels))
        return rectangles


async def checks(url):
    results = []
    async with aiohttp.ClientSession() as session:
        async def connect():
            rfb = CursorRfb(await session.ws_connect(url, max_msg_size=16 * protocol.MIB))
            await rfb.init(expected_size=None)
            return rfb

        async def check(name, function):
            try:
                detail = await function() or {}
                results.append(dict(check=name, status='pass', **detail))
            except Exception as error:
                results.append(dict(check=name, status='fail', error=repr(error)))
            print(f'{name}: {results[-1]["status"]}', flush=True)

        async def baseline_pixels():
            rfb = await connect()
            try:
                await rfb.ws.send_bytes(encodings(0) + request(*AREA))
                update = await rfb.update()
                assert len(update) == 1 and update[0]['encoding'] == 0, update
                assert update[0]['rect'] == AREA, update
                pixels = update[0]['pixels']
                assert any(a != b for a, b in zip(pixels[0::4], pixels[2::4])), \
                    'reference region lacks color'
                return pixels
            finally:
                await rfb.ws.close()

        async def assert_quiet(rfb):
            assert not rfb.buffer, 'unrequested RFB bytes already buffered'
            try:
                message = await rfb.ws.receive(timeout=.12)
            except asyncio.TimeoutError:
                return
            raise AssertionError(f'unrequested WebSocket message: {message.type}')

        async def mode(pixel_encoding):
            reference = await baseline_pixels()
            rfb = await connect()
            try:
                preferences = (-239, pixel_encoding, 0) if pixel_encoding == 6 else (-239, 0, 6)
                await rfb.ws.send_bytes(encodings(*preferences) + request(*AREA))
                first = await rfb.update()
                assert len(first) == 2, [(r['encoding'], r.get('rect')) for r in first]
                assert [r['encoding'] for r in first] == [pixel_encoding, -239], first
                pixel, cursor = first
                assert pixel['rect'] == AREA and pixel['pixels'] == reference, \
                    'batched pixel bytes differ from no-cursor reference'
                assert cursor['size'][0] > 0 and cursor['size'][1] > 0
                assert len(cursor['mask']) == ((cursor['size'][0] + 7) // 8) * cursor['size'][1]

                # Withdrawal before a request must suppress a pending Cursor reply.
                await rfb.ws.send_bytes(encodings(pixel_encoding) + request(*AREA))
                pixel_only = await rfb.update()
                assert len(pixel_only) == 1 and pixel_only[0]['encoding'] == pixel_encoding
                assert pixel_only[0]['rect'] == AREA and pixel_only[0]['pixels'] == reference

                # Re-enabling marks Cursor dirty; an incremental request with no
                # intersecting pixel dirt returns only that pseudo-rectangle.
                await rfb.ws.send_bytes(encodings(*preferences)
                                        + request(*AREA, incremental=True))
                cursor_only = await rfb.update()
                assert len(cursor_only) == 1 and cursor_only[0]['encoding'] == -239, \
                    [(r['encoding'], r.get('rect')) for r in cursor_only]
                return dict(pixel_encoding=pixel_encoding, pixels_compared=len(reference),
                            pixel_sha256=hashlib.sha256(reference).hexdigest(),
                            cursor_size=cursor['size'])
            finally:
                await rfb.ws.close()

        async def pending_incremental():
            rfb = await connect()
            try:
                await rfb.ws.send_bytes(encodings(0) + request(*AREA))
                assert len(await rfb.update()) == 1
                await rfb.ws.send_bytes(request(*AREA, incremental=True))
                await assert_quiet(rfb)
                # Negotiation dirties Cursor but makes no new update request.
                await rfb.ws.send_bytes(encodings(-239, 0))
                update = await rfb.update()
                assert len(update) == 1 and update[0]['encoding'] == -239, update
                await assert_quiet(rfb)
            finally:
                await rfb.ws.close()

        async def paused_reader():
            reference = await baseline_pixels()
            rfb = await connect()
            try:
                await rfb.ws.send_bytes(encodings(0))
                transport = rfb.ws._response.connection.transport
                transport.pause_reading()  # Stop TCP receive, not just parsing.
                try:
                    for _ in range(20):
                        await rfb.ws.send_bytes(request(0, 0, *rfb.size))
                        await asyncio.sleep(.05)
                    await rfb.ws.send_bytes(encodings(-239, 0) + request(*AREA))
                    await asyncio.sleep(.1)
                finally:
                    transport.resume_reading()

                frames = 0
                async with asyncio.timeout(15):
                    while True:
                        update = await rfb.update()
                        frames += 1
                        if [r['encoding'] for r in update] == [0, -239] and \
                                update[0]['rect'] == AREA:
                            assert update[0]['pixels'] == reference
                            break
                        assert frames < 20, 'requests did not coalesce while TCP reads paused'
                return dict(updates_received=frames, pixels_compared=len(reference))
            finally:
                await rfb.ws.close()

        await check('raw_cursor_and_pixels_one_update', lambda: mode(0))
        await check('zlib_cursor_and_pixels_one_update', lambda: mode(6))
        await check('incremental_request_waits_for_cursor_dirty', pending_incremental)
        await check('paused_reader_retains_cursor_and_pixels', paused_reader)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='running probe WebSocket URL')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = asyncio.run(checks(args.url))
    (args.output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    return int(any(result['status'] != 'pass' for result in results))


if __name__ == '__main__':
    raise SystemExit(main())
