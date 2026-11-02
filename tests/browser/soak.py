#!/usr/bin/env python3
"""Bounded two-user noVNC soak; local evidence, not a capacity qualification."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import time

from playwright.sync_api import sync_playwright

import workspace


RESOURCE_PROBE = r'''
import json, os, pathlib
result=[]
for path in pathlib.Path('/proc').iterdir():
 if not path.name.isdecimal(): continue
 try:
  if (path/'comm').read_text().strip() != 'swag-desktop': continue
  fields=dict(line.split(':',1) for line in (path/'status').read_text().splitlines())
  env=dict(x.split(b'=',1) for x in (path/'environ').read_bytes().split(b'\0') if b'=' in x)
  stat=(path/'stat').read_text().rsplit(')',1)[1].split()
  result.append(dict(user=env.get(b'SWAG_USER',b'').decode(), pid=int(path.name),
    rss_kib=int(fields['VmRSS'].split()[0]), peak_rss_kib=int(fields['VmHWM'].split()[0]),
    cpu_seconds=(int(stat[11])+int(stat[12]))/os.sysconf('SC_CLK_TCK'),
    fd_count=len(list((path/'fd').iterdir()))))
 except (FileNotFoundError,ProcessLookupError): pass
print(json.dumps(result))
'''


class SoakActor(workspace.Actor):
    def api(self, method="GET", path="/session", body=None, headers=None):
        result = self.page.evaluate("""async ({method,path,body,headers}) => {
          const response=await fetch(path,{method,headers,
            signal:AbortSignal.timeout(15000),
            body:method==='GET'?undefined:JSON.stringify(body)});
          return {status:response.status,body:await response.json()};
        }""", {"method":method,"path":path,"body":body,"headers":headers or {
            "Content-Type":"application/json", "X-Swag-CSRF":(self.session or {}).get("csrf_token", "")}})
        if path == "/session" and result["status"] == 200:
            self.session = result["body"]
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', default='http://localhost:6081')
    parser.add_argument('--engine', default='podman')
    parser.add_argument('--seconds', type=int, default=1800)
    parser.add_argument('--interval', type=float, default=10)
    parser.add_argument('--output', type=Path, default=workspace.ROOT/'artifacts/qt-ws-polish/soak')
    args = parser.parse_args()
    if args.seconds <= 0 or args.interval <= 0:
        parser.error('seconds and interval must be positive')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    workspace.OUTPUT = output
    suite = workspace.Suite(args.engine, args.origin, 'qt-ws')
    result = dict(status='running', requested_seconds=args.seconds, interval=args.interval,
                  users=['alice', 'bob'], cycles=[], resources=[], unexpected_closes=[],
                  browser_errors=[], limits='Two live users with alternating real input; local HTTP/Chrome. '
                  'No production capacity, Windows/RDP or WAN assertion. Resource samples are periodic, '
                  'not continuous. Queue deferrals are retained in Qt logs, not a continuous queue maximum.')
    actors, guards, identities, tokens, ports = {}, {}, {}, {}, {}
    started = time.monotonic()

    def save():
        result['elapsed_seconds'] = round(time.monotonic()-started, 2)
        (output/'results.json').write_text(json.dumps(result, indent=2)+'\n')

    def attach(actor):
        guard = {'sockets': [], 'expected': set()}
        guards[id(actor)] = guard
        def socket_opened(socket):
            if '/viewer/' in socket.url:
                guard['sockets'].append(socket)
                def closed(*_):
                    if socket not in guard['expected']:
                        result['unexpected_closes'].append(dict(user=actor.user,
                            elapsed=round(time.monotonic()-started, 2)))
                socket.on('close', closed)
        actor.page.on('websocket', socket_opened)
        actor.page.on('pageerror', lambda error: result['browser_errors'].append(str(error)))

    def allow_close(actor):
        guard = guards[id(actor)]
        guard['expected'].update(guard['sockets'])

    def connected(actor):
        actor.page.locator('#status').filter(has_text=re.compile(r'^Connected$')).wait_for(timeout=20000)
        actor.wait(lambda s: s.get('framebuffer_size') == actor.framebuffer_size())
        current = actor.api()['body']
        assert (current['pid'], current['generation']) == identities[actor.user]

    def retained(actor):
        connected(actor)
        state = actor.wait(lambda s: s.get('draft') == tokens[actor.user])
        assert state['dirty'] and state['form_values']['name'] == f'Soak unsaved {actor.user}', state
        assert not result['unexpected_closes'], result['unexpected_closes']
        assert not result['browser_errors'], result['browser_errors']

    def resources():
        values = json.loads(suite.command('exec', 'swag-host', 'python3.12', '-c', RESOURCE_PROBE).stdout)
        assert len(values) == 2 and {v['user'] for v in values} == {'alice', 'bob'}, values
        assert all(v['pid'] == identities[v['user']][0] for v in values), values
        result['resources'].append(dict(elapsed=round(time.monotonic()-started, 2), processes=values))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        try:
            for user in ('alice', 'bob'):
                actor = SoakActor(browser, user, args.origin)
                actors[user] = actor
                attach(actor)
                actor.login()
                allow_close(actor)
                actor.api('DELETE'); actor.api('POST')
                actor.page.reload(wait_until='domcontentloaded'); actor.connected()
                identities[user] = (actor.session['pid'], actor.session['generation'])
                manifest_path = workspace.STACK/'workspaces/users'/user/actor.session['generation']/'launcher.json'
                manifest = json.loads(manifest_path.read_text())
                assert manifest['render_backend'] == 'qt-ws' and manifest['app_pid'] == actor.session['pid'], manifest
                ports[user] = manifest['ws_port']
                actor.edit('field_name', f'Soak unsaved {user}')
                actor.wait(lambda s: s.get('dirty') and s.get('form_values', {}).get('name') == f'Soak unsaved {user}')
                tokens[user] = ''
            result['identities'] = identities
            # The requested duration starts only once both users are connected.
            workload_start = time.monotonic()
            result['workload_start_offset_seconds'] = round(workload_start-started, 2)
            cycle = 0
            while time.monotonic()-workload_start < args.seconds:
                cycle_start = time.monotonic()
                cycle += 1
                record = dict(number=cycle, elapsed=round(cycle_start-workload_start, 2), actions=[])
                for user in ('alice', 'bob'):
                    actor = actors[user]
                    token = f'{user} cycle {cycle} CHF € Zürich Ж'
                    tokens[user] = token
                    actor.open_clipboard()
                    actor.page.locator('#clipboard').fill(token)
                    actor.page.locator('#paste').click()
                    actor.click('draft')
                    actor.page.keyboard.press('Control+a')
                    actor.page.keyboard.press('Control+v')
                    actor.wait(lambda s: s.get('draft') == token)
                    actor.page.keyboard.press('Control+a')
                    actor.open_clipboard()
                    actor.page.locator('#clipboard').fill('waiting for outbound')
                    actor.page.locator('#clipboard-close').click()
                    actor.click('draft'); actor.page.keyboard.press('Control+a'); actor.page.keyboard.press('Control+c')
                    actor.page.wait_for_function('text => document.querySelector("#clipboard").value === text', arg=token)
                    size = {'width': 1440 if cycle % 2 else 1200, 'height': 1040 if cycle % 2 else 900}
                    actor.page.set_viewport_size(size)
                    workspace.wait_for(lambda: actor.framebuffer_size() == actor.page.locator('#screen').evaluate(
                        'e => [Math.floor(e.clientWidth),Math.floor(e.clientHeight)]'), 15)
                    retained(actor)
                    record['actions'].append(dict(user=user, clipboard_roundtrip=True, size=size, draft_preserved=True))
                    if cycle % 3 == 0:
                        allow_close(actor)
                        actor.page.goto('about:blank')
                        actor.page.goto(args.origin, wait_until='domcontentloaded')
                        connected(actor)
                        retained(actor)
                        record['actions'][-1]['reconnect'] = True
                    if cycle % 5 == 0:
                        allow_close(actor)
                        replacement = SoakActor(browser, user, args.origin)
                        attach(replacement)
                        replacement.login(); connected(replacement)
                        actor.page.locator('#status').filter(has_text='Viewer disconnected.').wait_for()
                        retained(replacement)
                        actor.context.close()
                        actors[user] = replacement
                        record['actions'][-1]['takeover'] = True
                resources()
                result['cycles'].append(record)
                save()
                print(f'soak cycle {cycle}: pass; workload {time.monotonic()-workload_start:.1f}s', flush=True)
                while time.monotonic()-cycle_start < args.interval and time.monotonic()-workload_start < args.seconds:
                    actors['alice'].page.wait_for_timeout(min(500, int(args.interval*1000)))
            result['workload_seconds'] = round(time.monotonic()-workload_start, 2)
            for user, actor in actors.items():
                retained(actor)
                actor.screenshot(f'{user}-final')
            assert not result['unexpected_closes'] and not result['browser_errors']
            result['status'] = 'pass'
        except Exception as error:
            result.update(status='fail', error=repr(error))
            for user, actor in actors.items():
                try:
                    actor.page.screenshot(path=str(output/f'{user}-failure.png'))
                except Exception:
                    pass
            raise
        finally:
            for guard in guards.values():
                guard['expected'].update(guard['sockets'])
            cleanup_errors = []
            for user, actor in actors.items():
                try:
                    if user in identities:
                        directory = workspace.STACK/'workspaces/users'/user/identities[user][1]
                        for name in ('launcher.json', 'qt.log'):
                            path = directory/name
                            if path.exists():
                                (output/f'{user}-{name}').write_bytes(path.read_bytes())
                    assert actor.api('DELETE')['status'] == 200
                except Exception as error:
                    cleanup_errors.append(f'{user}: {error!r}')
            try:
                workspace.wait_for(lambda: not suite.processes(), 15)
                listeners = suite.command('exec', 'swag-host', 'ss', '-ltnH').stdout
                endpoints = {line.split()[3] for line in listeners.splitlines()}
                assert all(f'127.0.0.1:{port}' not in endpoints for port in ports.values()), endpoints
            except Exception as error:
                cleanup_errors.append(repr(error))
            result['cleanup'] = dict(status='fail' if cleanup_errors else 'pass', errors=cleanup_errors)
            if cleanup_errors:
                result['status'] = 'fail'
            save()
            browser.close()
    return int(result['status'] != 'pass')


if __name__ == '__main__':
    raise SystemExit(main())
