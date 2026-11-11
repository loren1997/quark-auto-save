# !/usr/bin/env python3
# -*- coding: utf-8 -*-
from flask import (
    json,
    Flask,
    url_for,
    session,
    jsonify,
    request,
    redirect,
    Response,
    render_template,
    send_from_directory,
    stream_with_context,
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sdk.discovery import get_calendar, get_search_aliases
from sdk.common import normalize_share_url
from copy import deepcopy
from datetime import datetime, timedelta
import requests
import hashlib
import logging
import base64
import sys
import os
import re
import threading
import uuid

from run_manager import RunManager, validate_payload

parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, parent_dir)
from quark_auto_save import Quark, Config, MagicRename, resource_titles_match, apply_media_rename_defaults
from media_files import MEDIA_PATTERN, is_media_file, is_video_file
from resource_search import search_resources
from app.sdk.source_monitor import maintain_sources
from app.sdk.channel_registry import ChannelRegistry
from app.sdk.source_health import SourceHealth

print(
    r"""
   ____    ___   _____
  / __ \  /   | / ___/
 / / / / / /| | \__ \
/ /_/ / / ___ |___/ /
\___\_\/_/  |_/____/

-- Quark-Auto-Save --
 """
)
sys.stdout.flush()


def get_app_ver():
    """获取应用版本"""
    try:
        with open("build.json", "r") as f:
            build_info = json.loads(f.read())
            BUILD_SHA = build_info["BUILD_SHA"]
            BUILD_TAG = build_info["BUILD_TAG"]
    except Exception as e:
        BUILD_SHA = os.getenv("BUILD_SHA", "")
        BUILD_TAG = os.getenv("BUILD_TAG", "")
    if BUILD_TAG[:1] == "v":
        return BUILD_TAG
    elif BUILD_SHA:
        return f"{BUILD_TAG}({BUILD_SHA[:7]})"
    else:
        return "dev"


# 文件路径
PYTHON_PATH = sys.executable
SCRIPT_PATH = os.environ.get("SCRIPT_PATH", "./quark_auto_save.py")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "./config/quark_config.json")
PLUGIN_FLAGS = os.environ.get("PLUGIN_FLAGS", "")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = os.environ.get("PORT", 5005)
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", 1800))

config_data = {}
task_plugins_config_default = {}


def config_revision(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

app = Flask(__name__)
app.config["APP_VERSION"] = get_app_ver()
app.secret_key = "ca943f6db6dd34823d36ab08d8d6f65d"
app.config["SESSION_COOKIE_NAME"] = "QUARK_AUTO_SAVE_SESSION"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=31)
app.json.ensure_ascii = False
app.json.sort_keys = False
app.jinja_env.variable_start_string = "[["
app.jinja_env.variable_end_string = "]]"

scheduler = BackgroundScheduler()
SOURCE_MONITOR_JOB = "source-monitor"
run_managers = {}
run_manager_lock = threading.Lock()
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%m-%d %H:%M:%S",
)
# 过滤werkzeug日志输出
if not DEBUG:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("apscheduler").setLevel(logging.ERROR)
    sys.modules["flask.cli"].show_server_banner = lambda *x: None


def gen_md5(string):
    md5 = hashlib.md5()
    md5.update(string.encode("utf-8"))
    return md5.hexdigest()


def get_login_token():
    username = config_data["webui"]["username"]
    password = config_data["webui"]["password"]
    return gen_md5(f"token{username}{password}+-*/")[8:24]


def is_login():
    login_token = get_login_token()
    if session.get("token") == login_token or request.args.get("token") == login_token:
        return True
    else:
        return False


def source_option_enabled(config, source, option="enable"):
    return str(((config.get("source") or {}).get(source) or {}).get(option, True)).lower() == "true"


