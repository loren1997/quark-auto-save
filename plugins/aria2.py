import os
import re
import requests

from media_files import is_media_file, is_video_file, media_title
from run_progress import report_progress


class Aria2:

    default_config = {
        "host_port": "172.17.0.1:6800",  # Aria2 RPC地址，填入格式支持：host:port 或 http(s)://host:port
        "secret": "",  # Aria2 RPC 密钥
        "dir": "/downloads",  # 下载目录，需要Aria2有权限访问
    }
    default_task_config = {
        "auto_download": False,  # 是否自动添加下载任务
        "download_existing": False,  # 下次运行时补交保存目录内的文件，完成扫描后自动关闭
        "download_subdir": False,  # 是否递归下载子目录和文件
        "download_subtitles": False,  # 默认只下载视频；开启后同时下载字幕
        "save_path": "",  # 留空时使用任务保存目录，文件直接放入此目录，不复制源子目录结构
        "pause": False,  # 添加任务后为暂停状态，不自动开始（手动下载）
    }
    is_active = False
    rpc_url = None

    def __init__(self, **kwargs):
        self.plugin_name = self.__class__.__name__.lower()
        for key, value in self.default_config.items():
            setattr(self, key, kwargs.get(key, value))
        if kwargs:
            for key, _ in self.default_config.items():
                if key in kwargs:
                    setattr(self, key, kwargs[key])
                else:
                    print(f"{self.plugin_name} 模块缺少必要参数: {key}")
            if self.host_port and self.secret:
                self.rpc_url = self._get_rpc_url(self.host_port)
                if self.get_version():
                    self.is_active = True

    def _get_rpc_url(self, endpoint):
        """获取 RPC URL"""
        if "://" in endpoint:
            scheme, url_part = endpoint.split("://", 1)
            parts = url_part.split("/", 1)
            host_port = parts[0]
            path = parts[1] if len(parts) > 1 and parts[1] else "jsonrpc"
            return f"{scheme.lower()}://{host_port}/{path}"
        else:
            # 默认使用 HTTP
            return f"http://{self.host_port}/jsonrpc"

    def _recursive_dir(self, account, fid, parent_path, pending, recursive=True, download_subtitles=False):
        """递归遍历目录，获取所有文件和子目录"""
        print(f"Aria2下载: 递归目录 {parent_path}")
        response = account.ls_dir(fid)
        if response.get("code") != 0:
            raise RuntimeError("读取保存目录失败")
        for item in response["data"]["list"]:
            item_fid = item["fid"]
            item_name = item["file_name"]
            item_path = f"{parent_path}/{item_name}" if parent_path else item_name

            if item["dir"]:
                if recursive:
                    self._recursive_dir(account, item_fid, item_path, pending,
                                        download_subtitles=download_subtitles)
            elif is_video_file(item) or (download_subtitles and is_media_file(item)):
                pending[str(item_fid)] = item_path

    @staticmethod
    def _belongs_to_task(task, path):
        """共享目录只补交当前任务默认规则已生成的文件，不猜测原始标题。"""
        template = re.fullmatch(r"\{TASKNAME\}([ ._-]S\d{2,3}E)\{EXX\}\.\{EXT\}", task.get("replace", ""))
        title = media_title(task.get("taskname", ""))
        if not template or not title:
            return False
        stem = os.path.splitext(os.path.basename(path))[0]
        prefix = title + template[1]
        if not stem.startswith(prefix):
            return False
        episode, *suffixes = stem[len(prefix):].split(".")
        return bool(re.fullmatch(r"\d{2,3}", episode) and int(episode) > 0
                    and (not suffixes or (not is_video_file({"file_name": path})
                         and all(re.fullmatch(r"[\w-]+", suffix) for suffix in suffixes))))

    def run(self, task, **kwargs):
        task_config = task.get("addition", {}).get(self.plugin_name, self.default_task_config)
        allowed_file = is_media_file if task_config.get("download_subtitles") else is_video_file
        tree = kwargs.get("tree")
        rename_failed = {str(node.data["fid"]) for node in tree.all_nodes_itr()
                         if node.data.get("rename_failed") and node.data.get("fid")} if tree else set()
        rename_failed.update(str(fid) for fid in task.get("rename_pending", {}))
        shared_path = kwargs.get("shared_path") or task.get("savepath", "").strip("/") == "cartoon"
        # 先移除旧队列中的禁用文件和未成功重命名的文件。
        pending = task.get("aria2_pending", {})
        for fid, path in list(pending.items()):
            if fid in rename_failed or not allowed_file({"file_name": path, "dir": False}):
                pending.pop(fid)
        if not pending:
            task.pop("aria2_pending", None)
        if not task_config.get("auto_download"):
            if kwargs.get("tree"):
                print("Aria2下载: 此任务未开启 auto_download；仅转存到网盘")
            return
        account = kwargs.get("account")
        if not account:
            return
        pending = task.setdefault("aria2_pending", {})
        pending_dirs = task.setdefault("aria2_pending_dirs", {})
        try:
            if task_config.get("download_existing"):
                savepath = re.sub(r"/{2,}", "/", f"/{task['savepath']}")
                fid = account.savepath_fid.get(savepath)
                if not fid:
                    raise RuntimeError("保存目录尚未就绪")
                # 先完整扫描再入队，避免子目录失败后重复提交已扫描的文件。
                existing = {}
                self._recursive_dir(account, fid, savepath, existing,
                                    task_config.get("download_subdir", False),
                                    task_config.get("download_subtitles", False))
                if shared_path:
                    existing = {fid: path for fid, path in existing.items() if self._belongs_to_task(task, path)}
                pending.update(existing)
                task_config["download_existing"] = False
                print(f"Aria2下载: 已补入 {len(existing)} 个已转存文件")
            if tree:
                # 按文件路径排序添加下载任务
                for node in sorted(tree.all_nodes_itr(), key=lambda n: n.data.get("path", "")):
                    if not node.data.get("is_dir", True):
                        if (str(node.data["fid"]) not in rename_failed
                                and allowed_file({"file_name": node.data["path"], "dir": False})):
                            pending[str(node.data["fid"])] = node.data["path"]
                    elif not node.is_root() and task_config.get("download_subdir") and node.data.get("fid"):
                        pending_dirs[str(node.data["fid"])] = node.data["path"]
        except Exception as exc:
            print(f"Aria2下载: 扫描失败（{type(exc).__name__}），保留下次补交")
            return task
        for fid, path in list(pending_dirs.items()):
            try:
                files = {}
                self._recursive_dir(account, fid, path, files,
                                    download_subtitles=task_config.get("download_subtitles", False))
                if shared_path:
                    files = {file_id: file_path for file_id, file_path in files.items()
                             if self._belongs_to_task(task, file_path)}
                pending.update(files)
                pending_dirs.pop(fid)
            except Exception as exc:
                print(f"Aria2下载: 目录扫描失败 {path}（{type(exc).__name__}），下次重试此目录")
        if not pending_dirs:
            task.pop("aria2_pending_dirs", None)
        for fid in rename_failed:
            pending.pop(fid, None)
        if not pending:
            task.pop("aria2_pending", None)
            return task
        if not self.is_active:
            print(f"Aria2下载: RPC 未连接，保留 {len(pending)} 个文件下次重试；请检查地址、密钥和网络")
            return task
        for fid, file_path in sorted(list(pending.items()), key=lambda item: item[1]):
            report_progress("download", task=task.get("taskname", ""), source="Aria2", query=os.path.basename(file_path))
            try:
                # 逐文件取临时直链，避免接口返回乱序/部分失败导致文件名与内容错配。
                download_return, cookie = account.download([fid])
                items = download_return.get("data")
                if download_return.get("code") != 0 or not isinstance(items, list) or len(items) != 1:
                    print(f"Aria2下载: 获取直链失败 {file_path}；保留下次重试")
                    continue
                file_url = items[0].get("download_url")
                if not file_url or str(items[0].get("fid", fid)) != fid:
                    print(f"Aria2下载: 直链缺失或文件不匹配 {file_path}；保留下次重试")
                    continue
                save_path = (task_config.get("save_path") or task["savepath"]).strip("/")
                local_path = f"{self.dir.rstrip('/')}/{save_path}/{os.path.basename(file_path)}"
                aria2_params = [
                    [file_url],
                    {
                        "header": [
                            f"Cookie: {cookie or account.cookie}",
                            f"User-Agent: {account.USER_AGENT}",
                        ],
                        "out": os.path.basename(local_path),
                        "dir": os.path.dirname(local_path),
                        "pause": str(bool(task_config.get("pause", False))).lower(),
                    },
                ]
                if gid := self.add_uri(aria2_params):
                    pending.pop(fid)
                    print(f"📥 Aria2已提交: {file_path}（GID: {gid}）")
            except Exception as exc:
                print(f"Aria2下载: 提交失败 {file_path}（{type(exc).__name__}）；保留下次重试")
        if not pending:
            task.pop("aria2_pending", None)
        else:
            print(f"Aria2下载: 仍有 {len(pending)} 个文件未提交，下次运行自动重试")
        return task

    def _make_rpc_request(self, method, params=None):
        """发出 JSON-RPC 请求."""
        jsonrpc_data = {
            "jsonrpc": "2.0",
            "id": "quark-auto-save",
            "method": method,
            "params": params or [],
        }
        if self.secret:
            jsonrpc_data["params"].insert(0, f"token:{self.secret}")
        try:
            response = requests.post(
                self.rpc_url, json=jsonrpc_data, timeout=10, verify=False
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Aria2下载: RPC 请求失败（{type(e).__name__}）")
        return {}

    def get_version(self):
        """检查与 Aria2 的连接."""
        response = self._make_rpc_request("aria2.getVersion")
        if response.get("result"):
            print(f"Aria2下载: v{response['result']['version']}")
            return True
        else:
            error = response.get("error") or {}
            print(f"Aria2下载: 连接失败（错误码 {error.get('code', '未知')}），请检查 RPC 地址、密钥和网络")
            return False

    def add_uri(self, params=None):
        """添加 URI 下载任务."""
        response = self._make_rpc_request("aria2.addUri", params)
        if not response.get("result"):
            error = response.get("error") or {}
            print(f"Aria2下载: RPC 未接受下载任务（错误码 {error.get('code', '未知')}）；请检查 RPC 配置及下载目录权限")
        return response.get("result")
