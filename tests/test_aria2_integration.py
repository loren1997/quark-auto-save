import contextlib
import functools
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import HTTPServer, SimpleHTTPRequestHandler
from unittest.mock import Mock, patch

from plugins.aria2 import Aria2


@unittest.skipUnless(shutil.which("aria2c"), "aria2c is not installed; CI installs it")
class Aria2IntegrationTests(unittest.TestCase):
    def test_existing_file_downloads_through_real_rpc_and_is_not_submitted_twice(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}))
        sample = b"quark-auto-save real aria2 integration\n" * 128
        (root / "sample.mp4").write_bytes(sample)

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        log_path = root / "aria2.log"
        log = self.enterContext(log_path.open("wb"))
        process = subprocess.Popen([
            shutil.which("aria2c"), "--no-conf", "--enable-rpc=true", "--rpc-listen-all=false",
            f"--rpc-listen-port={port}", "--rpc-secret=integration-only", "--disable-ipv6=true",
            "--enable-dht=false", "--enable-dht6=false", "--enable-peer-exchange=false",
            "--file-allocation=none", "--max-concurrent-downloads=1", "--summary-interval=0",
            f"--dir={root / 'downloads'}",
        ], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def stop():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            self.fail("aria2 RPC did not start: " + log_path.read_text(errors="replace"))
        plugin = Aria2(host_port=f"127.0.0.1:{port}", secret="integration-only", dir=str(root / "downloads"))
        self.assertTrue(plugin.is_active, output.getvalue() + log_path.read_text(errors="replace"))
        account = Mock(cookie="integration-only", USER_AGENT="quark-auto-save-test")
        account.savepath_fid = {"/saved/series": "folder"}
        account.ls_dir.return_value = {"code": 0, "data": {"list": [
            {"fid": "sample", "file_name": "sample.mp4", "dir": False},
            {"fid": "subtitle", "file_name": "sample.srt", "dir": False}]}}
        account.download.return_value = ({"code": 0, "data": [{"fid": "sample",
            "download_url": f"http://127.0.0.1:{server.server_port}/sample.mp4"}]}, "")
        task = {"savepath": "/saved/series", "addition": {"aria2": {
            "auto_download": True, "download_existing": True, "download_subdir": False, "save_path": "cartoon"}}}
        plugin.run(task, account=account, tree=False)
        deadline, stopped = time.monotonic() + 15, []
        while time.monotonic() < deadline:
            stopped = plugin._make_rpc_request("aria2.tellStopped", [0, 10]).get("result", [])
            if stopped:
                break
            time.sleep(0.05)
        self.assertEqual([item["status"] for item in stopped], ["complete"],
                         output.getvalue() + log_path.read_text(errors="replace"))
        destination = root / "downloads" / "cartoon" / "sample.mp4"
        self.assertEqual(destination.read_bytes(), sample)
        plugin.run(task, account=account, tree=False)
        account.download.assert_called_once_with(["sample"])
        account.ls_dir.assert_called_once_with("folder")
        self.assertFalse(task["addition"]["aria2"]["download_existing"])
        status = plugin._make_rpc_request("aria2.getGlobalStat")
        self.assertIn("result", status, output.getvalue() + log_path.read_text(errors="replace"))
        stats = status["result"]
        self.assertEqual((stats["numActive"], stats["numWaiting"], stats["numStoppedTotal"]), ("0", "0", "1"))
        self.assertEqual([path.name for path in destination.parent.iterdir()], ["sample.mp4"])