@app.route("/source_status")
def source_status():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"}), 401
    try:
        current = Config.read_json(CONFIG_PATH)
        health_path = Config.search_health_path(CONFIG_PATH)
        registry = ChannelRegistry(health_path).status()
        # 只返回运行状态；配置中的 Cookie、访问令牌和服务器认证信息不进入响应。
        channel_fields = ("name", "status", "reason", "last_checked", "next_check")
        source_fields = ("host", "reason", "next_check", "status")
        data = {
            "monitor_enabled": source_option_enabled(current, "monitor"),
            "auto_discover_enabled": source_option_enabled(current, "channelsearch", "auto_discover"),
            "channel_search_enabled": source_option_enabled(current, "channelsearch"),
            "registry": {key: registry.get(key) for key in ("last_discovery", "next_discovery", "discovery_reason")},
            "sources": [{key: row.get(key) for key in source_fields} for row in SourceHealth(health_path).status()],
        }
        data["registry"]["channels"] = [{key: row.get(key) for key in channel_fields}
                                         for row in registry.get("channels", [])]
        return jsonify({"success": True, "data": data})
    except (OSError, ValueError, TypeError):
        logging.warning("无法读取渠道巡检状态")
        return jsonify({"success": False, "message": "暂时无法读取渠道状态，请稍后刷新"}), 503


# 设置icon
@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
    )


# 登录页面
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = config_data["webui"]["username"]
        password = config_data["webui"]["password"]
        # 验证用户名和密码
        if (username == request.form.get("username")) and (
            password == request.form.get("password")
        ):
            logging.info(f">>> 用户 {username} 登录成功")
            session.permanent = True
            session["token"] = get_login_token()
            return redirect(url_for("index"))
        else:
            logging.info(f">>> 用户 {username} 登录失败")
            return render_template("login.html", message="登录失败")

    if is_login():
        return redirect(url_for("index"))
    return render_template("login.html", error=None)


# 退出登录
@app.route("/logout")
def logout():
    session.pop("token", None)
    return redirect(url_for("login"))


# 管理页面
@app.route("/")
def index():
    if not is_login():
        return redirect(url_for("login"))
    return render_template(
        "index.html", version=app.config["APP_VERSION"], plugin_flags=PLUGIN_FLAGS
    )


# 获取配置数据
@app.route("/data")
def get_data():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    data = Config.read_json(CONFIG_PATH)
    revision = config_revision(data)
    del data["webui"]
    data["config_revision"] = revision
    data["api_token"] = get_login_token()
    data["task_plugins_config_default"] = task_plugins_config_default
    return jsonify({"success": True, "data": data})


# 更新数据
@app.route("/update", methods=["POST"])
def update():
    global config_data
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    # 使用允许列表防止批量赋值攻击
    allowed_keys = ["cookie", "crontab", "push_config", "tasklist",
                    "magic_regex", "plugins", "source", "auto_repair", "discovery"]
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(success=False, message="配置必须是 JSON 对象"), 400
    if "auto_repair" in payload and not isinstance(payload["auto_repair"], bool):
        return jsonify(success=False, message="自动换源必须是开关值"), 400
    if "discovery" in payload and (not isinstance(payload["discovery"], dict)
            or not valid_discovery_path(payload["discovery"].get("savepath"))
            or not isinstance(payload["discovery"].get("auto_download", False), bool)):
        return jsonify(success=False, message="热门订阅保存路径必须是以 / 开头的夸克网盘目录"), 400
    if "source" in payload and (not isinstance(payload["source"], dict)
            or any(not isinstance(value, dict) for value in payload["source"].values())):
        return jsonify(success=False, message="搜索配置格式错误"), 400
    if "plugins" in payload and (not isinstance(payload["plugins"], dict)
            or any(not isinstance(value, dict) for value in payload["plugins"].values())):
        return jsonify(success=False, message="插件配置格式错误"), 400
    try:
        if "crontab" in payload:
            CronTrigger.from_crontab(payload["crontab"])
    except (TypeError, ValueError):
        return jsonify(success=False, message="定时规则格式错误"), 400
    with Config.locked(CONFIG_PATH):
        current = Config.read_json(CONFIG_PATH)
        if payload.get("config_revision") and payload["config_revision"] != config_revision(current):
            return jsonify(success=False, message="配置已被任务或其他页面更新，请刷新后重新编辑，避免覆盖自动换源结果"), 409
        updated = deepcopy(current)
        if "tasklist" in payload:
            Config.remember_resources(Config.resource_history_path(CONFIG_PATH), current.get("tasklist", []))
        for key, value in payload.items():
            if key in allowed_keys:
                updated[key] = value
        if "discovery" in payload:
            auto_download = updated["discovery"].get("auto_download", False)
            if auto_download and not aria2_download_configured(updated):
                return jsonify(success=False, message="请先启用并配置 Aria2 插件的地址、密钥和下载目录"), 400
            for task in updated.get("tasklist", []) + [item["task"] for item in updated.get("pending_subscriptions", [])]:
                if not task.get("resource_id"):
                    continue
                addition = task.setdefault("addition", {})
                aria2 = addition.setdefault("aria2", {})
                was_enabled = aria2.get("auto_download", False)
                aria2["auto_download"] = auto_download
                if auto_download:
                    aria2["download_subdir"] = True
                    if not was_enabled:
                        aria2["download_existing"] = True
                else:
                    aria2.pop("download_existing", None)
        config_data = updated
        Config.write_json(CONFIG_PATH, config_data)
    # 重新加载任务
    if reload_tasks():
        logging.info(f">>> 配置更新成功")
        return jsonify({"success": True, "message": "配置更新成功", "config_revision": config_revision(config_data)})
    else:
        logging.info(f">>> 配置更新失败")
        return jsonify({"success": False, "message": "配置更新失败"})


