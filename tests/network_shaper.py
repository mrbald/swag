#!/usr/bin/env python3
"""Test-only bounded TCP relay: synthetic added RTT and per-direction byte rate.

Run in a swag-* sidecar between authenticated nginx and the session manager.
Never inspect or log payloads. Each connection snapshots profile.json. This is
an application-stream delay/rate model, not packet loss or a full WAN emulator.
"""
import asyncio
import errno
import json
import os
from pathlib import Path
import time
import uuid

ROOT = Path(os.environ.get('SWAG_PROFILE_DIR', '/profiles'))
RUN_ID = os.environ.get('SWAG_PROFILE_RUN_ID', 'manual')


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value)+'\n')
    temporary.replace(path)


async def paced(reader, writer, *, delay, rate, stats, direction):
    # 256 KiB queued, plus one 16 KiB producer chunk and StreamReader's
    # bounded internal buffer. No unbounded application-side accumulation.
    queue = asyncio.Queue(maxsize=16)

    async def read():
        try:
            while data := await reader.read(16384):
                await queue.put((time.monotonic(), data))
        except Exception as error:
            stats[direction+'_read_error'] = type(error).__name__
            stats[direction+'_read_errno'] = getattr(error, 'errno', None)
        await queue.put(None)

    task = asyncio.create_task(read())
    deadline = time.monotonic()
    phase = 'queue_read'
    try:
        while True:
            phase = 'queue_read'
            item = await queue.get()
            if item is None:
                break
            received, data = item
            deadline = max(deadline, received + delay)
            if rate:
                deadline += len(data) / rate
            phase = 'pace'
            await asyncio.sleep(max(0, deadline - time.monotonic()))
            phase = 'write'
            writer.write(data)
            phase = 'drain'
            await writer.drain()
            stats[direction] += len(data)
        phase = 'write_eof_support'
        if writer.can_write_eof():
            phase = 'write_eof'
            try:
                writer.write_eof()
            except OSError as error:
                if error.errno != errno.ENOTCONN:
                    raise
                # The peer has already closed after all queued payload bytes
                # were forwarded. There is no remaining write side to close.
                stats[direction+'_eof_already_closed'] = True
            else:
                phase = 'eof_drain'
                await asyncio.wait_for(writer.drain(), 1)
    except Exception as error:
        stats.setdefault('error_phase', phase)
        stats.setdefault('error_direction', direction)
        stats.setdefault('error_errno', getattr(error, 'errno', None))
        raise
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def relay(reader, writer):
    upstream = None
    tasks = []
    started = time.monotonic()
    profile = json.loads((ROOT/'profile.json').read_text())
    connection_id = uuid.uuid4().hex
    active = ROOT/f'active-{RUN_ID}-{connection_id}.json'
    stats = {'run_id':RUN_ID, 'profile':profile['name'], 'up_bytes':0, 'down_bytes':0}
    write_json(active, {'run_id':RUN_ID, 'profile':profile['name']})
    relay_phase = 'upstream_connect'
    try:
        source, upstream = await asyncio.wait_for(asyncio.open_connection('swag-host',8080), 5)
        delay = float(profile['rtt_ms']) / 2000
        tasks = [asyncio.create_task(paced(reader,upstream,delay=delay,
                    rate=float(profile['upload_mbps'])*1_000_000/8,stats=stats,direction='up_bytes'),name='up_bytes'),
                 asyncio.create_task(paced(source,writer,delay=delay,
                    rate=float(profile['download_mbps'])*1_000_000/8,stats=stats,direction='down_bytes'),name='down_bytes')]
        relay_phase = 'first_direction_completion'
        completed, pending = await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            if not task.cancelled() and (error := task.exception()) is not None:
                stats['error'] = type(error).__name__
                stats.setdefault('error_phase', 'direction_completion')
                stats.setdefault('error_direction', task.get_name())
                stats.setdefault('error_errno', getattr(error, 'errno', None))
        if pending and 'error' not in stats:
            try:
                relay_phase = 'opposite_direction_drain'
                await asyncio.wait_for(asyncio.gather(*pending), 5)
            except Exception as error:
                stats['error'] = type(error).__name__
                stats.setdefault('error_phase', relay_phase)
                stats.setdefault('error_direction', ','.join(task.get_name() for task in pending))
                stats.setdefault('error_errno', getattr(error, 'errno', None))
    except (OSError,TimeoutError,ConnectionError) as error:
        stats['error'] = type(error).__name__
        stats.setdefault('error_phase', relay_phase)
        stats.setdefault('error_direction', 'upstream' if not tasks else 'relay')
        stats.setdefault('error_errno', getattr(error, 'errno', None))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        for stream in [writer,upstream]:
            if stream:
                stream.close()
                try:
                    await asyncio.wait_for(stream.wait_closed(),1)
                except (OSError,TimeoutError):
                    pass
        stats['elapsed_seconds'] = round(time.monotonic()-started,6)
        write_json(ROOT/f'connection-{RUN_ID}-{connection_id}.json', stats)
        active.unlink()


async def main():
    server = await asyncio.start_server(relay,'0.0.0.0',8089,limit=65536)
    print('test stream shaper ready',flush=True)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
