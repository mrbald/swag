#!/usr/bin/env python3
"""Independent Linux/container acceptance for vncws filesystem Unix sockets.

Run as root inside the EL8 probe image. Qt runs as UID/GID 1000; a distinct UID
checks the filesystem boundary. Inputs are raw RFB, not Qt instrumentation.
Browser/noVNC acceptance remains a separate gate. Uses only the Python stdlib.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

UID = 1000
GID = 1000
MIB = 1024 * 1024
PIXEL = struct.pack('>BBBBHHHBBBxxx', 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.025)
    raise AssertionError('condition timed out')


def request(width=32, height=32):
    return struct.pack('>BBHHHH', 3, 0, 0, 0, width, height)


def key(down, keysym):
    return struct.pack('>BBHI', 4, int(down), 0, keysym)


def click(x, y):
    return struct.pack('>BBHH', 5, 1, x, y) + struct.pack('>BBHH', 5, 0, x, y)


class Rfb:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(5)
        self.socket.connect(str(path))

    def close(self):
        self.socket.close()

    def read(self, length):
        data = bytearray()
        while len(data) < length:
            part = self.socket.recv(min(65536, length - len(data)))
            if not part:
                raise EOFError(f'RFB closed with {length-len(data)} bytes outstanding')
            data.extend(part)
        return bytes(data)

    def init(self, fragmented=False, coalesced=False):
        assert self.read(12) == b'RFB 003.003\n'
        if fragmented:
            for byte in b'RFB 003.003\n':
                self.socket.sendall(bytes([byte]))
                time.sleep(.002)
        else:
            extra = b'\x01' + b'\0\0\0\0' + PIXEL + request() if coalesced else b''
            self.socket.sendall(b'RFB 003.003\n' + extra)
        assert self.read(4) == struct.pack('>I', 1)
        if not coalesced:
            self.socket.sendall(b'\x01')
        header = self.read(24)
        self.size = struct.unpack('>HH', header[:4])
        assert self.size == (1280, 800), self.size
        assert header[4] == 32, header
        name_size = struct.unpack('>I', header[20:24])[0]
        assert name_size < 4096
        self.read(name_size)

    def update(self):
        header = self.read(4)
        assert header[:2] == b'\0\0', header
        count = struct.unpack('>H', header[2:])[0]
        assert 0 < count <= 4096, count
        pixels = bytearray()
        area = 0
        for _ in range(count):
            x, y, width, height, encoding = struct.unpack('>HHHHi', self.read(12))
            assert encoding == 0, encoding
            assert width and height and x + width <= self.size[0] and y + height <= self.size[1]
            area += width * height
            pixels.extend(self.read(width * height * 4))
        return {'rectangles': count, 'pixel_bytes': len(pixels), 'area': area,
                'sha256': hashlib.sha256(pixels).hexdigest()}

    def closed(self):
        # This assertion is used only when no framebuffer was requested.
        try:
            assert self.socket.recv(1) == b''
        except ConnectionResetError:
            pass


class Probe:
    def __init__(self, case, platform, executable, pass_fds=()):
        self.case = case
        self.state_path = case / 'state.json'
        self.log_path = case / 'qt.log'
        self.log = self.log_path.open('wb')
        env = dict(os.environ, QT_QPA_PLATFORM=platform,
                   QT_PLUGIN_PATH='/opt/swag/plugins', QT_FORCE_STDERR_LOGGING='1',
                   QT_LOGGING_RULES='qt.qpa.vnc.debug=true', XDG_RUNTIME_DIR=str(case),
                   SWAG_STATE_FILE=str(self.state_path), SWAG_USER='probe', SWAG_GENERATION='unix-test')
        env.pop('DISPLAY', None)
        self.process = subprocess.Popen([executable], env=env, stdout=self.log,
                                        stderr=subprocess.STDOUT, pass_fds=pass_fds,
                                        user=UID, group=GID, extra_groups=[])

    def state(self):
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def ready(self):
        def observed():
            assert self.process.poll() is None, self.log_path.read_text()[-3000:]
            state = self.state()
            return state if state.get('ready') else None
        return wait_for(observed)

    def quit(self, client):
        x, y, width, height = self.ready()['geometry']['quit_button']
        client.socket.sendall(click(int(x + width/2), int(y + height/2)))
        assert self.process.wait(timeout=8) == 0, self.log_path.read_text()[-3000:]

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.log.close()


def socket_identity(path):
    info = path.lstat()
    assert stat.S_ISSOCK(info.st_mode), oct(info.st_mode)
    return info.st_dev, info.st_ino


def inspect_tcp_listeners(pid):
    descriptors = set()
    for fd in Path(f'/proc/{pid}/fd').iterdir():
        try:
            target = fd.readlink().as_posix()
        except FileNotFoundError:
            continue
        if target.startswith('socket:['):
            descriptors.add(target[8:-1])
    listeners = []
    for name in ('tcp', 'tcp6'):
        for line in Path(f'/proc/net/{name}').read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == '0A' and fields[9] in descriptors:
                listeners.append(fields[1])
    # An empty scan must not disguise denied access or a vanished process.
    assert descriptors, 'no socket descriptors observed for live Qt process'
    return {'listeners': listeners, 'socket_descriptors': len(descriptors)}


def owned_tcp_listeners(pid):
    # Container root may lack CAP_SYS_PTRACE and cannot read another UID's
    # /proc/PID/fd symlinks. Inspect as the same UID as Qt instead of weakening
    # the ownership assertion or depending on privileged container execution.
    inspected = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), '--inspect-tcp-pid', str(pid)],
        user=UID, group=GID, extra_groups=[], check=True, capture_output=True,
        text=True, timeout=5,
    )
    return json.loads(inspected.stdout)['listeners']


class Acceptance:
    def __init__(self, output, executable):
        self.output, self.executable = output, executable
        self.output.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='swag-unix-', dir='/tmp'))
        self.root.chmod(0o755)
        self.results = []
        self.counter = 0

    def case(self):
        self.counter += 1
        case = self.root / str(self.counter)
        case.mkdir(mode=0o700)
        os.chown(case, UID, GID)
        return case

    @contextmanager
    def app(self, case, parameter, pass_fds=()):
        app = Probe(case, 'vncws:size=1280x800:' + parameter, self.executable, pass_fds)
        try:
            yield app
        finally:
            app.close()
            shutil.copy2(app.log_path, self.output / f'case-{self.counter}-qt.log')

    def run(self, name, body):
        started = time.monotonic()
        try:
            details = body() or {}
            row = {'check': name, 'status': 'pass', **details}
        except Exception as error:
            row = {'check': name, 'status': 'fail', 'error': str(error)[:3000]}
        row['seconds'] = round(time.monotonic() - started, 3)
        self.results.append(row)
        (self.output/'results.json').write_text(json.dumps(self.results, indent=2)+'\n')
        print(f"unix: {name}: {row['status']}", flush=True)

    def listener(self, path, mode=0o600, listening=True):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chown(path, UID, GID)
        path.chmod(mode)
        if listening:
            listener.listen(8)
        return listener

    def standalone(self):
        case = self.case()
        path = case/'rfb.sock'
        with self.app(case, f'socket={path}') as app:
            app.ready()
            info = path.lstat()
            assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == UID
            assert not owned_tcp_listeners(app.process.pid)
            for fragmented, coalesced in ((True, False), (False, True)):
                client = Rfb(path)
                try:
                    client.init(fragmented=fragmented, coalesced=coalesced)
                    if not coalesced:
                        client.socket.sendall(request())
                    result = client.update()
                    assert result['pixel_bytes'] == 32*32*4, result
                finally:
                    client.close()
            client = Rfb(path)
            try:
                client.init()
                app.quit(client)
            finally:
                client.close()
            assert not path.exists(), 'standalone socket retained after normal Qt exit'
        return {'mode': '0600', 'uid': UID, 'tcp_listeners': [], 'raw_pixels_verified': True}

    def permission(self):
        case = self.case()
        path = case/'rfb.sock'
        with self.app(case, f'socket={path}') as app:
            app.ready()
            # This is an actual different Unix identity, not merely a mode assertion.
            code = ('import errno,socket,sys; s=socket.socket(socket.AF_UNIX); '
                    '\ntry: s.connect(sys.argv[1])\n'
                    'except OSError as e: sys.exit(0 if e.errno in (errno.EACCES,errno.EPERM) else 2)\n'
                    'else: sys.exit(3)')
            denied = subprocess.run([sys.executable, '-c', code, str(path)],
                                    user=1001, group=1001, extra_groups=[], timeout=5)
            assert denied.returncode == 0, denied.returncode
            client = Rfb(path)
            try:
                client.init()
                app.quit(client)
            finally:
                client.close()
        return {'denied_uid': 1001}

    def takeover(self):
        case = self.case()
        path = case/'rfb.sock'
        with self.app(case, f'socket={path}') as app:
            app.ready()
            first = Rfb(path)
            second = None
            try:
                first.init()
                first.socket.sendall(key(True, 0xffe3))
                wait_for(lambda: app.state().get('keyboard_modifiers') == 0x04000000)
                second = Rfb(path)
                second.init()
                first.closed()
                wait_for(lambda: app.state().get('keyboard_modifiers') == 0)
                second.socket.sendall(request())
                second.update()
                second.socket.sendall(key(True, 0xffe1))
                wait_for(lambda: app.state().get('keyboard_modifiers') == 0x02000000)
                second.close()
                second = None
                wait_for(lambda: app.state().get('keyboard_modifiers') == 0)
                replacement = Rfb(path)
                try:
                    replacement.init()
                    app.quit(replacement)
                finally:
                    replacement.close()
            finally:
                first.close()
                if second:
                    second.close()
        return {'takeover_and_disconnect_release_modifiers': True}

    def paused_reader(self):
        case = self.case()
        path = case/'rfb.sock'
        with self.app(case, f'socket={path}') as app:
            app.ready()
            client = Rfb(path)
            try:
                client.init()
                client.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                client.socket.sendall(request(1280, 800))
                # Leave a full Raw frame queued, then ask for another while paused.
                time.sleep(.15)
                for _ in range(12):
                    client.socket.sendall(request(1280, 800))
                    time.sleep(.025)
                wait_for(lambda: 'framebuffer deferred' in app.log_path.read_text())
                first = client.update()
                second = client.update()
                assert first['pixel_bytes'] == second['pixel_bytes'] == 1280*800*4
                wait_for(lambda: 'framebuffer resumed' in app.log_path.read_text())
                assert app.process.poll() is None
                app.quit(client)
            finally:
                client.close()
        return {'raw_bytes_per_frame': 1280*800*4, 'deferred_and_resumed': True}

    def protocol_faults(self):
        case = self.case()
        path = case/'rfb.sock'
        with self.app(case, f'socket={path}') as app:
            app.ready()
            for packet in (b'\xfa', struct.pack('>B3xI', 6, MIB+1)):
                client = Rfb(path)
                try:
                    client.init()
                    client.socket.sendall(packet)
                    client.closed()
                finally:
                    client.close()
                assert app.process.poll() is None
            client = Rfb(path)
            try:
                client.init()
                client.socket.sendall(request())
                client.update()
                app.quit(client)
            finally:
                client.close()
        return {'unknown_message_closed': True, 'oversized_clipboard_closed': True,
                'next_viewer_healthy': True}

    def inherited(self):
        case = self.case()
        path = case/'rfb.sock'
        with self.listener(path) as listener:
            identity = socket_identity(path)
            with self.app(case, f'fd={listener.fileno()}', (listener.fileno(),)) as app:
                app.ready()
                assert not owned_tcp_listeners(app.process.pid)
                client = Rfb(path)
                try:
                    client.init()
                    client.socket.sendall(request())
                    client.update()
                    app.quit(client)
                finally:
                    client.close()
                assert socket_identity(path) == identity, 'plugin unlinked the inherited pathname'
                assert listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
            path.unlink()
        return {'manager_path_retained': True, 'manager_listener_still_open': True}

    def replacement_cleanup(self):
        case = self.case()
        path = case/'rfb.sock'
        with self.app(case, f'socket={path}') as app:
            app.ready()
            original = socket_identity(path)
            client = Rfb(path)
            try:
                client.init()
                path.unlink()
                with self.listener(path) as replacement:
                    identity = socket_identity(path)
                    assert identity != original
                    app.quit(client)
                    assert socket_identity(path) == identity, 'cleanup removed a replacement socket'
                    assert replacement.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
            finally:
                client.close()
        return {'replacement_inode_preserved': True}

    def terminated_standalone_restart(self):
        case = self.case()
        path = case/'rfb.sock'
        with self.app(case, f'socket={path}') as app:
            app.ready()
            original = socket_identity(path)
            app.process.terminate()
            termination_code = app.process.wait(timeout=8)
            assert termination_code != 0, termination_code
        assert socket_identity(path) == original, 'SIGTERM unexpectedly removed the standalone socket'

        # Keep state/log output separate so stale readiness from the first
        # process cannot make the rejected restart appear to have initialized.
        restart_case = self.case()
        with self.app(restart_case, f'socket={path}') as restarted:
            assert restarted.process.wait(timeout=8) != 0, 'occupied path restart was accepted'
            assert not restarted.state().get('ready'), 'rejected restart published readiness'
        assert socket_identity(path) == original, 'restart removed or replaced the residual socket'

        fresh_case = self.case()
        fresh_path = fresh_case/'rfb.sock'
        with self.app(fresh_case, f'socket={fresh_path}') as fresh:
            fresh.ready()
            client = Rfb(fresh_path)
            try:
                client.init()
                client.socket.sendall(request())
                client.update()
                fresh.quit(client)
            finally:
                client.close()
        assert not fresh_path.exists(), 'normal Qt exit left the fresh generation socket'
        assert socket_identity(path) == original, 'fresh generation disturbed the previous socket'
        return {'termination_code': termination_code, 'residual_inode_preserved': True,
                'same_path_restart_rejected': True, 'fresh_generation_started_and_cleaned': True}

    def rejected(self, parameter, setup=None):
        case = self.case()
        path = case/'rfb.sock'
        resource = setup(case, path) if setup else None
        before = path.lstat() if path.exists() or path.is_symlink() else None
        try:
            value = parameter(case, path) if callable(parameter) else parameter
            with self.app(case, value) as app:
                code = app.process.wait(timeout=8)
                assert code != 0, 'invalid configuration started successfully'
                assert not app.state().get('ready'), 'invalid configuration published readiness'
            if before:
                after = path.lstat()
                assert (before.st_dev, before.st_ino, before.st_mode) == (after.st_dev, after.st_ino, after.st_mode)
            return {'startup_rejected': True, 'existing_entry_preserved': before is not None}
        finally:
            if resource is not None and hasattr(resource, 'close'):
                resource.close()

    def rejected_fd(self, kind):
        case = self.case()
        path = case/'rfb.sock'
        if kind == 'not-listening':
            listener = self.listener(path, listening=False)
        elif kind == 'insecure-socket':
            listener = self.listener(path, mode=0o666)
        elif kind == 'insecure-parent':
            listener = self.listener(path)
            case.chmod(0o755)
        elif kind == 'datagram':
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            listener.bind(str(path))
            os.chown(path, UID, GID)
            path.chmod(0o600)
        elif kind == 'abstract':
            listener = socket.socket(socket.AF_UNIX)
            listener.bind('\0swag-unix-acceptance-'+str(os.getpid()))
            listener.listen(8)
        else:
            raise AssertionError(kind)
        try:
            with self.app(case, f'fd={listener.fileno()}', (listener.fileno(),)) as app:
                code = app.process.wait(timeout=8)
                assert code != 0, 'invalid inherited socket accepted'
                assert not app.state().get('ready')
            if kind != 'abstract':
                assert path.is_socket(), 'invalid inherited pathname was removed'
        finally:
            listener.close()
        return {'invalid_fd': kind, 'startup_rejected': True}

    def run_all(self):
        self.run('standalone_permissions_raw_framing_no_tcp_cleanup', self.standalone)
        self.run('different_uid_connection_denied', self.permission)
        self.run('takeover_and_disconnect_release_input', self.takeover)
        self.run('paused_reader_defers_and_recovers', self.paused_reader)
        self.run('protocol_faults_close_only_the_viewer', self.protocol_faults)
        self.run('inherited_listener_and_path_survive_qt_exit', self.inherited)
        self.run('standalone_cleanup_preserves_replacement_socket', self.replacement_cleanup)
        self.run('terminated_standalone_requires_fresh_generation_or_cleanup',
                 self.terminated_standalone_restart)
        for name, parameter in (
            ('relative_path', 'socket=relative.sock'),
            ('abstract_name', 'socket=@abstract'),
            ('long_path', lambda c,p: 'socket='+str(c/('x'*120))),
            ('socket_and_port', lambda c,p: f'socket={p}:port=5900'),
            ('socket_and_addr', lambda c,p: f'socket={p}:addr=127.0.0.1'),
            ('socket_and_fd', lambda c,p: f'socket={p}:fd=12345'),
        ):
            self.run(name+'_rejected', lambda parameter=parameter: self.rejected(parameter))
        self.run('insecure_parent_rejected', lambda: self.rejected(
            lambda c,p: f'socket={p}', lambda c,p: c.chmod(0o755)))
        self.run('occupied_regular_file_preserved', lambda: self.rejected(
            lambda c,p: f'socket={p}', lambda c,p: p.write_text('do not remove')))
        def symlink(case, path):
            path.symlink_to(case/'missing')
        self.run('occupied_symlink_preserved', lambda: self.rejected(lambda c,p: f'socket={p}', symlink))
        self.run('occupied_socket_preserved', lambda: self.rejected(
            lambda c,p: f'socket={p}', lambda c,p: self.listener(p)))
        for kind in ('not-listening', 'insecure-socket', 'insecure-parent', 'datagram', 'abstract'):
            self.run('inherited_'+kind+'_rejected', lambda kind=kind: self.rejected_fd(kind))

    def close(self):
        shutil.rmtree(self.root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--executable', default='/opt/swag/bin/swag-probe')
    parser.add_argument('--inspect-tcp-pid', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inspect_tcp_pid is not None:
        if os.geteuid() != UID:
            parser.error('socket inspector must run under the Qt UID')
        print(json.dumps(inspect_tcp_listeners(args.inspect_tcp_pid)))
        return 0
    if args.output is None:
        parser.error('--output is required')
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('run as root inside the Linux probe image; Qt children drop to UID/GID1000')
    suite = Acceptance(args.output.resolve(), args.executable)
    try:
        suite.run_all()
    finally:
        suite.close()
    return int(any(row['status'] != 'pass' for row in suite.results))


if __name__ == '__main__':
    raise SystemExit(main())