def get_run_manager():
    key = (os.path.abspath(CONFIG_PATH), PYTHON_PATH, os.path.abspath(SCRIPT_PATH), TASK_TIMEOUT)
    with run_manager_lock:
        if key not in run_managers:
            run_managers[key] = RunManager(*key)
        return run_managers[key]


def start_requested_run():
    try:
        if request.content_length and request.content_length > 256 * 1024:
            return None, (jsonify(success=False, message="运行参数过大"), 413)
        payload = validate_payload(request.get_json(silent=True))
        if payload.get("shareurl"):
            normalized = normalize_share_url(payload["shareurl"])
            if not normalized:
                raise ValueError("请提供有效的夸克分享链接")
            payload["shareurl"] = normalized
        started, record = get_run_manager().start(payload, trigger="discovery" if "discovery_id" in payload else "manual")
        if not started:
            return None, (jsonify(success=False, data=record, message="已有任务运行中，请在运行日志查看进度"), 409)
        return record, None
    except ValueError as exc:
        return None, (jsonify(success=False, message=str(exc)), 400)
    except OSError:
        return None, (jsonify(success=False, message="无法启动后台任务，请检查配置目录权限"), 500)


@app.route("/api/runs", methods=["GET", "POST"])
def runs():
    if not is_login():
        return jsonify(success=False, message="未登录"), 401
    if request.method == "GET":
        return jsonify(success=True, data=get_run_manager().list())
    record, error = start_requested_run()
    return error if error else (jsonify(success=True, data=record), 202)


@app.route("/api/runs/<run_id>")
def run_detail(run_id):
    if not is_login():
        return jsonify(success=False, message="未登录"), 401
    after = request.args.get("after", "0")
    if not re.fullmatch(r"\d{1,12}", after):
        return jsonify(success=False, message="日志游标必须是非负整数"), 400
    record = get_run_manager().get(run_id, int(after))
    if record is None:
        return jsonify(success=False, message="运行记录不存在"), 404
    return jsonify(success=True, data=record)


