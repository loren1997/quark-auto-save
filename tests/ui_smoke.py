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
    donghua = dict(item, id="bilibili:123", title="国漫周更测试", search_title="国漫周更测试",
                   source="Bilibili", heat_label="Bilibili 追番人数")
    failed_item = dict(item, id="bangumi:456", title="失败番剧", search_title="失败番剧")
    calendar = {"week_start": "2026-11-02", "week_end": "2026-11-08", "cached": True,
                "warnings": [], "notes": ["测试周播表"], "days": [
                    {"weekday": day, "label": f"星期{day}", "date": f"2026-11-{day + 1:02}",
                      "donghua": [donghua] if day == 1 else [],
                      "anime": [item] if day == 1 else [failed_item] if day == 3 else [],
                      "tv": []} for day in range(1, 8)]}
    web.get_calendar = lambda: calendar
    web.get_search_aliases = lambda entry: []
    web.search_resources = lambda *args, **kwargs: {
        "success": False, "data": [], "message": "PanSou: 搜索源暂时不可用", "errors": []}
    web.Quark.find_valid_resource = lambda self, task, config: (
        {"success": False, "code": "no_match", "error": "未找到可验证的视频资源",
         "queries": ["失败番剧", "Test Anime"], "source_errors": [{"source": "网页搜索", "message": "请求超时"}]}
        if "失败番剧" in task["taskname"] else
        {"success": True, "candidate": {"shareurl": "https://pan.quark.cn/s/abc123", "stoken": "NEVER_SAVE"}})
    web.reload_tasks = lambda: True
    web.task_plugins_config_default = {"aria2": {"auto_download": False, "download_subdir": False}}

    def fake_run():
        runs.append(request.get_json())
        return Response("data: 测试转存完成\n\ndata: [DONE]\n\n", content_type="text/event-stream")

    web.app.view_functions["run_script_now"] = fake_run
    with tempfile.TemporaryDirectory() as folder:
        web.CONFIG_PATH = str(Path(folder) / "config.json")
        config = json.loads((ROOT / "quark_config.json").read_text(encoding="utf-8"))
        config.update(webui={"username": "test", "password": "test-password"},
                      cookie=["TEST_COOKIE"], crontab="0 8 * * *")
        config.setdefault("plugins", {})["aria2"] = {"host_port": "", "secret": "", "dir": ""}
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
                page.get_by_role("heading", name="1. 国漫周更测试").wait_for()
                page.get_by_text("Bilibili · 1 部", exact=False).wait_for()
                page.get_by_role("button", name="动漫", exact=True).click()
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
                page.get_by_role("button", name="周三", exact=True).click()
                page.get_by_role("button", name="一键订阅并转存").click()
                page.get_by_role("alert").filter(has_text="《失败番剧》：未找到可验证的视频资源").wait_for()
                page.get_by_text("查看搜索详情", exact=True).click()
                assert page.get_by_text("已尝试关键词：失败番剧、Test Anime", exact=True).is_visible()
                assert page.get_by_text("网页搜索：请求超时", exact=True).is_visible()
                assert len(web.Config.read_json(web.CONFIG_PATH)["tasklist"]) == 2
                page.get_by_role("button", name="周二", exact=True).click()
                assert page.get_by_text("当天暂无已收录的更新", exact=False).first.is_visible()
                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_role("button", name="周一", exact=True).click()
                page.screenshot(path=str(output / "discovery-mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.set_viewport_size({"width": 1440, "height": 1000})

                # Saving the global toggle must refresh an existing subscription before its next run.
                assert page.evaluate("app.formData.tasklist.find(task => task.resource_id === 'bangumi:123').addition.aria2.auto_download") is False
                page.locator("a.nav-link").filter(has_text="系统配置").click()
                page.locator('[data-target="#collapse_aria2"]').click()
                for key, value in {"host_port": "192.0.2.1:6800", "secret": "ui-test-only-secret", "dir": "/Downloads"}.items():
                    page.locator("#collapse_aria2 .form-group").filter(
                        has=page.get_by_text(key, exact=True)
                    ).locator("input").fill(value)
                page.locator("#discovery-download").check()
                with page.expect_response(lambda response: response.url.endswith("/update")
                                          and response.request.method == "POST") as saved_response:
                    page.locator('button[data-original-title="保存 CTRL+S"], button[title="保存 CTRL+S"]').click()
                assert saved_response.value.json()["success"]
                page.wait_for_function("""() => {
                    const task = app.formData.tasklist.find(task => task.resource_id === 'bangumi:123');
                    return !app.configModified && task.addition.aria2.auto_download
                        && task.addition.aria2.download_subdir && task.addition.aria2.download_existing;
                }""")
                page.locator("a.nav-link").filter(has_text="任务列表").click()
                page.locator(".task").filter(has_text="测试动漫").get_by_title(
                    "运行此任务（失效时重试与自动换源）", exact=True
                ).click()
                page.locator("#logModal").wait_for(state="visible")
                page.wait_for_function("!app.modalLoading")
                assert len(runs) == 2
                retry = runs[1]["tasklist"][0]
                assert retry["resource_id"] == "bangumi:123"
                assert retry["addition"]["aria2"] == {
                    "auto_download": True, "download_subdir": True, "download_existing": True,
                    "save_path": "cartoon",
                }
                assert "ui-test-only-secret" not in json.dumps(runs)
                page.locator("#logModal .close").click()

                page.locator("a.nav-link").filter(has_text="任务列表").click()
                page.locator('[data-target="#collapse_0"]').click()
                page.locator('input[name="taskname[]"]').first.fill("测试搜索")
                page.get_by_text("PanSou: 搜索源暂时不可用", exact=True).wait_for()
                assert not errors, errors
                browser.close()
            print("UI smoke passed: login, donghua/source groups, seven days, subscribe/run, dedup, mobile, enable existing Aria2 download/save/refresh/run, contextual search errors; no JS errors")
        finally:
            server.shutdown()
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
