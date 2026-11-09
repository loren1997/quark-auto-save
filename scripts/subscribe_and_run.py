"""Create one discovery subscription and run it in the managed subprocess."""

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

import run as web
import quark_auto_save as qas
from run_progress import report_progress


def main():
    web.CONFIG_PATH = sys.argv[1]
    payload = json.loads(os.environ.get("QAS_RUN_PAYLOAD", "{}"))
    report_progress("start", task=payload.get("discovery_id", "热门订阅"))
    print("正在读取热门周历，查找作品及可用分享", flush=True)
    web.config_data = qas.Config.read_json(web.CONFIG_PATH)
    cookies = web.config_data.get("cookie") or []
    cookie = cookies[0] if isinstance(cookies, list) and cookies else cookies
    if not isinstance(cookie, str) or not cookie.strip() or cookie.startswith("Your "):
        print("订阅未完成：请先在系统配置中保存有效的夸克 Cookie", flush=True)
        return 1
    _, _, web.task_plugins_config_default = qas.Config.load_plugins()
    with web.app.app_context():
        response = web.subscribe_discovery_payload({"id": payload.get("discovery_id"),
                                                   **({"shareurl": payload["shareurl"]} if "shareurl" in payload else {})})
        response = response[0] if isinstance(response, tuple) else response
        result = response.get_json()
    if not result.get("success"):
        print("订阅未完成：" + str(result.get("message", "资源验证失败")), flush=True)
        for error in result.get("source_errors", []):
            if isinstance(error, dict):
                print(f"来源：{error.get('source', '')}；{error.get('message', '')}", flush=True)
        if result.get("queries"):
            print("已查询关键词：" + "、".join(str(value) for value in result["queries"]), flush=True)
        return 1
    task = dict(result["data"])
    task.pop("runweek", None)
    task.pop("enddate", None)
    os.environ["TASKLIST"] = json.dumps([task], ensure_ascii=False)
    print(result.get("message", "订阅已就绪，开始转存"), flush=True)
    qas.main()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"订阅运行失败（{type(error).__name__}），请检查配置与网络后重试", flush=True)
        raise SystemExit(1)
