import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
with contextlib.redirect_stdout(io.StringIO()):
    import run as web
from run_manager import RunManager, redact


class RunFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config_path = self.root / "config.json"
        self.config = {"webui": {"username": "test", "password": "private-password"},
                       "cookie": ["__puus=private-cookie; __pus=another-cookie"],
                       "plugins": {"aria2": {"secret": "private-rpc"}}, "tasklist": []}
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.script = self.root / "fixture.py"
        self.script.write_text("print('finished')\n", encoding="utf-8")
        self.managers = []
        self.addCleanup(self.stop_managers)

    def stop_managers(self):
        for manager in self.managers:
            manager.close()
        for manager in self.managers:
            until = time.monotonic() + 5
            while manager.processes and time.monotonic() < until:
                with manager.condition:
                    manager.condition.wait(0.05)

    def manager(self, timeout=5):
        manager = RunManager(str(self.config_path), sys.executable, str(self.script), timeout)
        self.managers.append(manager)
        return manager

    def finish(self, manager, run_id):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            result = manager.get(run_id)
            if result["finished_at"]:
                return result
            with manager.condition:
                manager.condition.wait(0.05)
        self.fail("background run did not finish")


class BackgroundRunTests(RunFixture):
    def test_real_subprocess_progress_incremental_output_and_secret_redaction(self):
        self.script.write_text(
            "import json, os, sys, time\n"
            "config=json.load(open(sys.argv[1]))\n"
            "print('Cookie: '+config['cookie'][0], flush=True)\n"
            "print(config['webui']['password']+' '+config['plugins']['aria2']['secret'], flush=True)\n"
            "print('https://dl-pc-zb.drive.quark.cn/path/private?token=unsafe', flush=True)\n"
            "print('https://user:pass@search.example/path?api_key=unsafe', flush=True)\n"
            "assert os.environ['QAS_RUN_PROGRESS']=='1'\n"
            "print('QAS_PROGRESS: '+json.dumps({'stage':'search','task':'测试作品','source':'Google','query':'测试作品 02'}), flush=True)\n"
            "print('QAS_PROGRESS: '+json.dumps({'stage':'made-up-status','status':'failed'}), flush=True)\n"
            "time.sleep(0.15)\n"
            "print('仍有中间缺集 [2]')\n", encoding="utf-8")
        manager = self.manager()
        started, run = manager.start({})
        self.assertTrue(started)
        result = self.finish(manager, run["id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["warning"])
        self.assertEqual((result["stage"], result["source"], result["query"]), ("search", "Google", "测试作品 02"))
        rendered = json.dumps(result, ensure_ascii=False)
        persisted = (manager.directory / (run["id"] + ".json")).read_text(encoding="utf-8")
        for value in ("private-password", "private-cookie", "another-cookie", "private-rpc", "token=unsafe", "api_key=unsafe", "user:pass", "QAS_PROGRESS"):
            self.assertNotIn(value, rendered + persisted)
        self.assertEqual(manager.get(run["id"], result["cursor"])["logs"], [])
        self.assertEqual(manager.get(run["id"], 1)["logs"][0]["cursor"], 2)
        self.assertIn("[下载链接已隐藏]", rendered)

    def test_manual_and_scheduled_runs_share_exclusion_lock(self):
        self.script.write_text("import time\nprint('started', flush=True)\ntime.sleep(0.3)\n", encoding="utf-8")
        manager = self.manager()
        started, run = manager.start({})
        self.assertTrue(started)
        second_started, second = manager.start({}, trigger="scheduled")
        self.assertFalse(second_started)
        self.assertEqual(second["id"], run["id"])
        other_manager = self.manager()
        self.assertFalse(other_manager.start({})[0])
        self.assertEqual(self.finish(manager, run["id"])["status"], "succeeded")
        self.assertEqual(other_manager.get(run["id"])["status"], "succeeded")
        started, scheduled = manager.start({}, trigger="scheduled")
        self.assertTrue(started)
        self.assertEqual(self.finish(manager, scheduled["id"])["trigger"], "scheduled")

    def test_preexisting_managers_discover_new_runs_created_by_each_other(self):
        self.script.write_text("import time\ntime.sleep(0.2)\nprint('done')\n", encoding="utf-8")
        first, second = self.manager(), self.manager()
        _, run = first.start({})
        self.assertEqual(second.list()[0]["id"], run["id"])
        started, active = second.start({})
        self.assertFalse(started)
        self.assertEqual(active["id"], run["id"])
        self.finish(first, run["id"])
        self.assertEqual(second.get(run["id"])["status"], "succeeded")
        _, next_run = second.start({}, trigger="scheduled")
        self.assertIsNotNone(first.get(next_run["id"]))
        self.assertEqual(self.finish(second, next_run["id"])["status"], "succeeded")

    def test_timeout_and_failure_are_persisted(self):
        for script, timeout, expected in (("import time\ntime.sleep(2)\n", 0.1, "运行超过"),
                                           ("raise SystemExit(3)\n", 5, "退出码 3")):
            with self.subTest(expected=expected):
                self.script.write_text(script, encoding="utf-8")
                manager = self.manager(timeout)
                _, run = manager.start({})
                result = self.finish(manager, run["id"])
                self.assertEqual(result["status"], "failed")
                self.assertIn(expected, result["error"])
                self.assertIsNotNone(result["finished_at"])

    def test_process_start_failure_releases_execution_lock(self):
        manager = self.manager()
        with patch("run_manager.subprocess.Popen", side_effect=OSError("private-password")):
            _, run = manager.start({})
            result = self.finish(manager, run["id"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("private-password", json.dumps(result))
        started, next_run = manager.start({})
        self.assertTrue(started)
        self.assertEqual(self.finish(manager, next_run["id"])["status"], "succeeded")

    def test_thread_start_failure_does_not_leave_persisted_queued_run(self):
        manager, other = self.manager(), self.manager()
        with patch("run_manager.threading.Thread.start", side_effect=RuntimeError("private-password")):
            with self.assertRaises(RuntimeError):
                manager.start({})
        failed = manager.list()[0]
        self.assertEqual(failed["status"], "failed")
        self.assertIsNotNone(failed["finished_at"])
        self.assertNotIn("private-password", json.dumps(failed))
        self.assertEqual(other.get(failed["id"])["status"], "failed")
        started, run = other.start({})
        self.assertTrue(started)
        self.assertEqual(self.finish(other, run["id"])["status"], "succeeded")

    @unittest.skipUnless(os.name == "nt", "Windows atomic replacement sharing semantics")
    def test_temporary_log_read_lock_does_not_fail_or_cancel_running_process(self):
        self.script.write_text("print('completed')\n", encoding="utf-8")
        manager = self.manager()
        replace, conflicts = os.replace, []
        def locked_once(source, destination):
            record = json.loads(Path(source).read_text(encoding="utf-8"))
            if record["status"] == "running" and not conflicts:
                conflicts.append(True)
                raise PermissionError("simulated concurrent log read")
            return replace(source, destination)
        with patch("run_manager.os.replace", side_effect=locked_once):
            _, run = manager.start({})
            result = self.finish(manager, run["id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(conflicts, [True])
        self.assertIn("completed", [line["text"] for line in result["logs"]])

    def test_restart_marks_active_record_interrupted_and_retains_latest_twenty(self):
        manager = self.manager()
        _, run = manager.start({})
        record = self.finish(manager, run["id"])
        for number in range(22):
            row = {**record, "id": f"{number:032x}", "created_at": f"2026-01-{number + 1:02d}", "logs": []}
            row["status"] = "running" if number == 21 else "succeeded"
            (manager.directory / (row["id"] + ".json")).write_text(json.dumps(row), encoding="utf-8")
        restarted = self.manager()
        self.assertEqual(len(restarted.list()), 20)
        self.assertEqual(restarted.get(f"{21:032x}")["status"], "interrupted")
        self.assertEqual(len(list(manager.directory.glob("*.json"))), 20)

    def test_log_window_has_stable_cursor_and_truncation_indicator(self):
        self.script.write_text("for number in range(10): print('line '+str(number))\n", encoding="utf-8")
        manager = self.manager()
        with patch("run_manager.MAX_LINES", 3):
            _, run = manager.start({})
            result = self.finish(manager, run["id"])
        self.assertEqual(len(result["logs"]), 3)
        self.assertEqual((result["first_cursor"], result["cursor"]), (8, 10))
        self.assertTrue(result["truncated"])
        self.assertFalse(manager.get(run["id"], 7)["truncated"])

    def test_redaction_hides_unconfigured_sensitive_assignments(self):
        for text in ("stoken=temporary-secret", '"authorization": "Bearer temp-secret"',
                     "password: runtime-secret", "Cookie: runtime-cookie", "__puus=runtime-cookie"):
            self.assertIn("[已隐藏]", redact(text))
            self.assertNotIn(text.split()[-1], redact(text))

    def test_oversized_output_line_is_discarded_without_unbounded_capture(self):
        self.script.write_text("print('x'*100000+'private-password')\nprint('end')\n", encoding="utf-8")
        manager = self.manager()
        _, run = manager.start({})
        result = self.finish(manager, run["id"])
        self.assertEqual([line["text"] for line in result["logs"]], ["[过长日志行已隐藏]", "end"])


class BackgroundRunApiTests(RunFixture):
    def setUp(self):
        super().setUp()
        for name, value in (("CONFIG_PATH", str(self.config_path)), ("SCRIPT_PATH", str(self.script)),
                            ("config_data", self.config), ("run_managers", {})):
            patcher = patch.object(web, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session["token"] = web.get_login_token()
        self.web_manager = web.get_run_manager()
        self.managers.append(self.web_manager)

    def test_routes_require_login_and_reject_unknown_parameters_and_paths(self):
        for payload in ([], {"command": "bad"}, {"config_path": "other"}, {"tasklist": "bad"},
                        {"tasklist": [1]}, {"quark_test": "true"}, {"discovery_id": "../file"},
                        {"discovery_id": "bangumi:1", "tasklist": []}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/api/runs", json=payload).status_code, 400)
        self.assertEqual(self.client.get("/api/runs/unknown").status_code, 404)
        self.assertEqual(self.client.get("/api/runs/" + "a" * 32 + "?after=-1").status_code, 400)
        with self.client.session_transaction() as session:
            session.clear()
        for route, method in (("/api/runs", "get"), ("/api/runs", "post"),
                              ("/api/runs/" + "a" * 32, "get"), ("/run_script_now", "post")):
            self.assertEqual(getattr(self.client, method)(route).status_code, 401)

    def test_api_returns_immediately_and_schedule_uses_same_history(self):
        self.script.write_text("import time\nprint('live', flush=True)\ntime.sleep(0.4)\nprint('end')\n", encoding="utf-8")
        response = self.client.post("/api/runs", json={"tasklist": [{"taskname": "测试作品"}]})
        self.assertEqual(response.status_code, 202)
        run_id = response.json["data"]["id"]
        self.assertEqual(self.client.post("/api/runs", json={}).status_code, 409)
        self.assertEqual(web.run_python([])["id"], run_id)
        self.assertEqual(self.client.get("/api/runs").json["data"][0]["id"], run_id)
        result = self.finish(self.web_manager, run_id)
        self.assertEqual(result["task"], "测试作品")
        self.assertEqual(self.client.get(f"/api/runs/{run_id}?after={result['cursor']}").json["data"]["logs"], [])
        scheduled = web.run_python([])
        self.assertEqual(self.finish(self.web_manager, scheduled["id"])["trigger"], "scheduled")

    def test_sse_client_disconnect_does_not_cancel_run(self):
        self.script.write_text("import time\nprint('first', flush=True)\ntime.sleep(0.2)\nprint('last')\n", encoding="utf-8")
        response = self.client.post("/run_script_now", json={}, buffered=False)
        run_id = response.headers["X-Run-ID"]
        self.assertIn(b"first", next(iter(response.response)))
        response.close()
        result = self.finish(self.web_manager, run_id)
        self.assertEqual(result["status"], "succeeded")
        self.assertIn("last", [line["text"] for line in result["logs"]])

    def test_discovery_request_is_queued_without_fetching_calendar_in_request(self):
        with patch.object(self.web_manager, "start", return_value=(True, {"id": "a" * 32, "discovery_id": "bangumi:42"})) as start, \
                patch.object(web, "get_calendar") as calendar:
            response = self.client.post("/api/runs", json={"discovery_id": "bangumi:42"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["data"]["discovery_id"], "bangumi:42")
        calendar.assert_not_called()
        start.assert_called_once_with({"discovery_id": "bangumi:42"}, trigger="discovery")

    def test_actual_discovery_subprocess_reports_missing_cookie_without_network(self):
        self.config["cookie"] = []
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        response = self.client.post("/api/runs", json={"discovery_id": "bangumi:42"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["data"]["trigger"], "discovery")
        result = self.finish(self.web_manager, response.json["data"]["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 1)
        self.assertTrue(any("Cookie" in line["text"] for line in result["logs"]))


class DiscoveryWorkerTests(RunFixture):
    def setUp(self):
        super().setUp()
        from scripts import subscribe_and_run
        self.worker = subscribe_and_run
        self.calendar = {"days": [{"anime": [{"id": "bangumi:42", "title": "测试作品", "search_title": "测试作品", "year": 2026}]}]}
        for owner, name, value in ((web, "CONFIG_PATH", str(self.config_path)), (web, "config_data", self.config),
                                   (web, "task_plugins_config_default", {})):
            patcher = patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, result, missing_cookie=False):
        if missing_cookie:
            self.config["cookie"] = []
            self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        output = io.StringIO()
        with patch.object(self.worker.sys, "argv", ["subscribe_and_run.py", str(self.config_path)]), \
                patch.dict(os.environ, {"QAS_RUN_PAYLOAD": json.dumps({"discovery_id": "bangumi:42"}), "QAS_RUN_PROGRESS": "0"}), \
                patch.object(web, "get_calendar", return_value=self.calendar), \
                patch.object(web, "get_search_aliases", return_value=[]), \
                patch.object(web.Config, "load_plugins", return_value=({}, {}, {})), \
                patch.object(web.Quark, "find_valid_resource", return_value=result) as find, \
                patch.object(self.worker.qas, "main") as run, contextlib.redirect_stdout(output):
            code = self.worker.main()
            tasks = json.loads(os.environ.get("TASKLIST", "[]"))
        return code, tasks, run, find, output.getvalue()

    def test_worker_creates_one_subscription_then_runs_it_without_schedule_filter(self):
        result = {"success": True, "candidate": {"shareurl": "https://pan.quark.cn/s/example"}}
        code, tasks, run, find, _ = self.execute(result)
        self.assertEqual(code, 0)
        run.assert_called_once()
        find.assert_called_once()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["resource_id"], "bangumi:42")
        self.assertNotIn("runweek", tasks[0])
        stored = json.loads(self.config_path.read_text(encoding="utf-8"))["tasklist"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["runweek"], list(range(1, 8)))
        code, tasks, run, find, _ = self.execute(result)
        self.assertEqual(code, 0)
        run.assert_called_once()
        find.assert_not_called()
        self.assertEqual(len(json.loads(self.config_path.read_text(encoding="utf-8"))["tasklist"]), 1)

    def test_search_failure_and_missing_cookie_are_nonzero_without_starting_transfer(self):
        failure = {"success": False, "error": "查询暂未完成", "source_errors": [{"source": "Google", "message": "需要人工验证"}],
                   "queries": ["测试作品 02"]}
        code, tasks, run, _, output = self.execute(failure)
        self.assertEqual(code, 1)
        self.assertEqual(tasks, [])
        run.assert_not_called()
        self.assertIn("Google", output)
        self.assertIn("需要人工验证", output)
        code, _, run, find, output = self.execute(failure, missing_cookie=True)
        self.assertEqual(code, 1)
        run.assert_not_called()
        find.assert_not_called()
        self.assertIn("Cookie", output)


if __name__ == "__main__":
    unittest.main()
