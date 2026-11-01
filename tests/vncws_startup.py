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

    def fails(options):
        result = subprocess.run(['/opt/swag/bin/swag-probe'],
                                env=environment('vncws:'+options, state),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=3)
        assert result.returncode != 0, result.stdout
        assert 'vncws:' in result.stdout, result.stdout
        return dict(exit_code=result.returncode, diagnostic=result.stdout.strip())

    for options in ['size=0x800', 'size=4096x4096', 'size=2048x2048',
                    'size=1280x800junk', 'size=800x600:size=800x600',
                    'depth=16', 'port=0', 'port=65536', 'port=5900junk',
                    'addr=localhost', 'addr=999.0.0.1', 'unknown=1']:
        check('reject_'+options, lambda options=options: fails(options))
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        check('occupied_port_fails', lambda: fails(f'port={occupied.getsockname()[1]}'))

    def default_loopback():
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        state.unlink(missing_ok=True)
        with (Path(directory)/'qt.log').open('w+') as log:
            process = subprocess.Popen(['/opt/swag/bin/swag-probe'],
                                       env=environment(f'vncws:port={port}', state),
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
                        return dict(address='127.0.0.1', port=port)
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

print(json.dumps(results, indent=2))
raise SystemExit(int(any(r['status'] != 'pass' for r in results)))
