"""Anonymous cloud diagnostics; never load application credentials or transfer files."""
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
from app.sdk.channel_registry import ChannelRegistry
from app.sdk.discovery import get_search_aliases
from quark_auto_save import Config, Quark, _resource_episode_number

mode = sys.argv[1]
output = ROOT / 'test-results'
output.mkdir(exist_ok=True)
out = output / f'v310-live-{mode}.json'
started = time.monotonic()
def expired():
    out.write_text(json.dumps({'success': False, 'deadline_reached': True}), encoding='utf-8')
    os._exit(124)
timer = threading.Timer(270, expired)
timer.daemon = True
timer.start()
with (output / f'v310-live-{mode}.log').open('w', encoding='utf-8') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
    try:
        with tempfile.TemporaryDirectory() as temporary:
            if mode == 'registry':
                registry = ChannelRegistry(str(Path(temporary) / 'health.sqlite'))
                state = registry.maintain({})
                names = registry.active_channels([])
                result = {'success': bool(names), 'active_channels': names, 'state': state,
                          'empty_registry': True, 'official_directory_only': True}
            else:
                title = '乱世千金倪亚‧利斯顿转生为娇弱千金的弑神武人华丽无双录'
                aliases = get_search_aliases({'id': 'bangumi:624085', 'title': title})
                task = {'taskname': title + ' (2026)', 'resource_type': 'anime',
                        'resource_id': 'bangumi:624085', 'search_aliases': aliases,
                        'shareurl': 'https://pan.quark.cn/s/17befa71ef71'}
                account = Quark()
                account.resource_history_path = str(Path(temporary) / 'history.json')
                account.search_health_path = str(Path(temporary) / 'health.sqlite')
                result = account.find_valid_resource(task, {})
                candidate = result.get('candidate')
                episodes = []
                if candidate:
                    listing = account.get_detail(candidate['pwd_id'], candidate['stoken'], candidate['pdir_fid'])
                    episodes = sorted({ep for item in listing['data']['list']
                                       if (ep := _resource_episode_number(task, item.get('file_name', ''))) is not None})
                    Config.remember_resources(account.resource_history_path, [{**task, 'shareurl': candidate['shareurl']}])
                    restarted = Quark()
                    restarted.resource_history_path = account.resource_history_path
                    restarted.search_health_path = account.search_health_path
                    fill = restarted.find_valid_resource(task, {}, required_episodes={2})
                    result['missing_episode_verified'] = fill['success']
                    result['selected_missing_episodes'] = sorted(set((fill.get('candidate') or {}).get('episode_fids', {}).values()))
                    result['candidate'] = {key: candidate.get(key) for key in ('taskname', 'shareurl', 'pdir_fid', 'source')}
                result.update(aliases=aliases, episodes=episodes, empty_history=True,
                              seeded_candidates=False, default_sources=True)
            result.update(anonymous_read=True, transfer=False, seconds=round(time.monotonic()-started, 2))
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as error:
        import traceback
        traceback.print_exc()
        out.write_text(json.dumps({'success': False, 'error_type': type(error).__name__}), encoding='utf-8')
    finally:
        timer.cancel()
