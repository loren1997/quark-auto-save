import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests

# Only this diagnostic process explicitly uses the workstation's existing proxy.
proxy = requests.utils.get_environ_proxies('https://meijusou.com')
for scheme in ('http', 'https'):
    if proxy.get(scheme):
        os.environ[scheme.upper() + '_PROXY'] = proxy[scheme]
from app.sdk.discovery import get_search_aliases
from quark_auto_save import Quark, _resource_episode_number

index = int(sys.argv[1])
jobs = [(625799, '被放逐的作弊赋予魔术师享受随心所欲的第二人生。'),
        (624085, '乱世千金妮亚·利斯顿转生为娇弱千金的弑神武人华丽无双录')]
sid, title = jobs[index]
(ROOT / 'test-results').mkdir(exist_ok=True)
out = ROOT / f'test-results/v39-full-live-{index}.json'
started = time.monotonic()
def expired():
    out.write_text(json.dumps({'success': False, 'deadline_reached': True}), encoding='utf-8')
    os._exit(124)
timer = threading.Timer(240, expired)
timer.daemon = True
timer.start()
with (ROOT / f'test-results/v39-full-live-{index}.log').open('w', encoding='utf-8') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
    try:
        aliases = get_search_aliases({'id': f'bangumi:{sid}', 'title': title})
        task = {'taskname': title + ' (2026)', 'resource_type': 'anime', 'resource_id': f'bangumi:{sid}', 'search_aliases': aliases}
        with tempfile.TemporaryDirectory() as temporary:
            account = Quark()
            account.resource_history_path = str(Path(temporary) / 'history.json')
            account.search_health_path = str(Path(temporary) / 'health.sqlite')
            os.environ['QAS_RUN_PROGRESS'] = '1'
            result = account.find_valid_resource(task, {})
            candidate = result.get('candidate')
            episodes = []
            if candidate:
                pwd, token = candidate['pwd_id'], candidate['stoken']
                listing = account.get_detail(pwd, token, candidate['pdir_fid'])
                episodes = sorted({ep for item in listing['data']['list'] if (ep := _resource_episode_number(task, item.get('file_name', ''))) is not None})
                result['candidate'] = {key: candidate.get(key) for key in ('taskname', 'shareurl', 'pdir_fid', 'source')}
            result.update(aliases=aliases, episodes=episodes, empty_history=True, seeded_candidates=False, default_sources=True,
                          anonymous_read=True, transfer=False, explicit_workstation_proxy=bool(proxy), seconds=round(time.monotonic()-started,2))
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as error:
        import traceback
        traceback.print_exc()
        out.write_text(json.dumps({'success': False, 'error_type': type(error).__name__}), encoding='utf-8')
    finally:
        timer.cancel()
