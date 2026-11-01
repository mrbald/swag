#!/usr/bin/env python3
"""Independent RFB zlib decoding, negotiation and slow-reader recovery checks."""
import asyncio
import json
import struct
import zlib

import aiohttp
import vncws_protocol as protocol
from vncws_resize import set_size


def encodings(*values):
    return struct.pack('>BBH', 2, 0, len(values)) + b''.join(struct.pack('>i', x) for x in values)


def pixel_format(red=16, blue=0, depth=24):
    return b'\0\0\0\0' + struct.pack('>BBBBHHHBBBxxx', 32, depth, 0, 1,
                                      255, 255, 255, red, 8, blue)


class CompressedRfb(protocol.Rfb):
    def __init__(self, ws):
        super().__init__(ws)
        self.inflater = zlib.decompressobj()
        self.wire_bytes = 0
        self.compressed_blocks = []

    async def update(self):
        header = await self.read(4)
        assert header[:2] == b'\0\0', header
        count = struct.unpack('>H', header[2:])[0]
        assert count <= 4096
        self.wire_bytes += 4
        rectangles = []
        for _ in range(count):
            x, y, w, h, encoding = struct.unpack('>HHHHi', await self.read(12))
            self.wire_bytes += 12
            if encoding == -308:
                assert count == 1
                screens = (await self.read(4))[0]
                layout = await self.read(16*screens)
                if y == 0:
                    self.size = (w,h)
                return {'reason':x,'status':y,'size':(w,h),'layout':layout}
            assert encoding in (0,6), encoding
            assert 0 < w <= self.size[0] and 0 < h <= self.size[1]
            assert x+w <= self.size[0] and y+h <= self.size[1]
            size = w*h*4
            if encoding == 6:
                length = struct.unpack('>I', await self.read(4))[0]
                assert 0 < length < 9*protocol.MIB
                block = await self.read(length)
                pixels = self.inflater.decompress(block, size+1)
                assert len(pixels) == size
                assert not self.inflater.eof, 'stream ended before connection ended'
                assert not self.inflater.unconsumed_tail and not self.inflater.unused_data
                self.compressed_blocks.append(block)
                self.wire_bytes += 4+length
            else:
                pixels = await self.read(size)
                self.wire_bytes += size
            rectangles.append({'rect':(x,y,w,h),'encoding':encoding,'pixels':pixels})
        return rectangles

    async def pixels(self, area=(80,180,96,64), expected=6):
        await self.ws.send_bytes(protocol.request(*area))
        result = await self.update()
        assert isinstance(result,list) and result
        assert all(r['encoding']==expected for r in result)
        assert [r['rect'] for r in result] == [area]
        return b''.join(r['pixels'] for r in result)


