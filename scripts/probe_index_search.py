"""匿名只读核对 PanSou 实际目录及搜索结果，不读取账号配置或转存下载。"""

import argparse
from collections import Counter
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.sdk.pansou import PanSou
from app.sdk.common import SearchError
from resource_search import pansou_server, search_error_reason


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('query')
    parser.add_argument('--server', default=pansou_server({}))
    parser.add_argument('--verify', action='store_true', help='最多匿名检查前三个匹配分享是否含视频')
    args = parser.parse_args()
    client = PanSou(args.server)
    client.session.trust_env = False
    client.session.proxies = {scheme: value for scheme in ('http', 'https')
                              if (value := os.getenv(scheme.upper() + '_PROXY'))}
    # NO_PROXY must still apply to the local deployment service.
    if requests.utils.should_bypass_proxies(args.server, no_proxy=os.getenv('NO_PROXY')):
        client.session.proxies = {}
    started = time.monotonic()
    try:
        names = client.plugins()
        print(json.dumps({'installed_plugins': len(names), 'plugins': names}, ensure_ascii=False), flush=True)
        if not names:
            raise SearchError('PanSou 没有已启用且可查询的插件')
        rows = client.search(args.query, plugins=names, refresh=True)
        if not rows:
            print(json.dumps({'initial_candidates': 0, 'notice': '等待后台索引后读取一次缓存'}, ensure_ascii=False), flush=True)
            time.sleep(12)
            rows = client.search(args.query, plugins=names, refresh=False)
        counts = Counter(row.get('channel') if re.fullmatch(r'plugin:[a-z0-9_-]{1,64}', str(row.get('channel') or ''))
                         else 'unknown' for row in rows)
        print(json.dumps({'query': args.query, 'candidates': len(rows),
                          'by_plugin': dict(counts),
                          'seconds': round(time.monotonic() - started, 1)}, ensure_ascii=False), flush=True)
        if args.verify:
            with contextlib.redirect_stdout(io.StringIO()):
                from quark_auto_save import Quark
                result = Quark('')._find_verified_resource(
                    {'taskname': args.query}, rows, set(), [], max_checked=3, verify_index_title=False)
            print(json.dumps({'video_share_verified': bool(result['success']),
                              'notice': '仅验证前三个候选；未转存或下载'}, ensure_ascii=False), flush=True)
        return 0
    except SearchError as error:
        print(json.dumps({'error': search_error_reason(error)}, ensure_ascii=False), flush=True)
        return 2
    finally:
        client.session.close()


if __name__ == '__main__':
    raise SystemExit(main())
