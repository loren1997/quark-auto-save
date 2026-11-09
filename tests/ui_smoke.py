"""Optional browser smoke test: python tests/ui_smoke.py (requires Playwright)."""
import json
import sys
import tempfile
import threading
from pathlib import Path

from flask import jsonify, request
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
import run as web


def main():
    output = ROOT / "test-results"
    output.mkdir(exist_ok=True)
    errors, runs, link_checks = [], [], []
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
    web.get_calendar = lambda **kwargs: calendar
    web.get_search_aliases = lambda entry: []
    web.search_resources = lambda *args, **kwargs: {
        "success": False, "data": [], "message": "PanSou: 搜索源暂时不可用", "errors": []}
    web.Quark.find_valid_resource = lambda self, task, config: (
        {"success": False, "code": "no_match", "error": "未找到可验证的视频资源",
         "queries": ["失败番剧", "Test Anime"], "source_errors": [{"source": "网页搜索", "message": "请求超时"}]}
        if "失败番剧" in task["taskname"] else
        {"success": True, "candidate": {"shareurl": "https://pan.quark.cn/s/abc123", "stoken": "NEVER_SAVE"}})

    def verify_link(self, task, shareurl):
        link_checks.append((task["taskname"], shareurl))
        return {"success": True, "candidate": {"shareurl": shareurl, "stoken": "NEVER_SAVE"}}

    web.Quark.verify_resource_link = verify_link
    web.reload_tasks = lambda: True
    web.task_plugins_config_default = {"aria2": {"auto_download": False, "download_subdir": False}}

    records = []
    hold_list = threading.Event()
    list_started = threading.Event()
    release_list = threading.Event()
    hold_detail = threading.Event()
    detail_started = threading.Event()
    release_detail = threading.Event()
    extra_lines = []

    def fake_runs():
        if request.method == "GET":
            snapshot = json.loads(json.dumps(records))
            if hold_list.is_set():
                hold_list.clear()
                list_started.set()
                assert release_list.wait(10), "test did not release the delayed list response"
            return jsonify(success=True, data=snapshot)
        payload = request.get_json()
        runs.append(payload)
        record = dict(id=f"{len(runs):032x}", status="running", trigger="discovery" if "discovery_id" in payload else "manual",
                      task="测试动漫" if payload.get("discovery_id") == item["id"] else "失败番剧" if "discovery_id" in payload else "测试动漫",
                      stage="search", source="站内搜索 Meijusou", query="测试动漫 第2集",
                      created_at="2026-11-10T01:00:00+08:00", started_at="2026-11-10T01:00:00+08:00",
                      finished_at=None, error="", exit_code=None, discovery_id=payload.get("discovery_id", ""))
        if "discovery_id" in payload and any(row["status"] == "running" for row in records):
            record.update(status="queued", started_at=None)
        records.insert(0, record)
        return jsonify(success=True, data=record), 202

    def fake_detail(run_id):
        record = next(record for record in records if record["id"] == run_id)
        lines = ["正在查询站内来源", "正在验证候选分享中的第 2 集"]
        if record["status"] in {"failed", "waiting"}:
            lines.append("网页搜索：请求超时；未找到可验证的视频资源，订阅未创建")
        elif record["status"] == "succeeded":
            lines.append("测试转存完成，Aria2 已接收下载任务")
            lines.extend(extra_lines)
        after = int(request.args.get("after", "0"))
        if hold_detail.is_set():
            hold_detail.clear()
            detail_started.set()
            assert release_detail.wait(10), "test did not release delayed log response"
        return jsonify(success=True, data=dict(record, cursor=len(lines), first_cursor=1, truncated=False,
                       logs=[dict(cursor=i+1, text=line) for i, line in enumerate(lines) if i+1 > after]))

    def finish_run():
        record, payload = records[0], runs[-1]
        if payload.get("discovery_id"):
            with web.app.app_context():
                response = web.subscribe_discovery_payload({"id": payload["discovery_id"]})
                data = (response[0] if isinstance(response, tuple) else response).get_json()
            record.update(status="succeeded" if data["success"] else "waiting" if data.get("pending") else "failed", error=data.get("message", "") if not data["success"] else "")
        else:
            record["status"] = "succeeded"
        record.update(finished_at="2026-11-10T01:01:00+08:00", exit_code=0 if record["status"] == "succeeded" else 1)

    web.app.view_functions["runs"] = fake_runs
    web.app.view_functions["run_detail"] = fake_detail
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
                page.on("console", lambda message: print("Browser console:", message.text)
                        if message.type == "error" and "Vue warn" in message.text else None)
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
                page.wait_for_function("!app.runsLoading")
                hold_list.set()
                page.evaluate("void app.refreshRuns()")
                assert list_started.wait(5)
                page.get_by_role("button", name="一键订阅并转存").click()
                page.wait_for_function("app.backgroundRun && !app.runRequestPending")
                release_list.set()
                page.wait_for_function("!app.runsLoading")
                assert page.evaluate("app.backgroundRun.discovery_id") == "bangumi:123"
                assert page.evaluate("app.activeTab") == "discovery"
                assert page.locator("#logModal").count() == 0
                assert runs == [{"discovery_id": "bangumi:123"}]
                assert len(web.Config.read_json(web.CONFIG_PATH)["tasklist"]) == 1
                page.get_by_role("button", name="查看日志", exact=True).click()
                page.get_by_text("当前来源：", exact=False).wait_for()
                assert "Meijusou" in page.locator("section[aria-labelledby='runs-heading']").inner_text()
                page.get_by_label("运行日志内容").filter(has_text="第 2 集").wait_for()
                # Browser refresh must restore the active job and its incremental log.
                page.reload()
                page.locator("a.nav-link").filter(has_text="运行日志").click()
                page.get_by_label("运行日志内容").filter(has_text="第 2 集").wait_for()
                assert len(runs) == 1
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.screenshot(path=str(output / "runs-mobile.png"), full_page=True)
                page.set_viewport_size({"width": 1440, "height": 1000})
                finish_run()
                page.wait_for_function("!app.backgroundRun && app.formData.tasklist.some(task => task.resource_id === 'bangumi:123')")
                page.get_by_label("运行日志内容").filter(has_text="Aria2 已接收").wait_for()
                saved = web.Config.read_json(web.CONFIG_PATH)
                assert len(saved["tasklist"]) == 2 and "NEVER_SAVE" not in json.dumps(saved)
                page.screenshot(path=str(output / "runs-desktop.png"), full_page=True)
                page.get_by_role("link", name="热门追更").click()
                page.get_by_role("button", name="动漫", exact=True).click()
                page.get_by_role("button", name="周一", exact=True).click()
                page.get_by_role("button", name="已订阅", exact=True).wait_for()
                assert page.get_by_role("button", name="已订阅", exact=True).is_disabled()
                page.get_by_role("button", name="周三", exact=True).click()
                page.get_by_role("button", name="一键订阅并转存").click()
                page.wait_for_function("app.backgroundRun && !app.runRequestPending")
                finish_run()
                page.get_by_role("button", name="查看日志", exact=True).click()
                page.get_by_label("运行日志内容").filter(has_text="未找到可验证的视频资源").wait_for()
                assert len(web.Config.read_json(web.CONFIG_PATH)["tasklist"]) == 2
                assert page.locator("#run-history option").count() == 2
                # Previous completed runs remain readable without repeating a transfer.
                page.locator("#run-history").select_option(records[1]["id"])
                page.get_by_label("运行日志内容").filter(has_text="测试转存完成").wait_for()
                page.wait_for_function("!app.runsLoading")
                extra_lines.append("追加日志")
                hold_detail.set()
                page.evaluate("void app.refreshRuns()")
                assert detail_started.wait(5)
                page.locator("#run-history").select_option(records[0]["id"])
                page.locator("#run-history").select_option(records[1]["id"])
                release_detail.set()
                page.get_by_label("运行日志内容").filter(has_text="追加日志").wait_for()
                assert "正在查询站内来源" in page.get_by_label("运行日志内容").inner_text()
                assert len(runs) == 2
                page.get_by_role("link", name="热门追更").click()
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
                page.wait_for_function("app.backgroundRun && !app.runRequestPending")
                assert len(runs) == 3
                retry = runs[2]["tasklist"][0]
                assert retry["resource_id"] == "bangumi:123"
                assert retry["addition"]["aria2"] == {
                    "auto_download": True, "download_subdir": True, "download_existing": True,
                    "save_path": "cartoon",
                }
                assert "ui-test-only-secret" not in json.dumps(runs)
                finish_run()
                page.wait_for_function("!app.backgroundRun")

                page.locator("a.nav-link").filter(has_text="任务列表").click()
                page.locator('[data-target="#collapse_0"]').click()
                page.locator('input[name="taskname[]"]').first.fill("测试搜索")
                page.get_by_text("PanSou: 搜索源暂时不可用", exact=True).wait_for()
                # A running subscription must not disable other calendar cards.
                page.reload()
                page.locator("a.nav-link").filter(has_text="运行日志").click()
                page.get_by_role("button", name="取消查找", exact=True).click()
                page.wait_for_function("!app.pendingSubscriptions.length")
                page.locator("a.nav-link").filter(has_text="热门追更").click()
                page.get_by_role("button", name="国漫", exact=True).click()
                page.get_by_role("button", name="周一", exact=True).click()
                page.get_by_role("button", name="一键订阅并转存").click()
                page.wait_for_function("app.backgroundRun && !app.runRequestPending")
                page.get_by_role("button", name="动漫", exact=True).click()
                page.get_by_role("button", name="周三", exact=True).click()
                page.get_by_role("button", name="加入订阅队列", exact=True).click()
                page.wait_for_function("app.queuedRuns.length === 1 && !app.runRequestPending")
                page.get_by_role("button", name="排队中", exact=True).wait_for()
                assert page.get_by_role("button", name="排队中", exact=True).is_disabled()
                page.locator("a.nav-link").filter(has_text="运行日志").click()
                page.get_by_role("heading", name="订阅队列（1）", exact=True).wait_for()
                page.reload()
                page.locator("a.nav-link").filter(has_text="运行日志").click()
                page.get_by_role("heading", name="订阅队列（1）", exact=True).wait_for()
                assert not errors, errors
                browser.close()
            print("UI smoke passed: login, donghua/source groups, seven days, subscribe/run, dedup, mobile, background log, reload/reconnect/history/errors, enable existing Aria2 download/save/refresh/run, contextual search errors; no JS errors")
        finally:
            if errors:
                print("Browser errors:", errors)
            server.shutdown()
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
