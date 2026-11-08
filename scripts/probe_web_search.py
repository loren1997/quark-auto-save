"""只读实网诊断：搜索公开网页并验证匿名夸克目录，不转存或下载。"""

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.sdk.websearch import WebSearch
from quark_auto_save import Quark


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="公开网页搜索词")
    parser.add_argument("--title", help="待验证的准确片名，可含季数、年份")
    args = parser.parse_args()
    client = WebSearch()
    started = time.monotonic()
    try:
        candidates = client.search(args.query, refresh=True)
        print(json.dumps({"query": args.query, "seconds": round(time.monotonic() - started, 2),
                          "candidates": len(candidates), "errors": client.errors,
                          "pages": list(dict.fromkeys(item["page_url"] for item in candidates))}, ensure_ascii=False), flush=True)
        result = Quark()._find_verified_resource(
            {"taskname": args.title or args.query, "resource_type": "anime"},
            candidates, set(), [], max_checked=3, verify_index_title=False,
        )
        print(json.dumps({"verified": result["success"], "error": result["error"],
                          "title": (result.get("candidate") or {}).get("taskname"),
                          "seconds": round(time.monotonic() - started, 2)}, ensure_ascii=False), flush=True)
        return 0 if result["success"] else 1
    finally:
        client.session.close()


if __name__ == "__main__":
    raise SystemExit(main())
