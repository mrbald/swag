#!/usr/bin/env python3
"""Independent Extended Clipboard wire tests, including noVNC zlib framing."""
import asyncio
import json
import struct
import zlib

import aiohttp
import vncws_protocol as protocol

ENCODING=0xC0A1E5CE
TEXT=1
CAPS=1<<24
REQUEST=1<<25
PEEK=1<<26
NOTIFY=1<<27
PROVIDE=1<<28
ACTIONS=CAPS|REQUEST|PEEK|NOTIFY|PROVIDE


def extended(flags,body=b''):
    payload=struct.pack('>I',flags)+body
    return struct.pack('>B3xi',6,-len(payload))+payload


def provided(text,*,flush=False,raw=None):
    payload=(text.replace('\r\n','\n').replace('\r','\n').replace('\n','\r\n').encode()+b'\0') if raw is None else raw
    plain=struct.pack('>I',len(payload))+payload
    if flush:
        compressor=zlib.compressobj()
        data=compressor.compress(plain)+compressor.flush(zlib.Z_FULL_FLUSH)
    else:
        data=zlib.compress(plain)
    return extended(PROVIDE|TEXT,data)


class ClipboardRfb(protocol.Rfb):
    async def message(self):
        header=await self.read(8)
        assert header[0]==3, header
        length=struct.unpack('>i',header[4:])[0]
        assert abs(length)<=protocol.MIB
        payload=await self.read(abs(length))
        if length>=0:
            return {'legacy':payload.decode('latin1')}
        assert len(payload)>=4
        flags=struct.unpack('>I',payload[:4])[0]
        return {'flags':flags,'body':payload[4:]}

    async def action(self,wanted):
        for _ in range(8):
            message=await self.message()
            flags=message.get('flags',0)
            if (wanted==CAPS and flags&CAPS) or flags&0xFF000000==wanted:
                return message
            assert message.get('flags',0)&0xFF000000==NOTIFY, message
        raise AssertionError('too many unsolicited notifications')

    async def text(self):
        await self.ws.send_bytes(extended(REQUEST|TEXT))
        message=await self.action(PROVIDE)
        assert message['flags']==PROVIDE|TEXT, message
        inflater=zlib.decompressobj()
        plain=inflater.decompress(message['body'],protocol.MIB+5)
        assert not inflater.unconsumed_tail and not inflater.unused_data
        assert len(plain)>=5
        size=struct.unpack('>I',plain[:4])[0]
        assert size==len(plain)-4 and plain[-1:]==b'\0'
        return plain[4:-1].decode('utf-8').replace('\r\n','\n')


