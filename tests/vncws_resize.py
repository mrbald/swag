#!/usr/bin/env python3
"""Independent single-screen RFB resize contract over actual WebSockets."""
import asyncio
import json
import struct

import aiohttp
import vncws_protocol as protocol

EXTENDED_SIZE = -308


def set_size(width, height, *, screens=None, flags=0):
    if screens is None:
        screens = [(0, 0, 0, width, height, flags)]
    return (struct.pack('>BBHHBB', 251, 0, width, height, len(screens), 0)
            + b''.join(struct.pack('>IHHHHI', *screen) for screen in screens))


class ResizeRfb(protocol.Rfb):
    async def update(self):
        header = await self.read(4)
        assert header[:2] == b'\0\0', header
        count = struct.unpack('>H', header[2:])[0]
        assert count <= 4096
        rectangles = []
        for _ in range(count):
            x, y, w, h, encoding = struct.unpack('>HHHHi', await self.read(12))
            if encoding == EXTENDED_SIZE:
                assert count == 1, 'desktop descriptor must not share a pixel update'
                screen_count = (await self.read(4))[0]
                assert screen_count <= 16
                screens = [struct.unpack('>IHHHHI', await self.read(16))
                           for _ in range(screen_count)]
                if y == 0:
                    assert len(screens) == 1 and screens[0][1:5] == (0, 0, w, h), screens
                    self.size = (w, h)
                return {'reason': x, 'status': y, 'size': (w,h), 'screens': screens}
            assert encoding == 0, encoding
            assert 0 < w <= self.size[0] and 0 < h <= self.size[1]
            assert x+w <= self.size[0] and y+h <= self.size[1], (x,y,w,h,self.size)
            await self.read(w*h*4)
            rectangles.append((x,y,w,h))
        return {'pixels': rectangles}

    async def pixels(self, x=0, y=0, width=8, height=8):
        await self.ws.send_bytes(protocol.request(x, y, width, height))
        descriptor = await self.update()
        assert descriptor['reason'] == 0 and descriptor['status'] == 0, descriptor
        await self.ws.send_bytes(struct.pack('>BBHHHH',3,1,x,y,width,height))
        return await self.update()

    async def resize(self, width, height, **kwargs):
        await self.ws.send_bytes(set_size(width, height, **kwargs))
        reply = await self.update()
        assert reply['reason'] == 1, reply
        return reply


async def checks(url, output):
    results = []
    async with aiohttp.ClientSession() as session:
        async def connect(negotiate=True):
            rfb = ResizeRfb(await session.ws_connect(url, max_msg_size=16*protocol.MIB))
            await rfb.init(expected_size=None)
            if negotiate:
                await rfb.ws.send_bytes(struct.pack('>BBHii', 2, 0, 2, EXTENDED_SIZE, 0))
                assert (await rfb.pixels())['pixels']
            return rfb

        async def check(name, test):
            rfb = None
            try:
                rfb = await connect()
                detail = await test(rfb) or {}
                results.append(dict(check=name, status='pass', **detail))
            except Exception as error:
                results.append(dict(check=name, status='fail', error=repr(error)))
            finally:
                if rfb:
                    await rfb.ws.close()
            print(f'{name}: {results[-1]["status"]}', flush=True)
            (output/'results.json').write_text(json.dumps(results, indent=2)+'\n')

        async def initial(rfb):
            assert rfb.size == (1280, 800), rfb.size
            return {'initial_size':rfb.size}

        async def fragmented(rfb):
            for byte in set_size(960, 640):
                await rfb.ws.send_bytes(bytes([byte]))
                await asyncio.sleep(.002)
            reply = await rfb.update()
            assert (reply['reason'],reply['status'],reply['size']) == (1,0,(960,640)), reply
            pixels = await rfb.pixels(958,638,20,20)
            assert pixels == {'pixels':[(958,638,2,2)]}, pixels
            assert await rfb.pixels(1000,700,20,20) == {'pixels':[]}
            return {'shrink_clipping':True}

        async def ordered(rfb):
            sizes = [(1200,800),(800,600),(800,600),(1600,1000)]
            await rfb.ws.send_bytes(b''.join(set_size(*size) for size in sizes))
            for size in sizes:
                reply = await rfb.update()
                assert (reply['reason'],reply['status'],reply['size']) == (1,0,size), reply
            assert (await rfb.pixels())['pixels']
            return {'acknowledged_in_order':sizes}

        async def rejected_dimensions(rfb):
            before = rfb.size
            for size in [(0,800),(63,800),(4096,800),(2048,2048)]:
                reply = await rfb.resize(*size)
                assert reply['status'] == 3, reply
                assert rfb.size == before
            assert (await rfb.pixels())['pixels']
            return {'unchanged_size':before}

        async def rejected_layout(rfb):
            before = rfb.size
            for screens in [[], [(0,1,0,800,600,0)], [(0,0,0,799,600,0)],
                            [(0,0,0,400,600,0),(1,400,0,400,600,0)]]:
                reply = await rfb.resize(800,600,screens=screens)
                assert reply['status'] == 3, reply
                assert rfb.size == before
            assert (await rfb.pixels())['pixels']

        async def flags_and_takeover(rfb):
            reply = await rfb.resize(1000,700,flags=0x80000001)
            assert reply['status'] == 0 and reply['screens'][0][-1] == 0x80000001, reply
            newer = await connect(negotiate=False)
            try:
                assert newer.size == (1000,700)
                await rfb.closed()
                await newer.ws.send_bytes(struct.pack('>BBHii',2,0,2,EXTENDED_SIZE,0)
                                          + protocol.request())
                reply = await newer.update()
                assert reply['screens'][0][-1] == 0x80000001, reply
                await newer.ws.send_bytes(struct.pack('>BBHHHH',3,1,0,0,8,8))
                assert (await newer.update())['pixels']
                reply = await newer.resize(1280,800)
                assert reply['status'] == 0
            finally:
                await newer.ws.close()

        async def repeated_nonincremental(rfb):
            for _ in range(3):
                assert (await rfb.pixels())['pixels']

        async def unnegotiated(_rfb):
            other = await connect(negotiate=False)
            try:
                await other.ws.send_bytes(set_size(800,600))
                await other.closed()
            finally:
                await other.ws.close()

        async def hint_without_descriptor(_rfb):
            other = await connect(negotiate=False)
            try:
                await other.ws.send_bytes(struct.pack('>BBHii',2,0,2,EXTENDED_SIZE,0)
                                          + set_size(800,600))
                await other.closed()
            finally:
                await other.ws.close()

        async def withdrawn(rfb):
            await rfb.ws.send_bytes(struct.pack('>BBHi',2,0,1,0)+set_size(800,600))
            await rfb.closed()

        await check('initial_single_screen_descriptor', initial)
        await check('fragmented_resize_and_shrink_clipping', fragmented)
        await check('coalesced_resize_and_noop_ack_order', ordered)
        await check('invalid_dimensions_leave_display_usable', rejected_dimensions)
        await check('unsupported_layouts_leave_display_usable', rejected_layout)
        await check('opaque_flags_and_takeover_size_preserved', flags_and_takeover)
        await check('nonincremental_requests_each_report_layout', repeated_nonincremental)
        await check('unnegotiated_resize_rejected', unnegotiated)
        await check('resize_before_initial_descriptor_rejected', hint_without_descriptor)
        await check('resize_after_capability_withdrawal_rejected', withdrawn)
    return results


if __name__ == '__main__':
    raise SystemExit(protocol.main(checks, protocol.ROOT/'artifacts/qt-ws-resize/protocol'))