async def checks(url, output):
    results = []
    async with aiohttp.ClientSession() as session:
        async def connect():
            rfb = CompressedRfb(await session.ws_connect(url,max_msg_size=16*protocol.MIB))
            await rfb.init(expected_size=None)
            return rfb

        async def check(name, test):
            rfb = None
            try:
                rfb = await connect()
                detail = await test(rfb) or {}
                results.append(dict(check=name,status='pass',**detail))
            except Exception as error:
                results.append(dict(check=name,status='fail',error=repr(error)))
            finally:
                if rfb:
                    await rfb.ws.close()
            (output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
            print(f'{name}: {results[-1]["status"]}',flush=True)

        async def equivalence(rfb, red=16, blue=0, depth=24):
            await rfb.ws.send_bytes(pixel_format(red,blue,depth))
            raw = await rfb.pixels(expected=0)
            assert any(a!=b for a,b in zip(raw[0::4],raw[2::4])), 'test region lacks color'
            await rfb.ws.send_bytes(encodings(6,0))
            assert await rfb.pixels() == raw
            return {'pixels_compared':len(raw)//4,'depth':depth,'red_shift':red}

        async def preference(rfb, values, expected):
            await rfb.ws.send_bytes(encodings(*values))
            await rfb.pixels(expected=expected)
            return {'advertised':values,'selected':expected}

        async def dictionary(rfb, withdraw=False):
            await rfb.ws.send_bytes(encodings(6,0))
            first = await rfb.pixels()
            if withdraw:
                await rfb.ws.send_bytes(encodings(0))
                assert await rfb.pixels(expected=0) == first
            await rfb.ws.send_bytes(encodings(6,0))
            assert await rfb.pixels() == first
            # A new inflater cannot decode the second rectangle independently.
            try:
                standalone = zlib.decompressobj().decompress(rfb.compressed_blocks[-1])
            except zlib.error:
                standalone = b''
            assert standalone != first, 'dictionary appears reset between rectangles'
            return {'compressed_lengths':[len(b) for b in rfb.compressed_blocks]}

        async def format_change(rfb):
            await rfb.ws.send_bytes(pixel_format()+encodings(6,0))
            first = await rfb.pixels()
            await rfb.ws.send_bytes(pixel_format(0,16))
            second = await rfb.pixels()
            assert first[0::4] == second[2::4] and first[2::4] == second[0::4]
            assert first[1::4] == second[1::4]

        async def fragmented(rfb):
            for byte in encodings(6,0):
                await rfb.ws.send_bytes(bytes([byte]))
            await rfb.pixels()

        async def large_resize(rfb):
            await rfb.ws.send_bytes(encodings(6,-308,0)+protocol.request())
            assert (await rfb.update())['status'] == 0
            await rfb.ws.send_bytes(struct.pack('>BBHHHH',3,1,0,0,8,8))
            assert (await rfb.update())[0]['encoding'] == 6
            for size in ((2048,1012),(1280,800)):
                await rfb.ws.send_bytes(set_size(*size))
                assert (await rfb.update())['status'] == 0
                await asyncio.sleep(.25)  # Let ordinary Qt layout/repaint events settle.
                await rfb.ws.send_bytes(encodings(6,0))
                before = rfb.wire_bytes
                compressed = await rfb.pixels((0,0,*size))
                wire = rfb.wire_bytes-before
                assert wire < len(compressed)//4, (wire,len(compressed))
                await rfb.ws.send_bytes(encodings(0))
                assert await rfb.pixels((0,0,*size),expected=0) == compressed
                await rfb.ws.send_bytes(encodings(6,-308,0)+protocol.request())
                assert (await rfb.update())['status'] == 0
            return {'last_raw_bytes':len(compressed),'last_compressed_wire_bytes':wire}

        async def slow_reader(rfb):
            # Stop TCP receive, not just application message consumption. Send
            # spaced requests so this exercises transport backpressure as well
            # as timer coalescing. Raw ensures a large queued update.
            log_start = (output/'qt.log').stat().st_size
            await rfb.ws.send_bytes(encodings(0))
            transport = rfb.ws._response.connection.transport
            transport.pause_reading()
            try:
                for _ in range(20):
                    await rfb.ws.send_bytes(protocol.request(0,0,*rfb.size))
                    await asyncio.sleep(.05)
                area = (7,9,13,11)
                await rfb.ws.send_bytes(protocol.request(*area))
                await asyncio.sleep(.1)
            finally:
                transport.resume_reading()
            frames = 0
            before = rfb.wire_bytes
            async with asyncio.timeout(15):
                while True:
                    update = await rfb.update()
                    frames += 1
                    if [r['rect'] for r in update] == [area]:
                        break
                    assert frames < 20, 'requests did not coalesce under congestion'
            received = rfb.wire_bytes-before
            assert received < 24*protocol.MIB, received
            await rfb.ws.send_bytes(encodings(6,0))
            assert await rfb.pixels()
            # Logs are observations only; they prove the queue gate was reached.
            with (output/'qt.log').open('rb') as stream:
                stream.seek(log_start)
                log = stream.read().decode(errors='replace')
            assert 'framebuffer deferred' in log, 'transport gate was not exercised'
            assert 'framebuffer resumed' in log, 'transport gate did not resume'
            return {'updates_received':frames,'wire_bytes_after_pause':received}

        await check('default_raw',lambda r:preference(r,(),0))
        await check('zlib_matches_raw_bgr24',equivalence)
        await check('zlib_matches_raw_rgb24',lambda r:equivalence(r,0,16))
        await check('zlib_matches_raw_depth32',lambda r:equivalence(r,16,0,32))
        await check('raw_preference',lambda r:preference(r,(0,6),0))
        await check('unknown_hint_fallback',lambda r:preference(r,(-9999,),0))
        await check('zlib_after_unknown_hint',lambda r:preference(r,(-9999,6,0),6))
        await check('persistent_dictionary',dictionary)
        await check('withdraw_and_resume_dictionary',lambda r:dictionary(r,True))
        await check('pixel_format_change_same_stream',format_change)
        await check('fragmented_encoding_list',fragmented)
        await check('maximum_frame_resize_same_stream',large_resize)
        await check('slow_reader_coalesces_and_recovers',slow_reader)
        await check('fresh_connection_after_pressure',equivalence)
    return results


if __name__ == '__main__':
    raise SystemExit(protocol.main(checks_fn=checks,
        default_output=protocol.ROOT/'artifacts/qt-ws-compression/protocol'))