async def checks(url,output):
    results=[]
    async with aiohttp.ClientSession() as session:
        async def connect(negotiate=True,actions=ACTIONS):
            rfb=ClipboardRfb(await session.ws_connect(url,max_msg_size=16*protocol.MIB))
            await rfb.init()
            if negotiate:
                await rfb.ws.send_bytes(struct.pack('>BBHI',2,0,1,ENCODING))
                caps=await rfb.action(CAPS)
                assert caps['flags']==ACTIONS|TEXT and caps['body']==struct.pack('>I',0), caps
                await rfb.ws.send_bytes(extended(actions|TEXT,struct.pack('>I',0)))
            return rfb

        async def check(name,test,negotiate=True,actions=ACTIONS):
            rfb=None
            try:
                rfb=await connect(negotiate,actions)
                details=await test(rfb) or {}
                results.append(dict(check=name,status='pass',**details))
            except Exception as error:
                results.append(dict(check=name,status='fail',error=repr(error)))
            finally:
                if rfb:
                    await rfb.ws.close()
            print(f'{name}: {results[-1]["status"]}',flush=True)
            (output/'results.json').write_text(json.dumps(results,indent=2)+'\n')

        async def roundtrip(rfb,flush=False,fragmented=False):
            text='€ Zürich Łódź Ж 東京 😀\nCafe\u0301 العربية'
            await rfb.ws.send_bytes(extended(NOTIFY|TEXT))
            assert (await rfb.action(REQUEST))['flags']==REQUEST|TEXT
            packet=provided(text,flush=flush)
            if fragmented:
                for byte in packet:
                    await rfb.ws.send_bytes(bytes([byte]))
                    await asyncio.sleep(.001)
            else:
                await rfb.ws.send_bytes(packet)
            assert await rfb.text()==text
            return {'utf8_bytes':len(text.encode()),'novnc_flush':flush}

        async def empty(rfb):
            await rfb.ws.send_bytes(provided('stale')+provided(''))
            assert await rfb.text()==''
            await rfb.ws.send_bytes(provided('stale')+extended(NOTIFY))
            assert await rfb.text()==''

        async def peek_and_renegotiate(rfb):
            await rfb.ws.send_bytes(extended(PEEK))
            assert (await rfb.action(NOTIFY))['flags']==NOTIFY|TEXT
            await rfb.ws.send_bytes(struct.pack('>BBHI',2,0,1,ENCODING))
            assert (await rfb.action(CAPS))['flags']==ACTIONS|TEXT

        async def legacy_input(rfb):
            text='Café £ déjà vu'
            payload=text.encode('latin1')
            await rfb.ws.send_bytes(struct.pack('>B3xI',6,len(payload))+payload)
            assert await rfb.text()==text

        async def legacy_copy_button(rfb):
            state=json.loads((output/'state.json').read_text())
            x,y,w,h=state['geometry']['copy_button']
            await rfb.ws.send_bytes(struct.pack('>BBHH',5,1,int(x+w/2),int(y+h/2))
                                   +struct.pack('>BBHH',5,0,int(x+w/2),int(y+h/2)))
            reply=await rfb.message()
            assert reply=={'legacy':state['editor_text'].encode('latin1','replace').decode('latin1')},reply

        async def rejected(rfb,packet):
            await rfb.ws.send_bytes(packet)
            await rfb.closed()

        async def request_only_peer(rfb):
            await rfb.ws.send_bytes(extended(NOTIFY|TEXT))
            assert (await rfb.action(REQUEST))['flags']==REQUEST|TEXT
            await rfb.ws.send_bytes(extended(REQUEST|TEXT)+extended(PEEK))
            try:
                message=await rfb.ws.receive(timeout=.3)
            except TimeoutError:
                return {'unsupported_notify_and_provide_suppressed':True}
            raise AssertionError(f'unadvertised action emitted: {message.type}')

        async def notify_provide_peer(rfb):
            await rfb.ws.send_bytes(extended(NOTIFY|TEXT))
            # A peer willing to receive Provide but not Request must not receive
            # Request in response to Notify. text() rejects that wrong action.
            await rfb.text()
            await rfb.ws.send_bytes(extended(PEEK))
            assert (await rfb.action(NOTIFY))['flags']==NOTIFY|TEXT

        await check('utf8_finished_zlib_roundtrip',roundtrip)
        await check('utf8_novnc_full_flush_roundtrip',lambda r:roundtrip(r,flush=True))
        await check('fragmented_extended_payload',lambda r:roundtrip(r,flush=True,fragmented=True))
        await check('empty_provide_and_notify_clear',empty)
        await check('peek_and_repeated_capability_exchange',peek_and_renegotiate)
        await check('latin1_input_still_accepted',legacy_input)
        await check('legacy_viewer_receives_latin1_copy',legacy_copy_button,negotiate=False)
        await check('peer_receive_request_capability_honored',request_only_peer,actions=CAPS|REQUEST)
        await check('peer_receive_notify_provide_capabilities_honored',notify_provide_peer,actions=CAPS|NOTIFY|PROVIDE)
        bad=[('invalid_utf8',provided('',raw=b'\xff\0')),
             ('missing_text_terminator',provided('',raw=b'abc')),
             ('compressed_garbage',extended(PROVIDE|TEXT,b'not zlib')),
             ('truncated_zlib',provided('hello')[:-3]),
             ('multiple_actions',extended(REQUEST|PROVIDE|TEXT)),
             ('short_extended_body',struct.pack('>B3xi',6,-3)+b'abc'),
             ('oversized_wire_length',struct.pack('>B3xi',6,-protocol.MIB-1)),
             ('minimum_signed_length',struct.pack('>B3xi',6,-2147483648)),
             ('inflated_limit',provided('A'*(protocol.MIB+1))),
             ('declared_size_limit',extended(PROVIDE|TEXT,zlib.compress(struct.pack('>I',0x7fffffff)+b'\0'))),
             ('inflated_trailing_bytes',extended(PROVIDE|TEXT,zlib.compress(struct.pack('>I',2)+b'a\0junk'))),
             ('compressed_trailing_bytes',extended(PROVIDE|TEXT,zlib.compress(struct.pack('>I',2)+b'a\0')+b'junk'))]
        # Correct the truncated packet length, otherwise it is merely incomplete
        # transport input and should legitimately wait for the remaining bytes.
        name,packet=bad[3]
        bad[3]=(name,struct.pack('>B3xi',6,-(len(packet)-8))+packet[8:])
        for name,packet in bad:
            await check(name,lambda r,p=packet:rejected(r,p))
        await check('unnegotiated_extension_rejected',lambda r:rejected(r,extended(PEEK)),negotiate=False)
        await check('healthy_after_clipboard_faults',roundtrip)
    return results


if __name__=='__main__':
    raise SystemExit(protocol.main(checks,protocol.ROOT/'artifacts/qt-ws-clipboard/protocol'))
