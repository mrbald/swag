#!/usr/bin/env python3.12
"""Keep an authenticated noVNC workspace alive through a 20-second Qt GUI stall."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import traceback

from playwright.sync_api import sync_playwright

import workspace


ROOT = Path(__file__).resolve().parents[2]
PROCESS = r"""import json,os,pathlib,signal,sys
pid=int(sys.argv[1]); expected_generation=sys.argv[2]
expected_start=sys.argv[3]; action=sys.argv[4]
p=pathlib.Path('/proc')/str(pid)
status=(p/'status').read_text()
env=dict(item.split(b'=',1) for item in (p/'environ').read_bytes().split(b'\0') if b'=' in item)
identity={'pid':pid,'comm':(p/'comm').read_text().strip(),
 'uid':int(next(line for line in status.splitlines() if line.startswith('Uid:')).split()[1]),
 'user':env.get(b'SWAG_USER',b'').decode(),
 'generation':env.get(b'SWAG_GENERATION',b'').decode(),
 'render_backend':env.get(b'SWAG_RENDER_BACKEND',b'').decode(),
 'start_ticks':(p/'stat').read_text().rsplit(')',1)[1].split()[19]}
if (identity['comm']!='swag-desktop' or identity['uid']!=1000 or
    identity['user']!='alice' or identity['generation']!=expected_generation or
    identity['render_backend']!='qt-ws' or
    (expected_start!='-' and identity['start_ticks']!=expected_start)):
 raise RuntimeError('Qt process identity changed')
if action!='check': os.kill(pid, getattr(signal,'SIG'+action))
identity['action']=action
print(json.dumps(identity))
"""


def command(engine: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([engine, *args], cwd=ROOT, text=True, capture_output=True,
                          check=True, timeout=30)


def qt_process(engine: str, pid: int, generation: str, start: str = '-',
               action: str = 'check') -> dict:
    return json.loads(command(engine, 'exec', 'swag-host', 'python3.12', '-c', PROCESS,
                              str(pid), generation, start, action).stdout)


def canvas(actor: workspace.Actor, path: Path) -> str:
    data = actor.page.locator('#screen canvas').screenshot(path=str(path))
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', default='podman')
    parser.add_argument('--origin', default='http://localhost:6081')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/qt-ws-review-fixes/stall')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    result: dict = {'check': 'qt_gui_stall_authenticated_viewer', 'status': 'running'}
    actor = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            try:
                actor = workspace.Actor(browser, 'alice', args.origin)
                actor.login()
                assert actor.api('DELETE')['status'] == 200
                assert actor.api('POST')['status'] == 200
                actor.page.reload(wait_until='domcontentloaded')
                actor.connected()
                before = actor.wait(lambda s: s.get('ui_mode') == 'mdi' and
                                    len(s.get('windows', [])) == 1 and not s.get('loading'))
                actor.click('entity_portfolios')  # Real noVNC input, even for the initial entity.
                before = actor.wait(lambda s: s.get('entity') == 'portfolios' and
                                    len(s.get('windows', [])) == 1 and not s.get('loading'))
                pid, generation = actor.session['pid'], actor.session['generation']
                assert (before['pid'], before['generation']) == (pid, generation)
                identity = qt_process(args.engine, pid, generation)
                (output / 'process-identity.json').write_text(json.dumps(identity, indent=2) + '\n')
                (output / 'before-state.json').write_text(json.dumps(before, indent=2) + '\n')
                before_hash = canvas(actor, output / 'before-canvas.png')
                original_window = before['windows'][0]['window_id']
                samples = []
                stop_attempted = False
                try:
                    stop_attempted = True
                    stopped = qt_process(args.engine, pid, generation,
                                         identity['start_ticks'], 'STOP')
                    (output / 'stop-identity.json').write_text(json.dumps(stopped, indent=2) + '\n')
                    started = time.monotonic()
                    while time.monotonic() - started < 20:
                        assert actor.page.locator('#status').inner_text() == 'Connected'
                        session = actor.api()
                        assert session['status'] == 200, session
                        body = session['body']
                        assert (body['state'], body['pid'], body['generation']) == (
                            'running', pid, generation), body
                        samples.append({'at_seconds': round(time.monotonic() - started, 2),
                                        'viewer': 'Connected', 'workspace': body['state']})
                        time.sleep(1)
                    result['qt_stall_seconds'] = round(time.monotonic() - started, 2)
                finally:
                    # Even an assertion or Playwright failure during the stall
                    # must resume this exact Qt process before cleanup proceeds.
                    try:
                        if stop_attempted:
                            resumed = qt_process(args.engine, pid, generation,
                                                 identity['start_ticks'], 'CONT')
                            (output / 'resume-identity.json').write_text(json.dumps(resumed, indent=2) + '\n')
                    finally:
                        (output / 'stall-observations.json').write_text(json.dumps(samples, indent=2) + '\n')

                assert actor.page.locator('#status').inner_text() == 'Connected'
                actor.click('entity_instruments')
                after = actor.wait(lambda s: s.get('entity') == 'instruments' and
                                   len(s.get('windows', [])) == 2 and not s.get('loading') and
                                   any(w['window_id'] == original_window and w['entity'] == 'portfolios'
                                       for w in s['windows']))
                assert (after['pid'], after['generation']) == (pid, generation)
                assert (actor.api()['body']['pid'], actor.session['generation']) == (pid, generation)
                (output / 'after-state.json').write_text(json.dumps(after, indent=2) + '\n')
                actor.page.wait_for_timeout(250)
                after_hash = canvas(actor, output / 'after-canvas.png')
                assert after_hash != before_hash, 'new MDI frame did not paint on canvas'
                result.update(status='pass', pid=pid, generation=generation,
                              original_window_id=original_window,
                              window_ids_after=[w['window_id'] for w in after['windows']],
                              browser_checks_during_stall=len(samples),
                              before_canvas_sha256=before_hash, after_canvas_sha256=after_hash)
            except Exception:
                if actor is not None:
                    (output / 'failure-state.json').write_text(json.dumps(actor.state(), indent=2) + '\n')
                    try:
                        actor.page.screenshot(path=str(output / 'failure.png'))
                    except Exception:
                        pass
                raise
            finally:
                try:
                    if actor is not None and actor.session is not None:
                        cleanup = actor.api('DELETE')
                        result['workspace_cleanup_status'] = cleanup['status']
                        assert cleanup['status'] == 200, cleanup
                finally:
                    browser.close()
    except Exception as error:
        result.update(status='fail', error=repr(error))
        (output / 'failure.log').write_text(traceback.format_exc())
    (output / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    print(f"stall: {result['status']}", flush=True)
    return int(result['status'] != 'pass')


if __name__ == '__main__':
    raise SystemExit(main())
