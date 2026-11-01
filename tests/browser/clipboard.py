#!/usr/bin/env python3
"""Unicode clipboard through actual authenticated browser/noVNC/Qt actions."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
import workspace


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin',default='http://localhost:6081')
    parser.add_argument('--output',type=Path,default=workspace.ROOT/'artifacts/qt-ws-clipboard/browser')
    args=parser.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    workspace.OUTPUT=output
    results=[]
    current='start'
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='chrome',headless=True)
        actor=workspace.Actor(browser,'alice',args.origin)
        try:
            actor.login()
            actor.api('DELETE'); actor.api('POST')
            actor.page.reload(wait_until='domcontentloaded'); actor.connected()
            identity=(actor.session['pid'],actor.session['generation'])

            def passed(**detail):
                results.append(dict(check=current,status='pass',**detail))
                (output/'results.json').write_text(json.dumps({'checks':results},indent=2)+'\n')
                print(f'clipboard: {current}: pass',flush=True)

            def send(text):
                actor.open_clipboard()
                actor.page.locator('#clipboard').fill(text)
                actor.page.locator('#paste').click()
                actor.click('draft')
                actor.page.keyboard.press('Control+a')
                actor.page.keyboard.press('Backspace')
                actor.page.keyboard.press('Control+v')
                actor.wait(lambda s:s.get('draft')==text)

            def copy(expected):
                # Do not allow the browser's previous inbound value to masquerade
                # as evidence of an outbound clipboard event.
                actor.open_clipboard()
                actor.page.locator('#clipboard').fill('waiting-for-Qt-copy')
                actor.page.locator('#clipboard-close').click()
                actor.click('draft')
                actor.page.keyboard.press('Control+a')
                actor.page.keyboard.press('Control+c')
                actor.page.wait_for_function("text => document.querySelector('#clipboard').value===text",arg=expected)

            for current,text in [
                ('currency_and_multilingual_roundtrip','€ CHF Zürich Łódź — Ж 東京 😀'),
                ('combining_and_rtl_roundtrip','Cafe\u0301 • Ελληνικά • العربية • עברית'),
                ('latin1_roundtrip','Café £ déjà vu'),
            ]:
                send(text); copy(text)
                passed(text=text,both_directions=True)
            current='empty_clipboard_clears_previous_text'
            send('stale clipboard sentinel')
            send('')
            # Give noVNC's notify/request/provide exchange time to finish before
            # another paste; the text must remain empty, not the previous value.
            actor.page.wait_for_timeout(300)
            actor.page.keyboard.press('Control+v')
            actor.page.wait_for_timeout(300)
            assert actor.state()['draft']==''
            passed()
            current='unicode_survives_reconnect_and_copy'
            text='Rejoin € Zürich Ж 😀'
            send(text)
            actor.page.reload(wait_until='domcontentloaded'); actor.connected()
            assert actor.state()['draft']==text
            assert (actor.session['pid'],actor.session['generation'])==identity
            copy(text)
            actor.screenshot('unicode-reconnected')
            passed(same_process=True,both_directions=True)
            actor.api('DELETE')
        except Exception as error:
            results.append(dict(check=current,status='fail',error=str(error)))
            (output/'results.json').write_text(json.dumps({'checks':results},indent=2)+'\n')
            actor.page.screenshot(path=str(output/'failure.png'))
            raise
        finally:
            browser.close()


if __name__=='__main__':
    main()
