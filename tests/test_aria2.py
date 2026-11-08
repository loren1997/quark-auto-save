import contextlib
import copy
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock, patch

from treelib import Tree

import quark_auto_save as qas
from plugins.aria2 import Aria2


class Aria2Tests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        output = contextlib.redirect_stdout(self.output)
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.plugin = Aria2()
        self.plugin.is_active = True
        self.plugin.add_uri = Mock(return_value="accepted-gid")
        self.task = {"taskname": "测试剧", "shareurl": "https://pan.quark.cn/s/test",
                     "savepath": "/追更/测试剧", "addition": {"aria2": {
                         "auto_download": True, "download_subdir": True}}}
        self.account = Mock(cookie="PRIVATE_COOKIE", USER_AGENT="Test UA", nickname="test")
        self.account.savepath_fid = {"/追更/测试剧": "saved-folder"}
        self.account.download.side_effect = lambda fids: ({"code": 0, "data": [
            {"fid": fids[0], "download_url": f"https://download.invalid/{fids[0]}"}]}, "")
        self.account.ls_dir.return_value = {"code": 0, "data": {"list": [
            {"fid": "saved-01", "file_name": "01.mkv", "dir": False},
            {"fid": "saved-02", "file_name": "02.mkv", "dir": False}]}}
        self.tree = Tree()
        self.tree.create_node("root", "root", data={"is_dir": True})
        self.tree.create_node("01", "share-01", parent="root", data={
            "is_dir": False, "fid": "saved-01", "path": "/追更/测试剧/01.mkv"})

    def test_new_transfer_uses_saved_fid_and_download_options(self):
        self.plugin.run(self.task, account=self.account, tree=self.tree)
        self.account.download.assert_called_once_with(["saved-01"])
        options = self.plugin.add_uri.call_args.args[0][1]
        self.assertEqual(options["dir"], "/Downloads/追更/测试剧")
        self.assertEqual(options["out"], "01.mkv")
        self.assertEqual(options["pause"], "false")
        self.assertIn("Cookie: PRIVATE_COOKIE", options["header"])
        self.assertNotIn("aria2_pending", self.task)
        self.assertIn("GID: accepted-gid", self.output.getvalue())
        self.assertNotIn("PRIVATE_COOKIE", self.output.getvalue())

    def test_existing_files_without_new_transfer_retry_only_unaccepted_files(self):
        self.task["addition"]["aria2"]["download_existing"] = True
        self.plugin.add_uri.side_effect = ["accepted-01", None, "accepted-02"]
        self.plugin.run(self.task, account=self.account, tree=False)
        self.assertEqual(self.task["aria2_pending"], {"saved-02": "/追更/测试剧/02.mkv"})
        self.assertFalse(self.task["addition"]["aria2"]["download_existing"])
        self.plugin.run(self.task, account=self.account, tree=False)
        self.assertNotIn("aria2_pending", self.task)
        self.assertEqual(self.account.ls_dir.call_count, 1)
        self.assertEqual([call.args[0] for call in self.account.download.call_args_list],
                         [["saved-01"], ["saved-02"], ["saved-02"]])
        self.plugin.run(self.task, account=self.account, tree=False)
        self.assertEqual(self.plugin.add_uri.call_count, 3)

    def test_inactive_connection_keeps_new_transfer_for_next_run(self):
        self.plugin.is_active = False
        self.plugin.run(self.task, account=self.account, tree=self.tree)
        self.plugin.add_uri.assert_not_called()
        self.assertIn("saved-01", self.task["aria2_pending"])
        self.assertIn("RPC 未连接", self.output.getvalue())
        self.plugin.is_active = True
        self.plugin.run(self.task, account=self.account, tree=False)
        self.plugin.add_uri.assert_called_once()

    def test_failed_listing_preserves_backfill_and_does_not_submit_partial_scan(self):
        self.task["addition"]["aria2"]["download_existing"] = True
        self.account.ls_dir.side_effect = [
            {"code": 0, "data": {"list": [
                {"fid": "01", "file_name": "01.mkv", "dir": False},
                {"fid": "sub", "file_name": "子目录", "dir": True}]}},
            {"code": 500}]
        self.plugin.run(self.task, account=self.account, tree=False)
        self.assertTrue(self.task["addition"]["aria2"]["download_existing"])
        self.plugin.add_uri.assert_not_called()

    def test_partial_download_api_response_does_not_misname_or_drop_files(self):
        self.account.download.return_value = ({"code": 0, "data": []}, "")
        self.account.download.side_effect = None
        self.plugin.run(self.task, account=self.account, tree=self.tree)
        self.plugin.add_uri.assert_not_called()
        self.assertIn("saved-01", self.task["aria2_pending"])

    def test_subdirectory_failure_retries_only_that_directory(self):
        self.tree.create_node("sub", "share-sub", parent="root", data={
            "is_dir": True, "fid": "saved-sub", "path": "/追更/测试剧/新子目录"})
        self.account.ls_dir.side_effect = [{"code": 500}, {"code": 0, "data": {"list": [
            {"fid": "saved-02", "file_name": "02.mkv", "dir": False}]}}]
        self.plugin.run(self.task, account=self.account, tree=self.tree)
        self.assertEqual(self.plugin.add_uri.call_count, 1)
        self.assertFalse(self.task["addition"]["aria2"].get("download_existing", False))
        self.assertEqual(self.task["aria2_pending_dirs"], {"saved-sub": "/追更/测试剧/新子目录"})
        self.plugin.run(self.task, account=self.account, tree=False)
        self.assertEqual([call.args[0] for call in self.account.ls_dir.call_args_list], ["saved-sub", "saved-sub"])
        self.assertEqual([call.args[0] for call in self.account.download.call_args_list], [["saved-01"], ["saved-02"]])
        self.assertNotIn("aria2_pending_dirs", self.task)

    def test_disabled_task_logs_and_never_submits(self):
        self.task["addition"]["aria2"]["auto_download"] = False
        self.plugin.run(self.task, account=self.account, tree=self.tree)
        self.assertIn("未开启 auto_download", self.output.getvalue())
        self.plugin.add_uri.assert_not_called()

    def test_runtime_calls_backfill_without_new_transfer_and_persists_state(self):
        self.task["addition"]["aria2"]["download_existing"] = True
        original = copy.deepcopy(self.task)
        self.account.do_save_task.return_value = False
        other_plugin = Mock(is_active=True)
        del other_plugin.task_before
        del other_plugin.task_after
        with patch.object(qas, "CONFIG_DATA", {}), patch.object(qas.Config, "load_plugins", return_value=(
            {"aria2": self.plugin, "other": other_plugin}, {}, {"aria2": Aria2.default_task_config}
        )):
            qas.do_save(self.account, [self.task])
        self.assertEqual(self.plugin.add_uri.call_count, 2)
        other_plugin.run.assert_not_called()
        on_disk = {"tasklist": [copy.deepcopy(original)]}
        qas.Config.merge_task_state(on_disk, original, self.task)
        self.assertFalse(on_disk["tasklist"][0]["addition"]["aria2"]["download_existing"])

    def test_runtime_persists_pending_and_clears_it_after_later_success(self):
        original = copy.deepcopy(self.task)
        self.plugin.is_active = False
        self.account.do_save_task.return_value = self.tree
        with patch.object(qas, "CONFIG_DATA", {}), patch.object(qas.Config, "load_plugins", return_value=(
            {"aria2": self.plugin}, {}, {"aria2": Aria2.default_task_config}
        )):
            qas.do_save(self.account, [self.task])
        on_disk = {"tasklist": [copy.deepcopy(original)]}
        qas.Config.merge_task_state(on_disk, original, self.task)
        self.assertIn("saved-01", on_disk["tasklist"][0]["aria2_pending"])
        before_retry = copy.deepcopy(self.task)
        self.plugin.is_active = True
        self.plugin.run(self.task, account=self.account, tree=False)
        qas.Config.merge_task_state(on_disk, before_retry, self.task)
        self.assertNotIn("aria2_pending", on_disk["tasklist"][0])

    def test_real_http_jsonrpc_accepts_token_and_exposes_rpc_rejection(self):
        requests_seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests_seen.append(body)
                if body["method"] == "aria2.getVersion":
                    payload = {"result": {"version": "1.37.0"}}
                else:
                    payload = {"error": {"code": 1, "message": "rejected"}}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict("os.environ", {"NO_PROXY": "127.0.0.1"}):
                plugin = Aria2(host_port=f"127.0.0.1:{server.server_port}", secret="PRIVATE_RPC", dir="/Downloads")
                plugin.run(self.task, account=self.account, tree=self.tree)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertTrue(plugin.is_active)
        self.assertEqual([r["method"] for r in requests_seen], ["aria2.getVersion", "aria2.addUri"])
        self.assertEqual(requests_seen[1]["params"][0], "token:PRIVATE_RPC")
        self.assertIn("RPC 未接受", self.output.getvalue())
        self.assertNotIn("PRIVATE_RPC", self.output.getvalue())
        self.assertNotIn("PRIVATE_COOKIE", self.output.getvalue())
        self.assertIn("saved-01", self.task["aria2_pending"])


if __name__ == "__main__":
    unittest.main()