@app.route("/run_script_now", methods=["POST"])
def run_script_now():
    if not is_login():
        return jsonify(success=False, message="未登录"), 401
    record, error = start_requested_run()
    if error:
        return error
    return Response(stream_with_context(get_run_manager().stream(record["id"])),
                    content_type="text/event-stream;charset=utf-8",
                    headers={"X-Run-ID": record["id"], "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/task_suggestions")
def get_task_suggestions():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    from urllib.parse import urlsplit

    query = request.args.get("q", "")
    result = search_resources(
        query, Config.read_json(CONFIG_PATH),
        refresh=request.args.get("d") == "1",
        health_path=Config.search_health_path(CONFIG_PATH),
    )
    history = []
    if query.strip() and len(query) <= 200:
        for item in Config.resource_history(Config.resource_history_path(CONFIG_PATH)):
            title = item.get("taskname", "")
            link = normalize_share_url(item.get("shareurl", ""))
            if link and resource_titles_match(query, title, allow_missing_year=True):
                history.append({"taskname": title, "shareurl": link, "source": "历史资源（待验证）",
                                "content": "本机保存的分享线索，使用前仍需验证有效性", "datetime": ""})
    if not history:
        return jsonify(result)
    unique = {}
    for item in result.get("data", []) + history:
        parts = urlsplit(item["shareurl"])
        key = (parts.path.rstrip("/"), parts.fragment)
        previous = unique.get(key)
        if previous is None or (not urlsplit(previous["shareurl"]).query and parts.query):
            unique[key] = item
    return jsonify({**result, "success": True, "data": list(unique.values())})


@app.route("/api/discovery")
def discovery():
    if not is_login():
        return jsonify(success=False, message="未登录"), 401
    return jsonify(success=True, data=get_calendar(background=True, refresh=request.args.get("refresh") == "1"))


@app.route("/api/discovery/subscribe", methods=["POST"])
def subscribe_discovery():
    if not is_login():
        return jsonify(success=False, message="未登录"), 401
    return subscribe_discovery_payload(request.get_json(silent=True))


@app.route("/api/discovery/pending")
def pending_discovery():
    if not is_login():
        return jsonify(success=False, message="未登录"), 401
    current = Config.read_json(CONFIG_PATH)
    return jsonify(success=True, data=[Config.pending_summary(item)
                                      for item in current.get("pending_subscriptions", [])])


@app.route("/api/discovery/pending/<path:resource_id>", methods=["DELETE"])
def cancel_pending_discovery(resource_id):
    if not is_login():
        return jsonify(success=False, message="未登录"), 401
    if not re.fullmatch(r"[\w:-]{1,120}", resource_id):
        return jsonify(success=False, message="资源 ID 不正确"), 400
    with Config.locked(CONFIG_PATH):
        current = Config.read_json(CONFIG_PATH)
        before = current.get("pending_subscriptions", [])
        after = [item for item in before if item["task"]["resource_id"] != resource_id]
        if len(after) != len(before):
            current["pending_subscriptions"] = after
            Config.write_json(CONFIG_PATH, current)
    return jsonify(success=True, removed=len(after) != len(before))


def subscribe_discovery_payload(payload):
    global config_data
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
        return jsonify(success=False, message="缺少资源 ID"), 400
    shareurl = None
    if "shareurl" in payload:
        value = payload["shareurl"]
        shareurl = normalize_share_url(value) if isinstance(value, str) and len(value) <= 2000 else ""
        if not shareurl:
            return jsonify(success=False, message="请粘贴有效的夸克分享链接，可包含提取码"), 400
    current = Config.read_json(CONFIG_PATH)
    pending = next((item for item in current.get("pending_subscriptions", [])
                    if item["task"]["resource_id"] == payload["id"]), None)
    if pending and shareurl is None:
        return jsonify(success=False, pending=True, existing=True, data=Config.pending_summary(pending),
                       message="已保留自动查找订阅，将在现有定时规则执行时继续查找资源"), 202
    entry = next((dict(item, resource_type="anime" if kind == "donghua" else kind)
                  for day in get_calendar(resource_id=payload["id"])["days"]
                  for kind in ("donghua", "anime", "tv") for item in day.get(kind, [])
                  if item["id"] == payload["id"]), None)
    if entry is None:
        return jsonify(success=False, message="该资源不在本周列表中，请刷新后重试"), 404
    title = entry["search_title"]
    if re.fullmatch(r"\d{4}", str(entry.get("year", ""))):
        title += f" ({entry['year']})"
    from run_progress import report_progress
    report_progress("task", task=title)
    existing = existing_subscription(current.get("tasklist", []), entry["id"], title)
    if existing:
        return jsonify(success=True, existing=True, data=existing, message="已经订阅")
    settings = current.get("discovery", {})
    base = settings.get("savepath", "/cartoon") if isinstance(settings, dict) else None
    if not valid_discovery_path(base):
        return jsonify(success=False, message="热门订阅保存路径必须是以 / 开头的夸克网盘目录"), 400
    base = base.rstrip("/") or "/"
    auto_download = settings.get("auto_download", False)
    if auto_download and not aria2_download_configured(current):
        return jsonify(success=False, message="请先启用并配置 Aria2 插件的地址、密钥和下载目录"), 400
    cookies = current.get("cookie") or []
    cookie = cookies[0] if isinstance(cookies, list) and cookies else cookies
    if not isinstance(cookie, str) or not cookie.strip() or cookie.startswith("Your "):
        return jsonify(success=False, message="请先在系统配置中保存有效的夸克 Cookie"), 400
    task = {"taskname": title, "resource_type": entry["resource_type"]}
    aliases = get_search_aliases(entry)
    if aliases:
        task["search_aliases"] = aliases
    task.update({
        "savepath": base, "pattern": MEDIA_PATTERN, "replace": "", "update_subdir": ".*",
        "resource_id": entry["id"], "runweek": list(range(1, 8)),
        "addition": deepcopy(task_plugins_config_default),
    })
    apply_media_rename_defaults(task)
    task["addition"].setdefault("aria2", {})["save_path"] = base.strip("/")
    if auto_download:
        task["addition"].setdefault("aria2", {}).update(auto_download=True, download_subdir=True)
    account = Quark(cookie)
    account.resource_history_path = Config.resource_history_path(CONFIG_PATH)
    account.search_health_path = Config.search_health_path(CONFIG_PATH)
    result = (account.verify_resource_link(task, shareurl) if shareurl is not None
              else account.find_valid_resource(task, current))
    if not result.get("success") or not result.get("candidate"):
        if shareurl is None and not result.get("success"):
            with Config.locked(CONFIG_PATH):
                config_data = Config.read_json(CONFIG_PATH)
                existing = existing_subscription(config_data.get("tasklist", []), entry["id"], title)
                if existing:
                    return jsonify(success=True, existing=True, data=existing, message="已经订阅")
                records = config_data.setdefault("pending_subscriptions", [])
                pending = next((item for item in records if item["task"]["resource_id"] == entry["id"]), None)
                if pending is None:
                    timestamp = datetime.now().isoformat(timespec="seconds")
                    pending = {"id": uuid.uuid4().hex, "task": task, "created_at": timestamp,
                               "last_attempt_at": timestamp, "last_error": "尚未找到经视频文件验证的匹配资源"}
                    records.append(pending)
                    Config.write_json(CONFIG_PATH, config_data)
            return jsonify(success=False, pending=True, data=Config.pending_summary(pending),
                           code=result.get("code", "no_match"), queries=result.get("queries", []),
                           source_errors=result.get("source_errors", []),
                           search_complete=result.get("search_complete", False),
                           message=result.get("error") or "尚未找到经视频文件验证的匹配资源"), 202
        return jsonify(success=False, code=result.get("code", "no_match"),
                       queries=result.get("queries", []), source_errors=result.get("source_errors", []),
                       search_complete=result.get("search_complete", False),
                       message=result.get("error") or "未找到可验证的匹配资源，请在任务列表手动搜索"), 422
    task["shareurl"] = result["candidate"]["shareurl"]
    if result["candidate"].get("search_aliases"):
        task["search_aliases"] = result["candidate"]["search_aliases"]
    with Config.locked(CONFIG_PATH):
        config_data = Config.read_json(CONFIG_PATH)
        if pending and not any(item["id"] == pending["id"] for item in config_data.get("pending_subscriptions", [])):
            return jsonify(success=False, message="待查找订阅已取消，本次结果不会重新创建任务"), 409
        existing = existing_subscription(config_data.get("tasklist", []), entry["id"], title)
        if existing:
            return jsonify(success=True, existing=True, data=existing, message="已经订阅")
        config_data.setdefault("tasklist", []).append(task)
        if "pending_subscriptions" in config_data:
            config_data["pending_subscriptions"] = [item for item in config_data["pending_subscriptions"]
                                                     if item["task"]["resource_id"] != entry["id"]]
        Config.write_json(CONFIG_PATH, config_data)
        Config.remember_resources(account.resource_history_path, [task])
    return jsonify(success=True, existing=False, data=task, message="订阅已保存，正在开始转存")


def aria2_download_configured(config):
    plugins = config.get("plugins") or {}
    aria2 = plugins.get("aria2") if isinstance(plugins, dict) else None
    return ("-aria2" not in PLUGIN_FLAGS.split(",") and isinstance(aria2, dict)
            and all(aria2.get(key) for key in ("host_port", "secret", "dir")))


def valid_discovery_path(path):
    return (isinstance(path, str) and path.startswith("/") and len(path) <= 500
            and not re.search(r'[\\\x00-\x1f]', path)
            and not any(part in (".", "..") for part in path.split("/")))


def existing_subscription(tasks, resource_id, title):
    return Config.existing_subscription(tasks, resource_id, title)

@app.route("/get_share_detail", methods=["POST"])
def get_share_detail():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    shareurl = request.json.get("shareurl", "")
    stoken = request.json.get("stoken", "")
    account = Quark()
    pwd_id, passcode, pdir_fid, paths = account.extract_url(shareurl)
    if not stoken:
        get_stoken = account.get_stoken(pwd_id, passcode)
        if get_stoken.get("status") == 200:
            stoken = get_stoken["data"]["stoken"]
        else:
            return jsonify(
                {"success": False, "data": {"error": get_stoken.get("message")}}
            )
    share_detail = account.get_detail(
        pwd_id, stoken, pdir_fid, _fetch_share=1, fetch_share_full_path=1
    )

    if share_detail.get("code") != 0:
        return jsonify(
            {"success": False, "data": {"error": share_detail.get("message")}}
        )

    data = share_detail["data"]
    data["paths"] = [
        {"fid": i["fid"], "name": i["file_name"]}
        for i in share_detail["data"].get("full_path", [])
    ] or paths
    data["stoken"] = stoken

    # 过滤 01x.mp4 类型无效视频格式
    if os.getenv("FILTER_INVALID_VIDEO", "true") == "true":
        for share_file in data["list"]:
            if (
                share_file["file_name"].lower().endswith((".mp4", ".mkv"))
                and not share_file["dir"]
                and share_file["obj_category"] != "video"
            ):
                return jsonify({"success": False, "data": {"error": "无效视频格式"}})

    # 正则处理预览
    def preview_regex(data):
        task = request.json.get("task", {})
        magic_regex = request.json.get("magic_regex", {})
        mr = MagicRename(magic_regex)
        mr.set_taskname(task.get("taskname", ""))
        account = Quark(config_data["cookie"][0])
        get_fids = account.get_fids([task.get("savepath", "")])
        if get_fids:
            dir_file_list = account.ls_dir(get_fids[0]["fid"])["data"]["list"]
            dir_filename_list = [dir_file["file_name"] for dir_file in dir_file_list]
        else:
            dir_file_list = []
            dir_filename_list = []
        dir_video_filename_list = [item["file_name"] for item in dir_file_list if is_video_file(item)]

        pattern, replace = mr.magic_regex_conv(
            task.get("pattern", ""), task.get("replace", "")
        )
        for share_file in data["list"]:
            if not share_file["dir"] and not is_media_file(share_file):
                continue
            is_subtitle = not share_file["dir"] and not is_video_file(share_file)
            search_pattern = (
                task["update_subdir"]
                if share_file["dir"] and task.get("update_subdir")
                else pattern
            )
            matched = re.search(search_pattern, share_file["file_name"])
            if matched or is_subtitle:
                # 文件名重命名，目录不重命名
                file_name_re = (
                    share_file["file_name"]
                    if share_file["dir"] or not matched
                    else mr.media_name(pattern, replace, share_file["file_name"])
                )
                ignore_extension = bool(task.get("ignore_extension") and is_video_file(share_file))
                if file_name_saved := mr.is_exists(
                    file_name_re,
                    dir_video_filename_list if ignore_extension else dir_filename_list,
                    ignore_extension,
                ):
                    share_file["file_name_saved"] = file_name_saved
                else:
                    share_file["file_name_re"] = file_name_re

        # 文件列表排序
        if re.search(r"\{I+\}", replace):
            mr.set_dir_file_list(dir_file_list, replace)
            mr.sort_file_list(data["list"])

    if request.json.get("task"):
        preview_regex(data)

    return jsonify({"success": True, "data": data})


@app.route("/get_savepath_detail")
def get_savepath_detail():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    try:
        if fid := request.args.get("fid", None):
            file_list = _get_file_list(fid=fid)
        elif path := request.args.get("path", "/"):
            file_list = _get_file_list(path=path)
        return jsonify({"success": True, "data": file_list})
    except Exception as e:
        return jsonify({"success": False, "data": {"error": str(e)}})


def _get_file_list(fid: str = None, path: str = None):
    account = Quark(config_data["cookie"][0])
    paths = []
    if path and not fid:
        path = re.sub(r"/+", "/", path)
        if path == "/":
            fid = 0
        else:
            dir_names = path.split("/")
            if dir_names[0] == "":
                dir_names.pop(0)
            path_fids = []
            current_path = ""
            for dir_name in dir_names:
                current_path += "/" + dir_name
                path_fids.append(current_path)
            if get_fids := account.get_fids(path_fids):
                fid = get_fids[-1]["fid"]
                paths = [
                    {"fid": get_fid["fid"], "name": dir_name}
                    for get_fid, dir_name in zip(get_fids, dir_names)
                ]
            else:
                raise FileNotFoundError("获取fid失败")
    file_list = {
        "fid": fid,
        "list": account.ls_dir(fid)["data"]["list"],
        "paths": paths,
    }
    return file_list


def _path_to_fid(path):
    """根据路径获取文件的fid"""
    if not path:
        raise ValueError("路径不能为空")
    path = re.sub(r"/+", "/", path)
    if path == "/":
        return 0
    file_list = _get_file_list(None, os.path.dirname(path))
    for file in file_list["list"]:
        if file["file_name"] == os.path.basename(path):
            return file["fid"]
    raise FileNotFoundError(f"未找到文件: {path}")


@app.route("/delete_file", methods=["POST"])
def delete_file():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    try:
        fid = request.json.get("fid") or _path_to_fid(request.json.get("path"))
        if fid:
            account = Quark(config_data["cookie"][0])
            response = account.delete([fid])
            response["success"] = response["code"] == 0
            return jsonify(response)
        else:
            raise ValueError("缺失必要字段: fid 或 path")
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/rename_file", methods=["POST"])
def rename_file():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    try:
        fid = request.json.get("fid") or _path_to_fid(request.json.get("path"))
        file_name = request.json.get("file_name")
        if fid and file_name:
            account = Quark(config_data["cookie"][0])
            response = account.rename(fid, file_name)
            response["success"] = response["code"] == 0
            return jsonify(response)
        else:
            raise ValueError("缺失必要字段: fid, file_name")
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# 添加任务接口
@app.route("/api/add_task", methods=["POST"])
def add_task():
    global config_data
    # 验证token
    if not is_login():
        return jsonify({"success": False, "code": 1, "message": "未登录"}), 401
    # 必选字段
    request_data = request.json
    required_fields = ["taskname", "shareurl", "savepath"]
    for field in required_fields:
        if field not in request_data or not request_data[field]:
            return (
                jsonify(
                    {"success": False, "code": 2, "message": f"缺少必要字段: {field}"}
                ),
                400,
            )
    if not request_data.get("addition"):
        request_data["addition"] = task_plugins_config_default
    # 添加任务
    with Config.locked(CONFIG_PATH):
        config_data = Config.read_json(CONFIG_PATH)
        config_data["tasklist"].append(request_data)
        Config.write_json(CONFIG_PATH, config_data)
    logging.info(f">>> 通过API添加任务: {request_data['taskname']}")
    return jsonify(
        {"success": True, "code": 0, "message": "任务添加成功", "data": request_data}
    )


# 定时任务执行的函数
def run_python(args):
    try:
        started, record = get_run_manager().start({}, trigger="scheduled")
        logging.info("定时任务已后台启动" if started else "已有任务运行，本次定时检查合并到当前运行")
        return record
    except (OSError, ValueError):
        logging.error("无法启动定时后台任务，请检查配置及日志目录")


def run_source_monitor():
    """独立后台巡检，不创建转存任务或运行历史。"""
    try:
        current = Config.read_json(CONFIG_PATH)
        if source_option_enabled(current, "monitor"):
            maintain_sources(current, Config.search_health_path(CONFIG_PATH))
    except Exception as error:
        # 网络和配置故障不能终止之后的巡检，也不将认证信息写入日志。
        logging.warning("渠道巡检暂未完成 (%s)", type(error).__name__)


# 重新加载任务
def reload_tasks():
    crontab = config_data.get("crontab")
    trigger = CronTrigger.from_crontab(crontab) if crontab else None
    if scheduler.state == 1:
        scheduler.pause()
    if trigger:
        scheduler.add_job(
            run_python,
            trigger=trigger,
            args=[[SCRIPT_PATH, CONFIG_PATH]],
            id=SCRIPT_PATH,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            replace_existing=True,
        )
    elif scheduler.get_job(SCRIPT_PATH):
        scheduler.remove_job(SCRIPT_PATH)
    monitor_job = scheduler.get_job(SOURCE_MONITOR_JOB)
    if source_option_enabled(config_data, "monitor"):
        if not monitor_job:
            scheduler.add_job(
                run_source_monitor, "interval", minutes=5,
                id=SOURCE_MONITOR_JOB, max_instances=1, coalesce=True,
                misfire_grace_time=300, next_run_time=datetime.now() + timedelta(seconds=25),
            )
    elif monitor_job:
        scheduler.remove_job(SOURCE_MONITOR_JOB)
    # 重载转存定时不会清除巡检任务，也不会推迟已经排好的下一次巡检。
    if scheduler.state == 0 and scheduler.get_jobs():
        scheduler.start()
    elif scheduler.state == 2:
        scheduler.resume()
    logging.info(">>> 重载调度器，定时规则: %s，渠道巡检: %s", crontab or "未设置",
                 "启用" if source_option_enabled(config_data, "monitor") else "停用")
    return bool(crontab)


def init():
    global config_data, task_plugins_config_default
    logging.info(">>> 初始化配置")
    os.makedirs(os.path.dirname(os.path.abspath(CONFIG_PATH)), exist_ok=True)
    with Config.locked(CONFIG_PATH):
        # 检查配置文件是否存在
        new_config = not os.path.exists(CONFIG_PATH)
        if new_config:
            with open("quark_config.json", "rb") as src, open(CONFIG_PATH, "wb") as dest:
                dest.write(src.read())

        # 读取配置
        config_data = Config.read_json(CONFIG_PATH)
        Config.breaking_change_update(config_data)
        Config.remember_resources(Config.resource_history_path(CONFIG_PATH), config_data.get("tasklist", []))
        if not config_data.get("magic_regex"):
            config_data["magic_regex"] = MagicRename().magic_regex

        # 默认管理账号
        config_data["webui"] = {
            "username": os.environ.get("WEBUI_USERNAME")
            or config_data.get("webui", {}).get("username", "admin"),
            "password": os.environ.get("WEBUI_PASSWORD")
            or config_data.get("webui", {}).get("password", "admin123"),
        }

        # 默认定时规则
        if not config_data.get("crontab"):
            config_data["crontab"] = "0 8,18,20 * * *"
        config_data.setdefault("auto_repair", True)
        config_data.setdefault("discovery", {"savepath": "/cartoon"})
        source = config_data.setdefault("source", {})
        source.setdefault("pansou", {"enable": True, "server": "https://so.252035.xyz"})
        source.setdefault("cloudsaver", {"enable": False, "server": "", "username": "", "password": ""})
        source.setdefault("websearch", {"enable": True})
        source.setdefault("monitor", {"enable": True})
        source.setdefault("channelsearch", {"enable": True, "auto_discover": True,
                                            "telegram_channels": ["guoman4K", "kuakedongman"],
                                            "qq_channels": ["pd26778899"]})
        if new_config and os.getenv("PANSOU_SERVER"):
            source["pansou"]["server"] = os.environ["PANSOU_SERVER"]

        # 初始化插件配置
        _, plugins_config_default, task_plugins_config_default = Config.load_plugins()
        for name, config in plugins_config_default.items():
            for key, value in config.items():
                config[key] = (
                    config_data.setdefault("plugins", {})
                    .setdefault(name, {})
                    .get(key, value)
                )
        config_data["plugins"] = plugins_config_default

        # 更新配置
        Config.write_json(CONFIG_PATH, config_data)


if __name__ == "__main__":
    init()
    get_run_manager()
    reload_tasks()
    logging.info(">>> 启动Web服务")
    logging.info(f"运行在: http://{HOST}:{PORT}")
    app.run(
        debug=DEBUG,
        host=HOST,
        port=PORT,
    )
