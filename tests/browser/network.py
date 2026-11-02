#!/usr/bin/env python3
"""Measure real authenticated noVNC input under synthetic stream delay/rate caps.

Temporarily routes nginx's authenticated session upstream through a test-only
sidecar, restores its exact configuration in finally, and never logs payloads.
Latency numbers are observations, not a production SLO or WAN qualification.
"""
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import time
import uuid

from playwright.sync_api import sync_playwright
import workspace

ROOT = Path(__file__).resolve().parents[2]
PROFILES = [dict(name='local',rtt_ms=0,download_mbps=0,upload_mbps=0),
            dict(name='rtt50_down20_up5',rtt_ms=50,download_mbps=20,upload_mbps=5),
            dict(name='rtt100_down5_up1',rtt_ms=100,download_mbps=5,upload_mbps=1)]
PIXEL = """({x,y}) => Array.from(document.querySelector('#screen canvas').getContext('2d').getImageData(x,y,1,1).data)"""
WAIT_PIXEL = """({x,y,color}) => {
 const c=document.querySelector('#screen canvas');
 if (!c || x>=c.width || y>=c.height) return false;
 return Array.from(c.getContext('2d').getImageData(x,y,1,1).data).every((v,i)=>v===color[i]);
}"""
BLUE_PIXEL = """({x,y}) => {
 const c=document.querySelector('#screen canvas');
 if (!c || x>=c.width || y>=c.height) return false;
 const p=c.getContext('2d').getImageData(x,y,1,1).data;
 return p[2]>p[0]+40 && p[3]===255;
}"""


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine',default='podman')
    parser.add_argument('--origin',default='http://localhost:6081')
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/qt-ws-network')
    parser.add_argument('--samples',type=int,default=12)
    parser.add_argument('--profile',choices=[p['name'] for p in PROFILES])
    parser.add_argument('--cursor-transitions',action='store_true',
                        help='also measure row paints after moving from a text cursor')
    args=parser.parse_args()
    if not 3 <= args.samples <= 100:
        parser.error('--samples must be between 3 and 100')
    selected=[p for p in PROFILES if args.profile is None or p['name']==args.profile]
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    profiles=output/'relay'
    profiles.mkdir(exist_ok=True)
    run_id=uuid.uuid4().hex
    config=workspace.STACK/'config/nginx.conf'
    original=config.read_bytes()
    marker=b'set $session_backend swag-host:8080;'
    assert original.count(marker)==1, 'unexpected nginx upstream configuration'
    settings=json.loads((workspace.STACK/'public.json').read_text())
    assert settings['origin']==args.origin and settings['render_backend']=='qt-ws'
    name='swag-network-profile'
    results=[]
    changed=started=False

    def command(*parts,check=True):
        return subprocess.run([args.engine,*map(str,parts)],check=check,text=True,
                              capture_output=True,timeout=60)

    def set_profile(profile):
        temp=profiles/'profile.tmp'
        temp.write_text(json.dumps(profile)+'\n')
        temp.replace(profiles/'profile.json')

    def write_config(data):
        # nginx.conf is a file bind mount. Replacing the host inode would leave
        # the running container reading the old inode on reload.
        with config.open('r+b') as stream:
            stream.seek(0)
            if stream.write(data)!=len(data):
                raise OSError('short nginx configuration write')
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())

    def drained_records(profile):
        deadline=time.monotonic()+10
        while True:
            active=[]
            for path in profiles.glob(f'active-{run_id}-*.json'):
                try:
                    if json.loads(path.read_text())['profile']==profile['name']:
                        active.append(path)
                except FileNotFoundError:
                    pass  # Closed after glob, before this read.
            if not active:
                break
            if time.monotonic()>=deadline:
                raise TimeoutError(f'{len(active)} shaped connections still active for {profile["name"]}')
            time.sleep(.1)
        records=[json.loads(path.read_text()) for path in
                profiles.glob(f'connection-{run_id}-*.json')
                if json.loads(path.read_text())['profile']==profile['name']]
        if not records:
            raise RuntimeError(f'no shaped connection records for {profile["name"]}')
        return records

    def resources(pid):
        code="""import json,os,pathlib,sys
p=pathlib.Path('/proc')/sys.argv[1]
s=(p/'stat').read_text().rsplit(')',1)[1].split()
f=dict(line.split(':',1) for line in (p/'status').read_text().splitlines())
print(json.dumps({'cpu_seconds':(int(s[11])+int(s[12]))/os.sysconf('SC_CLK_TCK'),
 'rss_kib':int(f['VmRSS'].split()[0]),'peak_rss_kib':int(f['VmHWM'].split()[0])}))
"""
        return json.loads(command('exec','swag-host','python3.12','-c',code,str(pid)).stdout)

    try:
        assert command('container','inspect',name,check=False).returncode != 0, 'test sidecar already exists'
        set_profile(PROFILES[0])
        keep=['--userns=keep-id:uid=1000,gid=1000'] if Path(args.engine).name=='podman' else []
        command('run','-d','--name',name,'--network','swag-net',*keep,
                '-e',f'SWAG_PROFILE_RUN_ID={run_id}',
                '-v',f'{ROOT}/tests/network_shaper.py:/tmp/shaper.py:ro,Z',
                '-v',f'{profiles}:/profiles:Z','--entrypoint','python3.12',
                'localhost/swag-app-ws:dev','/tmp/shaper.py')
        started=True
        workspace.wait_for(lambda:'ready' in command('logs',name).stdout,10)
        changed=True
        write_config(original.replace(marker,b'set $session_backend swag-network-profile:8089;'))
        command('exec','swag-edge','nginx','-t')
        command('exec','swag-edge','nginx','-s','reload')
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=True)
            try:
                for profile in selected:
                    set_profile(profile)
                    actor=workspace.Actor(browser,'alice',args.origin)
                    actor.page.set_default_timeout(45000)
                    result={'profile':profile,'status':'running'}
                    results.append(result)
                    try:
                        actor.login()
                        api_latencies=[]
                        for _ in range(3):
                            begin=time.perf_counter()
                            assert actor.api()['status']==200
                            api_latencies.append(round((time.perf_counter()-begin)*1000,2))
                        result['session_roundtrip_ms']=api_latencies
                        assert min(api_latencies)>=profile['rtt_ms']*.8, 'configured delay was not observed'
                        actor.api('DELETE')
                        actor.api('POST')
                        begin=time.perf_counter()
                        actor.page.reload(wait_until='domcontentloaded')
                        actor.connected()
                        actor.wait(lambda s:s.get('geometry',{}).get('window')==[0,0,*actor.framebuffer_size()])
                        initial=actor.state()
                        assert len(initial['items'])>=2
                        row0=initial['geometry']['grid_row_0']
                        row1=initial['geometry']['grid_row_1']
                        points=[{'x':int(r[0]+6),'y':int(r[1]+r[3]-4)} for r in (row0,row1)]
                        # The selected row's background must be visibly blue, not
                        # just different from an earlier screenshot.
                        actor.page.wait_for_function(BLUE_PIXEL,arg=points[0],timeout=45000,polling=20)
                        result['connect_to_first_selected_row_ms']=round((time.perf_counter()-begin)*1000,2)
                        # Establish keyboard/window focus before sampling the
                        # active-selection color; inactive Qt selection differs.
                        for target in (1,0):
                            rect=actor.state()['geometry'][f'grid_row_{target}']
                            actor.page.mouse.click(*actor.canvas_point(rect))
                            actor.page.wait_for_function(BLUE_PIXEL,arg=points[target],timeout=45000,polling=20)
                            actor.wait(lambda s:s.get('selected_id')==initial['items'][target]['id'],45)
                        color=actor.page.evaluate(PIXEL,points[0])
                        result['selection_rgba']=color
                        before=resources(actor.session['pid'])
                        samples=[]
                        for index in range(args.samples):
                            target=1 if index%2==0 else 0
                            point=points[target]
                            assert actor.page.evaluate(PIXEL,point)!=color
                            rect=actor.state()['geometry'][f'grid_row_{target}']
                            begin=time.perf_counter()
                            actor.page.mouse.click(*actor.canvas_point(rect))
                            actor.page.wait_for_function(WAIT_PIXEL,arg={**point,'color':color},
                                                         timeout=45000,polling=20)
                            visible=(time.perf_counter()-begin)*1000
                            actor.wait(lambda s:s.get('selected_id')==initial['items'][target]['id'],45)
                            samples.append(round(visible,2))
                        if args.cursor_transitions:
                            cursor_samples=[]
                            arrow_cursor=actor.page.locator('#screen canvas').evaluate('e => e.style.cursor')
                            assert arrow_cursor.startswith('url('), 'native noVNC cursor expected'
                            for index in range(args.samples):
                                # Real hover over the editor establishes an I-beam;
                                # the following real click changes cursor and row paint.
                                actor.page.mouse.move(*actor.canvas_point(actor.state()['geometry']['draft']))
                                actor.page.wait_for_function("old => { const s=document.querySelector('#screen canvas').style.cursor; return s.startsWith('url(') && s!==old; }", arg=arrow_cursor, timeout=45000)
                                target=1 if actor.state()['selected_id']==initial['items'][0]['id'] else 0
                                point=points[target]
                                assert actor.page.evaluate(PIXEL,point)!=color
                                begin=time.perf_counter()
                                actor.page.mouse.click(*actor.canvas_point(actor.state()['geometry'][f'grid_row_{target}']))
                                actor.page.wait_for_function(WAIT_PIXEL,arg={**point,'color':color},
                                                             timeout=45000,polling=20)
                                cursor_samples.append(round((time.perf_counter()-begin)*1000,2))
                                actor.page.wait_for_function("old => document.querySelector('#screen canvas').style.cursor===old", arg=arrow_cursor, timeout=45000)
                                actor.wait(lambda s:s.get('selected_id')==initial['items'][target]['id'],45)
                            result.update(cursor_transition_to_row_paint_ms=cursor_samples,
                                cursor_transition_median_ms=round(statistics.median(cursor_samples),2),
                                cursor_transition_p95_ms=sorted(cursor_samples)[math.ceil(len(cursor_samples)*.95)-1])
                        after=resources(actor.session['pid'])
                        result.update(command_to_visible_ms=samples,
                            median_ms=round(statistics.median(samples),2),
                            p95_ms=sorted(samples)[math.ceil(len(samples)*.95)-1],
                            qt_cpu_seconds=round(after['cpu_seconds']-before['cpu_seconds'],3),
                            qt_rss_kib=after['rss_kib'],qt_peak_rss_kib=after['peak_rss_kib'])
                        old_size=actor.framebuffer_size()
                        corner=actor.page.evaluate(PIXEL,{'x':old_size[0]-5,'y':old_size[1]-5})
                        assert corner[3]==255
                        begin=time.perf_counter()
                        actor.page.set_viewport_size({'width':1680,'height':1100})
                        actor.page.wait_for_function("""() => {
                          const c=document.querySelector('#screen canvas'),s=document.querySelector('#screen');
                          return c.width===1680 && c.height===Math.floor(s.clientHeight);
                        }""",timeout=45000)
                        size=actor.framebuffer_size()
                        actor.page.wait_for_function(WAIT_PIXEL,arg={'x':size[0]-5,'y':size[1]-5,'color':corner},
                                                     timeout=45000,polling=20)
                        result['resize_to_bottom_right_paint_ms']=round((time.perf_counter()-begin)*1000,2)
                        actor.page.screenshot(path=str(output/(profile['name']+'.png')))
                        assert actor.page.locator('#status').inner_text()=='Connected'
                        result['status']='pass'
                        actor.api('DELETE')
                    except Exception as error:
                        result.update(status='fail',error=str(error))
                        actor.page.screenshot(path=str(output/(profile['name']+'-failure.png')))
                    finally:
                        actor.context.close()
                        try:
                            records=drained_records(profile)
                            result['up_bytes']=sum(record['up_bytes'] for record in records)
                            result['down_bytes']=sum(record['down_bytes'] for record in records)
                            result['connection_records']=len(records)
                            errors=[error for record in records for error in
                                    (record.get('error'),record.get('up_bytes_read_error'),
                                     record.get('down_bytes_read_error')) if error]
                            if errors:
                                raise RuntimeError(f'shaper connection errors: {errors}')
                            result['byte_totals_complete']=True
                        except Exception as error:
                            result.update(status='fail',byte_totals_complete=False,
                                          error=str(error))
                        (output/'results.json').write_text(json.dumps({'profiles':results,
                            'method':'Playwright mouse.click command to observed noVNC canvas pixel, including automation overhead and 20ms polling; independent Qt state checked afterward.',
                            'traffic_scope':'Bytes forwarded by the shaper for session-upstream HTTP (/session and /session/lease) plus viewer WebSocket; excludes auth and static assets.',
                            'limits':'Synthetic added stream delay and rate caps; no packet loss, TLS, Windows/RDP, concurrency or production SLO qualification.'},indent=2)+'\n')
                        print(f"network {profile['name']}: {result['status']}",flush=True)
            finally:
                browser.close()
    finally:
        # Restoring the production-shaped route takes priority over sidecar cleanup.
        restored=False
        try:
            if changed:
                write_config(original)
                assert config.read_bytes()==original
                command('exec','swag-edge','nginx','-t')
                command('exec','swag-edge','nginx','-s','reload')
            restored=True
        finally:
            if started and restored:
                command('rm','-f',name,check=False)
            elif started:
                print('nginx restoration failed; leaving the network sidecar running',flush=True)
    return int(any(result['status']!='pass' for result in results))


if __name__=='__main__':
    raise SystemExit(main())
