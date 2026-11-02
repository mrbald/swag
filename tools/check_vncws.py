#!/usr/bin/env python3
"""Run the vncws functional suites sequentially against existing images.

Print a JSON summary to stdout; progress goes to stderr and full logs stay in the
unique run directory. Shared-demo checks are opt-in. No images are rebuilt, and
network, performance, soak and visual-review gates are separate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
# Ordered so isolated probes finish before any explicitly selected shared demo.
SUITES = {
    'protocol': ('tests/vncws_protocol.py', 'wire'),
    'input': ('tests/vncws_input.py', 'wire'),
    'clipboard': ('tests/vncws_clipboard.py', 'wire'),
    'compression': ('tests/vncws_compression.py', 'wire'),
    'resize': ('tests/vncws_resize.py', 'wire'),
    'smoke': ('tests/browser/smoke.py', 'probe'),
    'everyday': ('tests/browser/everyday.py', 'probe'),
    'outbound-clipboard': ('tests/browser/outbound_clipboard.py', 'fixed-probe'),
    'app-mdi': ('tests/browser/mdi.py', 'app-engine'),
    'app-workspace': ('tests/browser/workspace.py', 'app-engine'),
    'app-resize': ('tests/browser/resize.py', 'app'),
    'app-clipboard': ('tests/browser/clipboard.py', 'app-clipboard'),
}
DEFAULT_SUITES = tuple(name for name, (_, kind) in SUITES.items()
                       if not kind.startswith('app'))


def command(name: str, args: argparse.Namespace, output: Path) -> list[str]:
    script, kind = SUITES[name]
    result = [sys.executable, str(ROOT / script), '--output', str(output)]
    if kind in ('wire', 'probe', 'fixed-probe', 'app-engine'):
        result += ['--engine', args.engine]
    if kind in ('wire', 'probe', 'fixed-probe'):
        result += ['--port', str(args.port)]
    if kind == 'probe':
        result += ['--backend', 'qt-ws']
    if name == 'smoke':
        result += ['--browser-channel', 'chrome']
    if kind.startswith('app'):
        result += ['--origin', args.origin]
        if kind != 'app-clipboard':
            result += ['--render-backend', 'qt-ws']
    return result


def check_results(value):
    """Read existing flat or grouped harness reports without trusting exit alone."""
    if isinstance(value, dict):
        if 'check' in value and 'status' in value:
            yield value
        else:
            for child in value.values():
                yield from check_results(child)
    elif isinstance(value, list):
        for child in value:
            yield from check_results(child)


def write_summary(path: Path, summary: dict) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(summary, indent=2) + '\n')
    temporary.replace(path)


def run(args: argparse.Namespace, names: list[str]) -> tuple[int, dict]:
    run_name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
    output = args.output.resolve() / run_name
    output.mkdir(parents=True, exist_ok=False)
    summary = {'schema_version': 1, 'status': 'running', 'output': str(output),
               'engine': args.engine, 'port': args.port, 'origin': args.origin,
               'selected_suites': names, 'suites': []}
    summary_path = output / 'summary.json'
    write_summary(summary_path, summary)
    failed = False
    interrupted = False
    for name in names:
        suite_output = output / name
        argv = command(name, args, suite_output)
        row = {'suite': name, 'status': 'skipped', 'command': argv,
               'output': str(suite_output), 'checks': 0}
        if interrupted or (failed and not args.keep_going):
            row['reason'] = 'earlier suite failed or was interrupted'
            summary['suites'].append(row)
            continue
        suite_output.mkdir()
        log_path = suite_output / 'run.log'
        row['log'] = str(log_path)
        started = time.monotonic()
        print(f'vncws: {name}: running ({log_path})', file=sys.stderr, flush=True)
        try:
            with log_path.open('w') as log:
                process = subprocess.run(argv, cwd=ROOT, stdout=log,
                                         stderr=subprocess.STDOUT, check=False)
            row['returncode'] = process.returncode
            try:
                reports = list(check_results(json.loads(
                    (suite_output / 'results.json').read_text())))
                row['checks'] = len(reports)
                row['failed_checks'] = [r['check'] for r in reports if r['status'] != 'pass']
                if not reports:
                    row['error'] = 'results.json contains no checks'
            except (OSError, ValueError) as error:
                row['error'] = f'Cannot read results.json: {error}'
            row['status'] = ('pass' if process.returncode == 0 and
                             row['checks'] > 0 and not row.get('failed_checks') and
                             'error' not in row else 'fail')
        except KeyboardInterrupt:
            interrupted = True
            row.update(status='fail', error='Interrupted', returncode=130)
        except OSError as error:
            row.update(status='fail', error=str(error))
        row['seconds'] = round(time.monotonic() - started, 3)
        failed |= row['status'] != 'pass'
        summary['suites'].append(row)
        write_summary(summary_path, summary)
        print(f"vncws: {name}: {row['status']}", file=sys.stderr, flush=True)
    summary['status'] = 'fail' if failed else 'pass'
    summary['checks'] = sum(row['checks'] for row in summary['suites'])
    write_summary(summary_path, summary)
    return (130 if interrupted else int(failed)), summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true', help='List available suites as JSON; do not run')
    parser.add_argument('--suite', action='append', choices=SUITES,
                        help='Select a suite; repeat to select several (canonical order)')
    parser.add_argument('--engine', default='podman')
    parser.add_argument('--port', type=int, default=6300,
                        help='Isolated probe HTTP port; the following port is its WebSocket port')
    parser.add_argument('--origin', default='http://localhost:6081',
                        help='Origin of the local shared MDI demo for explicit app suites')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/qt-ws-functional',
                        help='Parent directory; a unique child is created for each run')
    parser.add_argument('--keep-going', action='store_true', help='Run later suites after a failure')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65534:
        parser.error('--port must be in 1..65534 (the next port is also used)')
    if args.list:
        print(json.dumps({'schema_version': 1, 'suites': [
            {'name': name, 'script': script, 'default': name in DEFAULT_SUITES,
             'shared_demo': kind.startswith('app')}
            for name, (script, kind) in SUITES.items()
            if args.suite is None or name in args.suite]}, indent=2))
        return 0
    selected = set(args.suite or DEFAULT_SUITES)
    names = [name for name in SUITES if name in selected]
    code, summary = run(args, names)
    print(json.dumps(summary, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
