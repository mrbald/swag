#!/usr/bin/env python3
"""Run inside the probe image: listener defaults and fail-fast configuration."""
import json
import os
from pathlib import Path
import resource
import socket
import subprocess
import tempfile
import time

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
results = []


def environment(platform, state):
    return {**os.environ, 'QT_PLUGIN_PATH': '/opt/swag/plugins',
            'QT_QPA_PLATFORM': platform, 'SWAG_STATE_FILE': str(state),
            'QT_FORCE_STDERR_LOGGING': '1'}


def check(name, function):
    try:
        detail = function() or {}
        results.append(dict(check=name, status='pass', **detail))
    except Exception as error:
        results.append(dict(check=name, status='fail', error=repr(error)))


with tempfile.TemporaryDirectory(prefix='swag-vncws-') as directory:
    state = Path(directory)/'state.json'

    def fails(options, pass_fds=()):
        result = subprocess.run(['/opt/swag/bin/swag-probe'],
                                env=environment('vncws:'+options, state),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=3, pass_fds=pass_fds)
        assert result.returncode != 0, result.stdout
        assert 'vncws:' in result.stdout, result.stdout
        return dict(exit_code=result.returncode, diagnostic=result.stdout.strip())

    for options in ['size=0x800', 'size=4096x4096', 'size=4097x800',
                    'size=3440x1441', 'size=4096x1210',
                    'size=1280x800junk', 'size=800x600:size=800x600',
                    'depth=16', 'port=0', 'port=65536', 'port=5900junk',
                    'addr=localhost', 'addr=999.0.0.1', 'unknown=1',
                    'fd=2', 'fd=-1', 'fd=999999', 'fd=3junk',
                    'fd=3:port=5900', 'fd=3:addr=127.0.0.1', 'fd=3:fd=3']:
        check('reject_'+options, lambda options=options: fails(options))
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        check('occupied_port_fails', lambda: fails(f'port={occupied.getsockname()[1]}'))

    def default_loopback(size=None):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        state.unlink(missing_ok=True)
        with (Path(directory)/'qt.log').open('w+') as log:
            platform = f'vncws:port={port}'
            if size:
                platform += f':size={size}'
            process = subprocess.Popen(['/opt/swag/bin/swag-probe'],
                                       env=environment(platform, state),
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic()+5
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        log.seek(0)
                        raise AssertionError(log.read())
                    listeners = [line.split()[1] for line in Path('/proc/net/tcp').read_text().splitlines()[1:]
                                 if line.split()[3] == '0A' and int(line.split()[1].split(':')[1], 16) == port]
                    if listeners:
                        assert listeners == [f'0100007F:{port:04X}'], listeners
                        return dict(address='127.0.0.1', port=port, size=size or 'default')
                    time.sleep(.05)
                raise TimeoutError('listener readiness')
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    check('default_listener_is_loopback', default_loopback)
    check('ultrawide_startup_listener', lambda: default_loopback('3440x1440'))

    read_fd, write_fd = os.pipe()
    try:
        check('reject_pipe_fd', lambda: fails(f'fd={read_fd}', (read_fd,)))
    finally:
        os.close(read_fd)
        os.close(write_fd)
    with socket.socket() as unbound:
        check('reject_non_listening_fd', lambda: fails(f'fd={unbound.fileno()}', (unbound.fileno(),)))
    with socket.socket() as wildcard:
        wildcard.bind(('0.0.0.0', 0))
        wildcard.listen()
        check('reject_non_loopback_listener', lambda: fails(f'fd={wildcard.fileno()}', (wildcard.fileno(),)))

    def inherited_listener():
        reservation = socket.socket()
        reservation.bind(('127.0.0.1', 0))
        reservation.listen()
        port, fd = reservation.getsockname()[1], reservation.fileno()
        trampoline = os.environ.get('PYTHONPATH') == '/opt/swag/python'
        command = ['/opt/swag/bin/swag-probe']
        if trampoline:
            command = ['/usr/bin/python3.12', '-m', 'swag_session.processes', str(os.getpid()), *command]
        state.unlink(missing_ok=True)
        with (Path(directory)/'inherited.log').open('w+') as log:
            try:
                # Exercise the real parent-death/exec trampoline as well as Popen.
                process = subprocess.Popen(
                    command,
                    env=environment(f'vncws:fd={fd}', state), pass_fds=(fd,),
                    stdout=log, stderr=subprocess.STDOUT)
            finally:
                reservation.close()
            try:
                deadline = time.monotonic()+5
                while not state.exists() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        log.seek(0)
                        raise AssertionError(log.read())
                    time.sleep(.05)
                assert state.exists(), 'Qt observation did not become ready'
                info = Path(f'/proc/{process.pid}/fdinfo/{fd}').read_text()
                flags = int(next(line.split()[1] for line in info.splitlines()
                                 if line.startswith('flags:')), 8)
                assert flags & os.O_CLOEXEC, 'adopted listener can leak through a later exec'
                with socket.create_connection(('127.0.0.1', port), timeout=3) as client:
                    client.sendall((f'GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                                    'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                                    'Sec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg==\r\n'
                                    'Sec-WebSocket-Version: 13\r\n\r\n').encode())
                    received = b''
                    while b'RFB 003.003\n' not in received:
                        data = client.recv(4096)
                        assert data, received
                        received += data
                        assert len(received) < 8192, received
                    assert received.startswith(b'HTTP/1.1 101 '), received
                return dict(address='127.0.0.1', port=port, parent_copy_closed=True,
                            exec_trampoline=trampoline, websocket_rfb_greeting=True,
                            close_on_exec=True)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    check('inherited_listener_survives_exec_and_parent_close', inherited_listener)

    def inherited_nodelay():
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1',0))
            reservation.listen()
            assert reservation.getsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY)==0
            with (Path(directory)/'nodelay.log').open('w+') as log:
                process=subprocess.Popen(['/opt/swag/bin/swag-probe'],
                    env=environment(f'vncws:fd={reservation.fileno()}',state),
                    pass_fds=(reservation.fileno(),),stdout=log,stderr=subprocess.STDOUT)
                try:
                    deadline=time.monotonic()+5
                    while reservation.getsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY)!=1:
                        assert process.poll() is None, 'Qt exited before configuring listener'
                        assert time.monotonic()<deadline, 'TCP_NODELAY was not enabled'
                        time.sleep(.05)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill();process.wait()
            # The parent's descriptor references the socket configured by Qt.
            # After Qt exits, verify Linux copies its option on actual accept.
            with socket.create_connection(reservation.getsockname(),timeout=3):
                accepted,_=reservation.accept()
                with accepted:
                    assert accepted.getsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY)==1
            return {'listener_nodelay':True,'accepted_connection_nodelay':True}
    check('nodelay_inherited_by_accepted_connections', inherited_nodelay)

print(json.dumps(results, indent=2))
raise SystemExit(int(any(r['status'] != 'pass' for r in results)))
