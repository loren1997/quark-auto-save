"""Optional browser smoke test: python tests/ui_smoke.py (requires Playwright)."""
import json
import sys
import tempfile
import threading
from pathlib import Path

from flask import Response, request
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
import run as web


def main():
    output = ROOT / "test-results"
    output.mkdir(exist_ok=True)
    errors, runs = [], []
    item = {"id": "bangumi:123", "title": "测试动漫", "search_title": "测试动漫",
            "year": "2026", "source": "Bangumi", "source_url": "https://bgm.tv/subject/123",
            "poster": "", "heat": 100, "heat_label": "Bangumi 在看人数", "schedule_note": "周播表"}
    calendar = {"week_start": "2026-11-02", "week_end": "2026-11-08", "cached": True,
                "warnings": [], "notes": ["测试周播表"], "days": [
                    {"weekday": day, "label": f"星期{day}", "date": f"2026-11-{day + 1:02}",
                     "anime": [item] if day == 1 else [], "tv": []} for day in range(1, 8)]}
    web.get_calendar = lambda: calendar
    web.search_resources = lambda *args, **kwargs: {
        "success": False, "data": [], "message": "PanSou: 搜索源暂时不可用", "errors": []}
    web.Quark.find_valid_resource = lambda *args: {
        "success": True, "candidate": {"shareurl": "https://pan.quark.cn/s/abc123", "stoken": "NEVER_SAVE"}}
    web.reload_tasks = lambda: True
    web.task_plugins_config_default = {}

    def fake_run():
        runs.append(request.get_json())
        return Response("data: 测试转存完成\n\ndata: [DONE]\n\n", content_type="text/event-stream")

    web.app.view_functions["run_script_now"] = fake_run
    with tempfile.TemporaryDirectory() as folder:
        web.CONFIG_PATH = str(Path(folder) / "config.json")
        config = json.loads((ROOT / "quark_config.json").read_text(encoding="utf-8"))
        config.update(webui={"username": "test", "password": "test-password"},
                      cookie=["TEST_COOKIE"], crontab="0 8 * * *")
        config["tasklist"] = [{"taskname": "已有任务", "shareurl": "", "savepath": "/测试",
                               "pattern": "", "replace": "", "runweek": list(range(1, 8))}]
        web.Config.write_json(web.CONFIG_PATH, config)
        web.config_data = config
        server = make_server("127.0.0.1", 0, web.app, threaded=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("https://api.github.com/**", lambda route: route.fulfill(json=[{"name": "dev"}]))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.locator('input[name="username"]').fill("test")
                page.locator('input[name="password"]').fill("test-password")
                page.locator('button[type="submit"]').click()
                page.get_by_role("link", name="热门追更").click()
                page.get_by_role("button", name="周一", exact=True).click()
                page.get_by_role("heading", name="1. 测试动漫").wait_for()
                assert page.get_by_role("group", name="更新星期").get_by_role("button").count() == 7
                page.screenshot(path=str(output / "discovery-desktop.png"), full_page=True)
                page.wait_for_function("!app.configModified")
                page.get_by_role("button", name="一键订阅并转存").click()
                page.wait_for_function("app.formData.tasklist.some(task => task.resource_id === 'bangumi:123')")
                page.locator("#logModal").wait_for(state="visible")
                page.wait_for_function("!app.modalLoading")
                assert len(runs) == 1 and runs[0]["tasklist"][0]["resource_id"] == "bangumi:123"
                saved = web.Config.read_json(web.CONFIG_PATH)
                assert len(saved["tasklist"]) == 2 and "NEVER_SAVE" not in json.dumps(saved)
                page.locator("#logModal .close").click()
                page.get_by_role("button", name="已订阅", exact=True).wait_for()
                assert page.get_by_role("button", name="已订阅", exact=True).is_disabled()
                page.get_by_role("button", name="周二", exact=True).click()
                assert page.get_by_text("当天暂无已收录的更新", exact=False).first.is_visible()
                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_role("button", name="周一", exact=True).click()
                page.screenshot(path=str(output / "discovery-mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.locator("a.nav-link").filter(has_text="任务列表").click()
                page.locator('[data-target="#collapse_0"]').click()
                page.locator('input[name="taskname[]"]').first.fill("测试搜索")
                page.get_by_text("PanSou: 搜索源暂时不可用", exact=True).wait_for()
                assert not errors, errors
                browser.close()
            print("UI smoke passed: login, seven days, subscribe/run, dedup, mobile, search errors; no JS errors")
        finally:
            server.shutdown()
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
