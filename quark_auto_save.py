# !/usr/bin/env python3
# -*- coding: utf-8 -*-
# Modify: 2026-02-04
# Repo: https://github.com/Cp0204/quark_auto_save
# ConfigFile: quark_config.json
"""
new Env('夸克自动追更');
0 8,18,20 * * * quark_auto_save.py
"""
import os
import re
import sys
import json
import time
import random
import platform
import requests
import importlib
import traceback
import urllib.parse
import unicodedata
import tempfile
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from natsort import natsorted
from media_files import MEDIA_PATTERN, is_media_file, is_video_file, media_title

# 兼容青龙
try:
    from treelib import Tree
except:
    print("正在尝试自动安装依赖...")
    os.system("pip3 install treelib &> /dev/null")
    from treelib import Tree


CONFIG_DATA = {}
NOTIFYS = []
GH_PROXY = os.environ.get("GH_PROXY", "https://ghproxy.net/")


# 发送通知消息
def send_ql_notify(title, body):
    try:
        # 导入通知模块
        import notify

        # 如未配置 push_config 则使用青龙环境通知设置
        if CONFIG_DATA.get("push_config"):
            notify.push_config.update(CONFIG_DATA["push_config"])
            notify.push_config["CONSOLE"] = notify.push_config.get("CONSOLE", True)
        notify.send(title, body)
    except Exception as e:
        if e:
            print("发送通知消息失败！")


# 添加消息
def add_notify(text):
    global NOTIFYS
    NOTIFYS.append(text)
    print("📢", text)
    return text


def is_expired_share(response):
    """Only an explicit cancelled/expired share permits changing a subscription."""
    if response.get("status") in (401, 403, 429) or str(response.get("status", "")).startswith("5"):
        return False
    message = str(response.get("message", "")).lower()
    if re.search(r"提取码|访问码|密码|验证码|登录|登陆|权限|网络|超时|繁忙|频繁|风控|认证|password|passcode|login|timeout|permission", message):
        return False
    return bool(re.search(
        r"(?:分享|链接).{0,12}(?:已取消|已被取消|取消了|已过期|已失效|不存在|已被删除)"
        r"|(?:取消|删除)了.{0,6}分享|(?:share|link).{0,20}(?:expired|cancelled|canceled|not found)",
        message,
    ))


def share_listing_complete(response):
    data = response.get("data") or {}
    files = data.get("list")
    if response.get("code") != 0 or not isinstance(files, list) or data.get("truncated"):
        return False
    # 接口的总数可能包含已隐藏文件；实际读到成功空页也代表可见列表结束。
    if data.get("listing_complete") is True:
        return True
    total = (response.get("metadata") or {}).get("_total", len(files))
    return type(total) is int and 0 <= total <= len(files)


_RESOURCE_SEASON_PATTERN = r"第\s*([\d零一二三四五六七八九十两]+)\s*[季部]|\bseason\s*(\d{1,2})\b|(?<![a-z])s(\d{1,2})(?=e\d|\b)"


def _resource_title_parts(title):
    title = unicodedata.normalize("NFKC", str(title)).casefold()
    seasons = []

    def season_number(match):
        value = next(value for value in match.groups() if value)
        if value.isdigit():
            seasons.append(int(value))
        else:
            digits = {char: i for i, char in enumerate("零一二三四五六七八九")}
            value = value.replace("两", "二")
            if "十" in value:
                tens, ones = value.split("十", 1)
                seasons.append(digits.get(tens, 1) * 10 + digits.get(ones, 0))
            else:
                seasons.append(digits.get(value, -1))
        return " "

    title = re.sub(_RESOURCE_SEASON_PATTERN, season_number, title)
    years = set(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", title))
    title = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", " ", title)
    # shortcut: only known release labels are stripped; ambiguous names stay manual.
    title = re.sub(r"(?:更新至?|更至|全)\s*第?\s*\d+\s*集?|第\s*\d+(?:\s*[-~至]\s*\d+)?\s*集|\be\d{1,3}\b", " ", title)
    title = re.sub(r"^\s*#(?:动漫|动画|番剧|电视剧)\s*", " ", title)
    title = re.sub(r"\b(?:2160p|1080p|720p|4k|hdr|hdr10|sdr|bluray|web[- .]?dl|h\.?26[45]|x26[45])\b|中文字幕|中英双字|简繁字幕|国语中字|高码率|高码|杜比视界|内[嵌封][简繁](?:中|繁)?|高清|超清|完结|持续更新|连载中", " ", title)
    title = re.sub(r"\b\d+(?:\.\d+)?\s*[gm]b\s*/\s*集", " ", title)
    return re.sub(r"[\W_]+", "", title), set(seasons), years


def _resource_seasons_match(expected, actual, years):
    return expected == actual or (bool(years) and not expected and actual == {1})


def _resource_candidate_parts(expected, candidate):
    name, seasons, _ = _resource_title_parts(expected)
    other_name, other_seasons, other_years = _resource_title_parts(candidate)
    # Only a task with an explicit season can interpret its exact base name + number.
    if len(seasons) == 1 and not other_seasons:
        numbered = re.fullmatch(re.escape(name) + r"(\d{1,2})", other_name)
        if numbered:
            other_name, other_seasons = name, {int(numbered.group(1))}
    return other_name, other_seasons, other_years


def _resource_video_title(filename):
    """识别视频发布标签；不猜测被替换的片名字词，也不删除片名自带的数字。"""
    title = unicodedata.normalize("NFKC", re.sub(r"\.[^.]+$", "", filename))
    title = re.sub(r"^\s*[\[【]\s*(?:0?[1-9]|1[0-2])月(?:新番)?\s*[\]】]\s*", "", title)
    title = re.sub(r"\s*[\[【](?:完全无修版|无修版|未删减版|无删减版|修正版)[\]】]\s*$", "", title)
    # shortcut: bare episode numbers need zero padding; ambiguous sequel numbers stay intact.
    return re.sub(r"(?:\s+|\s*[-_]\s*)0\d{1,2}\s*$", "", title).strip()


def _resource_episode_number(task, filename):
    """只识别单作品目录中的明确集号，不用重命名规则里的任意数字兜底。"""
    if not is_video_file({"file_name": filename}):
        return None
    title = task.get("taskname", "")
    name, seasons, years = _resource_title_parts(title)
    season = next(iter(seasons)) if len(seasons) == 1 else (1 if not seasons and years else None)
    if not name or not season:
        return None
    stem = unicodedata.normalize("NFKC", filename.rsplit(".", 1)[0]).strip()
    stem = re.sub(r"^\s*[\[【]\s*(?:0?[1-9]|1[0-2])月(?:新番)?\s*[\]】]\s*", "", stem)
    stem = re.sub(r"\s*[\[【](?:完全无修版|无修版|未删减版|无删减版|修正版)[\]】]\s*$", "", stem)
    stem = re.sub(r"(?:[ ._-]+|\s*[\[【(])(?:2160p|1080p|720p|4k)[\]】)]?\s*$", "", stem, flags=re.I)
    prefix, number = "", None
    match = re.fullmatch(r"(.*?)s(\d{1,2})e(\d{1,3})", stem, re.I)
    if match:
        if int(match[2]) != season:
            return None
        prefix, number = match[1], int(match[3])
    else:
        match = re.fullmatch(r"(.*?)(?:第\s*(\d{1,3})\s*[集话]|(?:ep?|episode)[ ._-]*(\d{1,3}))", stem, re.I)
        if match:
            prefix, number = match[1], int(match[2] or match[3])
        elif re.fullmatch(r"\d{1,3}", stem):
            number = int(stem)
        elif match := re.fullmatch(r"(.+?)(?:\s+|[._-])0(\d{1,2})", stem):
            prefix, number = match[1], int(match[2])
    if number is None or number < 1:
        return None
    if re.search(r"s\d{1,2}e\d|\be(?:p)?\d|第\s*\d+\s*[集话]", prefix, re.I):
        return None
    other_name, other_seasons, other_years = _resource_title_parts(prefix)
    names = {name} | {_resource_title_parts(alias)[0] for alias in task.get("search_aliases", []) if isinstance(alias, str)}
    names |= {first + second for first in names for second in names if first != second}
    if (other_name and other_name not in names) or (other_seasons and other_seasons != {season}) or (years and other_years and years != other_years):
        return None
    return number


def _episode_repair_files(task, files, required_episodes):
    mr = MagicRename(CONFIG_DATA.get("magic_regex", {}))
    mr.set_taskname(task["taskname"])
    pattern, replace = mr.magic_regex_conv(task.get("pattern", ""), task.get("replace", ""))
    selected, numbers = {}, set()
    for item in files:
        if not is_video_file(item):
            continue
        filename = item.get("file_name", "")
        episode = _resource_episode_number(task, filename)
        if episode not in required_episodes or episode in numbers or not re.search(pattern, filename):
            continue
        renamed = mr.media_name(pattern, replace, filename)
        if _resource_episode_number(task, renamed) != episode:
            continue
        selected[item["fid"]] = episode
        numbers.add(episode)
    return selected


def _resource_search_queries(title, aliases):
    queries, seen = [], set()

    def add(name):
        name = unicodedata.normalize("NFKC", name)
        name = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", " ", name)
        query = re.sub(r"[^\w\s/']+", " ", name.replace("’", "'"))
        query = re.sub(r"\s+", " ", query).strip()
        key = re.sub(r"[\W_]+", "", query.casefold())
        if key and key not in seen:
            seen.add(key)
            queries.append(query)

    # Give every official name a turn before spending requests on season spellings.
    for name in [title] + aliases:
        add(name)
    _, seasons, _ = _resource_title_parts(title)
    if len(seasons) == 1:
        season = next(iter(seasons))
        if 0 < season < 100:
            number = "零一二三四五六七八九"
            chinese = number[season] if season < 10 else (
                (number[season // 10] if season >= 20 else "") + "十"
                + (number[season % 10] if season % 10 else "")
            )
            initial = len(queries)
            for label in (f"第{season}季", f"第{chinese}季", f"S{season:02d}"):
                add(re.sub(_RESOURCE_SEASON_PATTERN, label, title, flags=re.I))
                if len(queries) - initial == 2:
                    break
            # Broaden only the lookup; verification still requires the original season.
            add(re.sub(_RESOURCE_SEASON_PATTERN, " ", title, flags=re.I))
    return queries


def resource_titles_match(expected, candidate, allow_missing_year=False, aliases=()):
    name, seasons, years = _resource_title_parts(expected)
    other_name, other_seasons, other_years = _resource_candidate_parts(expected, candidate)
    names = {name}
    for alias in aliases[:8] if isinstance(aliases, (list, tuple)) else ():
        if not isinstance(alias, str) or len(alias) > 200:
            continue
        alias_name, alias_seasons, alias_years = _resource_title_parts(alias)
        if alias_name and (not alias_seasons or _resource_seasons_match(seasons, alias_seasons, years)) and (not years or not alias_years or alias_years == years):
            names.add(alias_name)
    # A bilingual release may join two names supplied by the same catalogue entry.
    title_matches = other_name in names or any(
        other_name == first + second for first in names for second in names if first != second
    )
    return bool(name and title_matches and _resource_seasons_match(seasons, other_seasons, years)
                and len(seasons) <= 1 and (not years or years == other_years
                or (allow_missing_year and not other_years)))


def apply_media_rename_defaults(task):
    if not task.get("resource_id"):
        return task
    title = task.get("taskname", "")
    safe_title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")[:180]
    migrated_path = task.get("savepath") in ("/追更", "/追更/" + safe_title)
    if migrated_path:
        task["savepath"] = "/cartoon"
        task.pop("aria2_pending", None)
        task.pop("aria2_pending_dirs", None)
    if task.get("savepath") == "/cartoon":
        aria2 = task.setdefault("addition", {}).setdefault("aria2", {})
        if migrated_path or aria2.get("save_path") in (None, "", "cartoon", "追更/" + safe_title):
            aria2["save_path"] = "cartoon"
        if migrated_path and aria2.get("auto_download"):
            aria2["download_existing"] = True
    legacy_pattern = r"(?i).*\.(mp4|mkv|mov|m4v|avi|ts)$"
    if (task.get("replace")
            or task.get("pattern") not in (None, "", legacy_pattern, MEDIA_PATTERN)):
        return task
    _, seasons, _ = _resource_title_parts(title)
    if len(seasons) > 1 or re.search(r"第\s*[\d零一二三四五六七八九十两]+\s*[-~～至到、]\s*[\d零一二三四五六七八九十两]+\s*季", title):
        return task
    season = next(iter(seasons)) if seasons else 1
    if season < 1:
        return task
    task["pattern"] = MEDIA_PATTERN
    task["replace"] = f"{{TASKNAME}} S{season:02d}E{{EXX}}.{{EXT}}"
    return task


class Config:
    _thread_lock = threading.RLock()
    _locked_paths = threading.local()

    @staticmethod
    @contextmanager
    def locked(config_path):
        """同目录锁文件跨进程协调读改写；同线程允许嵌套调用。"""
        path = os.path.normcase(os.path.realpath(os.path.abspath(config_path)))
        with Config._thread_lock:
            held = getattr(Config._locked_paths, "paths", set())
            if path in held:
                yield
                return
            with open(path + ".lock", "a+b") as lock:
                if os.name == "nt":
                    import msvcrt

                    lock.seek(0, os.SEEK_END)
                    if not lock.tell():
                        lock.write(b"\0")
                        lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                Config._locked_paths.paths = held
                held.add(path)
                try:
                    yield
                finally:
                    held.remove(path)
                    if os.name == "nt":
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    # 下载配置
    def download_file(url, save_path):
        response = requests.get(url)
        if response.status_code == 200:
            with open(save_path, "wb") as file:
                file.write(response.content)
            return True
        else:
            return False

    # 读取 JSON 文件内容
    def read_json(config_path):
        with Config.locked(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        return data

    # 将数据写入 JSON 文件
    def write_json(config_path, data):
        config_path = os.path.realpath(os.path.abspath(config_path))
        with Config.locked(config_path):
            fd, temporary = tempfile.mkstemp(prefix=".quark-config-", suffix=".tmp", dir=os.path.dirname(config_path))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, sort_keys=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                if os.path.exists(config_path):
                    os.chmod(temporary, os.stat(config_path).st_mode & 0o777)
                os.replace(temporary, config_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def _merge_changes(current, original, updated):
        """仅写入运行期间改变、且磁盘仍保留原值的字段。"""
        missing = object()
        for key in original.keys() | updated.keys():
            before = original.get(key, missing)
            after = updated.get(key, missing)
            latest = current.get(key, missing)
            if before == after:
                continue
            if isinstance(after, dict) and isinstance(latest, dict) and (isinstance(before, dict) or before is missing):
                Config._merge_changes(current[key], {} if before is missing else before, after)
            elif latest == before:
                if after is missing:
                    current.pop(key, None)
                else:
                    current[key] = deepcopy(after)

    def resource_history_path(config_path):
        return os.path.splitext(os.path.abspath(config_path))[0] + ".resources.json"

    def resource_history(path):
        if not path or not os.path.exists(path):
            return []
        try:
            data = Config.read_json(path)
            return [item for item in data if isinstance(item, dict)][:256] if isinstance(data, list) else []
        except (OSError, ValueError) as exc:
            print(f"资源记录读取失败（{type(exc).__name__}），继续查询其他来源")
            return []

    def remember_resources(path, tasks):
        """仅记录作品与分享线索；删除订阅不会删线索，使用前仍必须实时验证。"""
        from app.sdk.common import normalize_share_url

        if not path or not tasks:
            return
        try:
            with Config.locked(path):
                old = Config.resource_history(path)
                records, seen, counts = [], set(), {}
                for task in list(tasks) + old:
                    if not isinstance(task, dict):
                        continue
                    title, link = task.get("taskname"), task.get("shareurl")
                    if not isinstance(title, str) or not title.strip() or len(title) > 200:
                        continue
                    link = normalize_share_url(link) if isinstance(link, str) and len(link) <= 4096 else ""
                    if not link:
                        continue
                    title = title.strip()
                    key = (title.casefold(), link)
                    if key in seen or counts.get(key[0], 0) >= 5:
                        continue
                    seen.add(key)
                    counts[key[0]] = counts.get(key[0], 0) + 1
                    record = {"taskname": title, "shareurl": link}
                    aliases = task.get("search_aliases", [])
                    if isinstance(aliases, list):
                        aliases = list(dict.fromkeys(value.strip() for value in aliases
                                                     if isinstance(value, str) and value.strip() and len(value) <= 200))[:8]
                        if aliases:
                            record["search_aliases"] = aliases
                    records.append(record)
                    if len(records) == 256:
                        break
                if records != old:
                    Config.write_json(path, records)
        except (OSError, ValueError) as exc:
            print(f"资源记录保存失败（{type(exc).__name__}），本次转存与搜索继续")

    def merge_task_state(config_data, original, updated):
        matches = [task for task in config_data.get("tasklist", []) if all(
            task.get(key) == original.get(key) for key in ("taskname", "shareurl", "savepath")
        )]
        if len(matches) != 1:
            return False
        fields = ("shareurl", "shareurl_ban", "startfid", "repair_history", "search_aliases", "addition", "pattern", "replace", "savepath", "rename_pending", "aria2_pending", "aria2_pending_dirs")
        Config._merge_changes(matches[0], {key: original[key] for key in fields if key in original},
                             {key: updated[key] for key in fields if key in updated})
        return True

    def merge_runtime_state(current, original, updated):
        originals = original.get("tasklist", [])
        for task in originals:
            identity = ("taskname", "shareurl", "savepath")
            if sum(all(other.get(key) == task.get(key) for key in identity) for other in originals) != 1:
                continue
            matches = [other for other in updated.get("tasklist", []) if all(
                other.get(key) == task.get(key) for key in identity)]
            if not matches and task.get("resource_id"):
                resource_id = task["resource_id"]
                groups = (originals, updated.get("tasklist", []), current.get("tasklist", []))
                if any(sum(other.get("resource_id") == resource_id for other in tasks) != 1 for tasks in groups):
                    continue
                matches = [other for other in updated.get("tasklist", []) if other.get("resource_id") == resource_id]
            if not matches:
                # 自动换源会改变 shareurl；仅作品名和保存路径唯一时允许配对。
                pair = ("taskname", "savepath")
                if sum(all(other.get(key) == task.get(key) for key in pair) for other in originals) != 1:
                    continue
                matches = [other for other in updated.get("tasklist", []) if all(
                    other.get(key) == task.get(key) for key in pair)]
            if len(matches) == 1:
                Config.merge_task_state(current, task, matches[0])
        Config._merge_changes(current, {"plugins": original["plugins"]} if "plugins" in original else {},
                             {"plugins": updated["plugins"]} if "plugins" in updated else {})
        return current

    # 读取CK
    def get_cookies(cookie_val):
        if isinstance(cookie_val, list):
            return cookie_val
        elif cookie_val:
            if "\n" in cookie_val:
                return cookie_val.split("\n")
            else:
                return [cookie_val]
        else:
            return False

    def load_plugins(plugins_config={}, plugins_dir="plugins"):
        PLUGIN_FLAGS = os.environ.get("PLUGIN_FLAGS", "").split(",")
        plugins_available = {}
        task_plugins_config = {}
        # 获取所有模块
        sys_ext = "pyd" if platform.system() == "Windows" else "so"
        module_ext = [".py", f".{sys_ext}"]
        all_modules = [
            f.replace(ext, "")
            for f in os.listdir(plugins_dir)
            for ext in module_ext
            if f.endswith(ext)
        ]
        # 调整模块优先级
        priority_path = os.path.join(plugins_dir, "_priority.json")
        try:
            with open(priority_path, encoding="utf-8") as f:
                priority_modules = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            priority_modules = []
        if priority_modules:
            all_modules = [
                module for module in priority_modules if module in all_modules
            ] + [module for module in all_modules if module not in priority_modules]
        # 加载模块
        for module_name in all_modules:
            if f"-{module_name}" in PLUGIN_FLAGS:
                continue
            try:
                module = importlib.import_module(f"{plugins_dir}.{module_name}")
                ServerClass = getattr(module, module_name.capitalize())
                # 检查配置中是否存在该模块的配置
                if module_name in plugins_config:
                    plugin = ServerClass(**plugins_config[module_name])
                    plugins_available[module_name] = plugin
                else:
                    plugin = ServerClass()
                    plugins_config[module_name] = plugin.default_config
                # 检查插件是否支持单独任务配置
                if hasattr(plugin, "default_task_config"):
                    task_plugins_config[module_name] = plugin.default_task_config
            except (ImportError, AttributeError) as e:
                print(f"载入模块 {module_name} 失败: {e}")
        return plugins_available, plugins_config, task_plugins_config

    def breaking_change_update(config_data):
        discovery = config_data.setdefault("discovery", {})
        if isinstance(discovery, dict) and discovery.get("savepath") in (None, "/追更"):
            discovery["savepath"] = "/cartoon"
        plugins = config_data.get("plugins")
        aria2 = plugins.get("aria2") if isinstance(plugins, dict) else None
        if isinstance(aria2, dict) and aria2.get("dir") == "/Downloads":
            aria2["dir"] = "/downloads"
        # 🔼 Update config v0.5.x to 0.6.0
        for task in config_data.get("tasklist", []):
            if "$TASKNAME" in task.get("replace", ""):
                task["replace"] = task["replace"].replace("$TASKNAME", "{TASKNAME}")
            apply_media_rename_defaults(task)


class MagicRename:

    magic_regex = {
        "$TV": {
            "pattern": r".*?([Ss]\d{1,2})?(?:[第EePpXx\.\-\_\( ]{1,2}|^)(\d{1,3})(?!\d).*?\.(mp4|mkv)",
            "replace": r"\1E\2.\3",
        },
        "$BLACK_WORD": {
            "pattern": r"^(?!.*纯享)(?!.*加更)(?!.*超前企划)(?!.*训练室)(?!.*蒸蒸日上).*",
            "replace": "",
        },
    }

    magic_variable = {
        "{TASKNAME}": "",
        "{I}": 1,
        "{EXT}": [r"(?<=\.)\w+$"],
        "{CHINESE}": [r"[\u4e00-\u9fa5]{2,}"],
        "{DATE}": [
            r"(18|19|20)?\d{2}[\.\-/年]\d{1,2}[\.\-/月]\d{1,2}",
            r"(?<!\d)[12]\d{3}[01]?\d[0123]?\d",
            r"(?<!\d)[01]?\d[\.\-/月][0123]?\d",
        ],
        "{YEAR}": [r"(?<!\d)(18|19|20)\d{2}(?!\d)"],
        "{S}": [r"(?<=[Ss])\d{1,2}(?=[EeXx])", r"(?<=[Ss])\d{1,2}"],
        "{SXX}": [r"[Ss]\d{1,2}(?=[EeXx])", r"[Ss]\d{1,2}"],
        "{E}": [
            r"(?<=[Ss]\d\d[Ee])\d{1,3}",
            r"(?<=[Ee])\d{1,3}",
            r"(?<=[Ee][Pp])\d{1,3}",
            r"(?<=第)\d{1,3}(?=[集期话部篇])",
            r"(?<!\d)\d{1,3}(?=[集期话部篇])",
            r"(?!.*19)(?!.*20)(?<=[\._])\d{1,3}(?=[\._])",
            r"^\d{1,3}(?=\.\w+)",
            r"(?<!\d)\d{1,3}(?!\d)(?!$)",
        ],
        "{EXX}": [],  # 严格识别真实集号，最少补齐两位；不使用 {E} 的任意数字兜底。
        "{PART}": [
            r"(?<=[集期话部篇第])[上中下一二三四五六七八九十]",
            r"[上中下一二三四五六七八九十]",
        ],
        "{VER}": [r"[\u4e00-\u9fa5]+版"],
    }

    priority_list = [
        "上",
        "中",
        "下",
        "一",
        "二",
        "三",
        "四",
        "五",
        "六",
        "七",
        "八",
        "九",
        "十",
        "百",
        "千",
        "万",
    ]

    def __init__(self, magic_regex={}, magic_variable={}):
        self.magic_regex = {**self.magic_regex, **magic_regex}
        self.magic_variable = {**self.magic_variable, **magic_variable}
        self.dir_filename_dict = {}

    def set_taskname(self, taskname):
        """设置任务名称"""
        self.magic_variable["{TASKNAME}"] = media_title(taskname)

    def magic_regex_conv(self, pattern, replace):
        """魔法正则匹配"""
        keyword = pattern
        if keyword in self.magic_regex:
            pattern = self.magic_regex[keyword]["pattern"]
            if replace == "":
                replace = self.magic_regex[keyword]["replace"]
        return pattern, replace

    subtitle_suffix = r"(?:\.(?:zh(?:[-_](?:hans|hant|cn|tw|hk))?|chs|cht|chi|zho|en|eng|ja|jpn|ko|kor|简体|繁体|中英|简英|繁英|forced|sdh|default))+$"

    @classmethod
    def _strict_episode(cls, file_name):
        stem = os.path.splitext(file_name)[0].strip()
        stem = re.sub(r"^\s*(?:[\[【](?:0?[1-9]|1[0-2])月(?:新番)?[\]】]\s*)+", "", stem)
        while True:
            clean = re.sub(
                r"\s*[\[【(（](?:完全无修版|无修版|未删减版|无删减版|修正版|2160p|1080p|720p|4k)[\]】)）]\s*$",
                "", stem, flags=re.I,
            )
            clean = re.sub(r"[ ._-]+(?:2160p|1080p|720p|4k)\s*$", "", clean, flags=re.I).strip()
            if clean == stem:
                break
            stem = clean
        stem = re.sub(cls.subtitle_suffix, "", stem, flags=re.I)
        if re.search(r"(?<!\d)\d{1,3}\s*[-~～]\s*\d{1,3}$", stem):
            return None
        match = re.fullmatch(
            r"(.*?)(?:(?<![A-Za-z0-9])s\d{1,2}e(\d{1,3})|(?<![A-Za-z0-9])(?:ep?|episode)[ ._-]*(\d{1,3})|第\s*(\d{1,3})\s*[集话])",
            stem, re.I,
        )
        if match:
            prefix, *numbers = match.groups()
            number = next(value for value in numbers if value is not None)
        elif re.fullmatch(r"\d{1,3}", stem):
            prefix, number = "", stem
        elif match := re.fullmatch(r"(.+?)[ ._-]+(0\d{1,2})", stem):
            prefix, number = match.groups()
        else:
            return None
        if (int(number) < 1 or re.search(r"s\d{1,2}e\d|(?<![A-Za-z])e(?:p)?\d|第\s*\d+\s*[集话]", prefix, re.I)
                or re.search(r"(?:^|[ ._-])(?:sp|ova|oad|op|ed|pv)\s*$", prefix, re.I)
                or re.search(r"(?:^|[ ._-])\d{1,3}\s*[-~～]\s*$", prefix)):
            return None
        return number

    def sub(self, pattern, replace, file_name):
        """魔法正则、变量替换"""
        if not replace:
            return file_name
        if "{EXX}" in replace:
            episode = self._strict_episode(file_name)
            if episode is None:
                return file_name
            replace = replace.replace("{EXX}", episode.zfill(2))
        # 预处理替换变量
        for key, p_list in self.magic_variable.items():
            if key in replace:
                match = None
                # 正则类替换变量
                if p_list and isinstance(p_list, list):
                    source_name = file_name
                    if key == "{E}":
                        # 发布月份不是集号；只去掉明确的开头月份标签。
                        source_name = re.sub(
                            r"^\s*(?:[\[【](?:0?[1-9]|1[0-2])月(?:新番)?[\]】]\s*)+",
                            "", file_name,
                        )
                    for p in p_list:
                        match = re.search(p, source_name)
                        if match:
                            # 匹配成功，替换为匹配到的值
                            value = match.group()
                            # 日期格式处理：补全、格式化
                            if key == "{DATE}":
                                value = "".join(
                                    [char for char in value if char.isdigit()]
                                )
                                value = (
                                    str(datetime.now().year)[: (8 - len(value))] + value
                                )
                            replace = replace.replace(key, value)
                            break
                # 非正则类替换变量
                if key == "{TASKNAME}":
                    replace = replace.replace(key, self.magic_variable["{TASKNAME}"])
                elif key == "{SXX}" and not match:
                    replace = replace.replace(key, "S01")
                elif key == "{I}":
                    continue
                else:
                    # 清理未匹配的 magic_variable key
                    replace = replace.replace(key, "")
        if pattern and replace:
            file_name = re.sub(pattern, replace, file_name)
        else:
            file_name = replace
        return file_name

    def media_name(self, pattern, replace, file_name):
        """保留媒体类型和安全文件名；不适用的字幕规则保留原名。"""
        original = {"file_name": file_name, "dir": False}
        if not is_media_file(original):
            return file_name
        try:
            pattern, replace = self.magic_regex_conv(pattern, replace)
            if pattern and not re.search(pattern, file_name):
                return file_name
            renamed = self.sub(pattern, replace, file_name)
        except (re.error, IndexError):
            return file_name
        if not is_video_file(original) and renamed != file_name:
            if suffix := re.search(self.subtitle_suffix, os.path.splitext(file_name)[0], re.I):
                stem, extension = os.path.splitext(renamed)
                if not stem.casefold().endswith(suffix.group().casefold()):
                    renamed = stem + suffix.group() + extension
        if (not renamed or renamed != renamed.strip() or renamed.endswith(".")
                or ".." in renamed or re.search(r'[\\/:*?"<>|]', renamed)
                or any(unicodedata.category(char).startswith("C") for char in renamed)):
            return file_name
        result = {"file_name": renamed, "dir": False}
        if not is_media_file(result) or is_video_file(result) != is_video_file(original):
            return file_name
        return renamed

    def _custom_sort_key(self, name):
        """自定义排序键"""
        for i, keyword in enumerate(self.priority_list):
            if keyword in name:
                name = name.replace(keyword, f"_{i:02d}_")  # 替换为数字，方便排序
        return name

    def sort_file_list(self, file_list, dir_filename_dict={}):
        """文件列表统一排序，给{I+}赋值"""
        filename_list = [
            # 强制加入`文件修改时间`字段供排序，效果：1无可排序字符时则按修改时间排序，2和目录已有文件重名时始终在其后
            f"{f['file_name_re']}_{f['updated_at']}"
            for f in file_list
            if f.get("file_name_re") and not f["dir"]
        ]
        # print(f"filename_list_before: {filename_list}")
        dir_filename_dict = dir_filename_dict or self.dir_filename_dict
        # print(f"dir_filename_list: {dir_filename_list}")
        # 合并目录文件列表
        filename_list = list(set(filename_list) | set(dir_filename_dict.values()))
        filename_list = natsorted(filename_list, key=self._custom_sort_key)
        filename_index = {}
        for name in filename_list:
            if name in dir_filename_dict.values():
                continue
            i = filename_list.index(name) + 1
            while i in dir_filename_dict.keys():
                i += 1
            dir_filename_dict[i] = name
            filename_index[name] = i
        for file in file_list:
            if file.get("file_name_re"):
                if match := re.search(r"\{I+\}", file["file_name_re"]):
                    i = filename_index.get(
                        f"{file['file_name_re']}_{file['updated_at']}", 0
                    )
                    file["file_name_re"] = re.sub(
                        match.group(),
                        str(i).zfill(match.group().count("I")),
                        file["file_name_re"],
                    )

    def set_dir_file_list(self, file_list, replace):
        """设置目录文件列表"""
        self.dir_filename_dict = {}
        filename_list = [f["file_name"] for f in file_list if not f["dir"]]
        filename_list.sort()
        if not filename_list:
            return
        if match := re.search(r"\{I+\}", replace):
            # 由替换式转换匹配式
            magic_i = match.group()
            pattern_i = r"\d" * magic_i.count("I")
            pattern = replace.replace(match.group(), "🔢")
            for key, _ in self.magic_variable.items():
                if key in pattern:
                    pattern = pattern.replace(key, "🔣")
            pattern = re.sub(r"\\[0-9]+", "🔣", pattern)  # \1 \2 \3
            pattern = f"({re.escape(pattern).replace('🔣', '.*?').replace('🔢', f')({pattern_i})(')})"
            # print(f"pattern: {pattern}")
            # 获取起始编号
            if match := re.match(pattern, filename_list[-1]):
                self.magic_variable["{I}"] = int(match.group(2))
            # 目录文件列表
            for filename in filename_list:
                if match := re.match(pattern, filename):
                    self.dir_filename_dict[int(match.group(2))] = (
                        match.group(1) + magic_i + match.group(3)
                    )
            # print(f"filename_list: {self.filename_list}")

    def is_exists(self, filename, filename_list, ignore_ext=False):
        """判断文件是否存在，处理忽略扩展名"""
        # print(f"filename: {filename} filename_list: {filename_list}")
        if ignore_ext:
            filename = os.path.splitext(filename)[0]
            filename_list = [os.path.splitext(f)[0] for f in filename_list]
        # {I+} 模式，用I通配数字序号
        if match := re.search(r"\{I+\}", filename):
            magic_i = match.group()
            pattern_i = r"\d" * magic_i.count("I")
            pattern = re.escape(filename).replace(re.escape(magic_i), pattern_i)
            for filename in filename_list:
                if re.match(pattern, filename):
                    return filename
            return None
        else:
            return filename if filename in filename_list else None


class Quark:
    BASE_URL = "https://drive-pc.quark.cn"
    BASE_URL_APP = "https://drive-m.quark.cn"
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) quark-cloud-drive/3.14.2 Chrome/112.0.5615.165 Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch"

    def __init__(self, cookie="", index=0):
        self.cookie = cookie.strip()
        self.resource_history_path = None
        self.index = index + 1
        self.is_active = False
        self.nickname = ""
        self.mparam = self._match_mparam_form_cookie(cookie)
        self.savepath_fid = {"/": "0"}

    def _match_mparam_form_cookie(self, cookie):
        mparam = {}
        kps_match = re.search(r"(?<!\w)kps=([a-zA-Z0-9%+/=]+)[;&]?", cookie)
        sign_match = re.search(r"(?<!\w)sign=([a-zA-Z0-9%+/=]+)[;&]?", cookie)
        vcode_match = re.search(r"(?<!\w)vcode=([a-zA-Z0-9%+/=]+)[;&]?", cookie)
        if kps_match and sign_match and vcode_match:
            mparam = {
                "kps": kps_match.group(1).replace("%25", "%"),
                "sign": sign_match.group(1).replace("%25", "%"),
                "vcode": vcode_match.group(1).replace("%25", "%"),
            }
        return mparam

    def _send_request(self, method, url, **kwargs):
        headers = {
            "cookie": self.cookie,
            "content-type": "application/json",
            "user-agent": self.USER_AGENT,
        }
        if "headers" in kwargs:
            headers = kwargs["headers"]
            del kwargs["headers"]
        if self.mparam and "share" in url and self.BASE_URL in url:
            url = url.replace(self.BASE_URL, self.BASE_URL_APP)
            kwargs["params"].update(
                {
                    "device_model": "M2011K2C",
                    "entry": "default_clouddrive",
                    "_t_group": "0%3A_s_vp%3A1",
                    "dmn": "Mi%2B11",
                    "fr": "android",
                    "pf": "3300",
                    "bi": "35937",
                    "ve": "7.4.5.680",
                    "ss": "411x875",
                    "mi": "M2011K2C",
                    "nt": "5",
                    "nw": "0",
                    "kt": "4",
                    "pr": "ucpro",
                    "sv": "release",
                    "dt": "phone",
                    "data_from": "ucapi",
                    "kps": self.mparam.get("kps"),
                    "sign": self.mparam.get("sign"),
                    "vcode": self.mparam.get("vcode"),
                    "app": "clouddrive",
                    "kkkk": "1",
                }
            )
            del headers["cookie"]
        try:
            response = requests.request(method, url, headers=headers, **kwargs)
            # print(f"{response.text}")
            # response.raise_for_status()  # 检查请求是否成功，但返回非200也会抛出异常
            return response
        except Exception as e:
            print(f"_send_request error:\n{e}")
            fake_response = requests.Response()
            fake_response.status_code = 500
            fake_response._content = (
                b'{"status": 500, "code": 1, "message": "request error"}'
            )
            return fake_response

    def init(self):
        account_info = self.get_account_info()
        if account_info:
            self.is_active = True
            self.nickname = account_info["nickname"]
            return account_info
        else:
            return False

    def get_account_info(self):
        url = "https://pan.quark.cn/account/info"
        querystring = {"fr": "pc", "platform": "pc"}
        response = self._send_request("GET", url, params=querystring).json()
        if response.get("data"):
            return response["data"]
        else:
            return False

    def get_growth_info(self):
        url = f"{self.BASE_URL_APP}/1/clouddrive/capacity/growth/info"
        querystring = {
            "pr": "ucpro",
            "fr": "android",
            "kps": self.mparam.get("kps"),
            "sign": self.mparam.get("sign"),
            "vcode": self.mparam.get("vcode"),
        }
        headers = {
            "content-type": "application/json",
        }
        response = self._send_request(
            "GET", url, headers=headers, params=querystring
        ).json()
        if response.get("data"):
            return response["data"]
        else:
            return False

    def get_growth_sign(self):
        url = f"{self.BASE_URL_APP}/1/clouddrive/capacity/growth/sign"
        querystring = {
            "pr": "ucpro",
            "fr": "android",
            "kps": self.mparam.get("kps"),
            "sign": self.mparam.get("sign"),
            "vcode": self.mparam.get("vcode"),
        }
        payload = {
            "sign_cyclic": True,
        }
        headers = {
            "content-type": "application/json",
        }
        response = self._send_request(
            "POST", url, json=payload, headers=headers, params=querystring
        ).json()
        if response.get("data"):
            return True, response["data"]["sign_daily_reward"]
        else:
            return False, response["message"]

    # 可验证资源是否失效
    def get_stoken(self, pwd_id, passcode=""):
        url = f"{self.BASE_URL}/1/clouddrive/share/sharepage/token"
        querystring = {"pr": "ucpro", "fr": "pc"}
        payload = {"pwd_id": pwd_id, "passcode": passcode}
        response = self._send_request(
            "POST", url, json=payload, params=querystring, timeout=(5, 15)
        ).json()
        return response

    def get_detail(
        self, pwd_id, stoken, pdir_fid, _fetch_share=0, fetch_share_full_path=0,
        max_pages=None,
    ):
        temporary_error = {"code": 1, "status": 503, "message": "分享服务暂不可用或返回格式异常，请稍后重试"}
        list_merge = []
        share_info = None
        complete = False
        page = 1
        while True:
            url = f"{self.BASE_URL}/1/clouddrive/share/sharepage/detail"
            querystring = {
                "pr": "ucpro",
                "fr": "pc",
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": pdir_fid,
                "force": "0",
                "_page": page,
                "_size": "50",
                "_fetch_banner": "0",
                "_fetch_share": _fetch_share,
                "_fetch_total": "1",
                "_sort": "file_type:asc,updated_at:desc",
                "ver": "2",
                "fetch_share_full_path": fetch_share_full_path,
            }
            try:
                response = self._send_request(
                    "GET", url, params=querystring, timeout=(5, 15)
                ).json()
            except (requests.RequestException, ValueError) as exc:
                print(f"读取分享暂时失败：{type(exc).__name__}")
                return temporary_error
            if not isinstance(response, dict) or "code" not in response:
                return temporary_error
            if str(response.get("status", "")).startswith("5"):
                return temporary_error
            if response["code"] != 0:
                return response
            data = response.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                return temporary_error
            files = data["list"]
            if page == 1:
                share_info = data.get("share")
            if any(not isinstance(item, dict) or not isinstance(item.get("file_name"), str)
                   or "fid" not in item or not isinstance(item.get("dir"), bool) for item in files):
                return temporary_error
            if files:
                metadata = response.get("metadata")
                total = metadata.get("_total") if isinstance(metadata, dict) else None
                if type(total) is not int or total < 0:
                    return temporary_error
                list_merge += files
                page += 1
            else:
                complete = True
                break
            if len(list_merge) >= total or (max_pages and page > max_pages):
                complete = len(list_merge) >= total
                if len(list_merge) < total:
                    response["data"]["truncated"] = True
                break
        response["data"]["list"] = list_merge
        response["data"]["listing_complete"] = complete
        if isinstance(share_info, dict):
            response["data"]["share"] = share_info
        return response

    def get_fids(self, file_paths):
        fids = []
        while True:
            url = f"{self.BASE_URL}/1/clouddrive/file/info/path_list"
            querystring = {"pr": "ucpro", "fr": "pc"}
            payload = {"file_path": file_paths[:50], "namespace": "0"}
            response = self._send_request(
                "POST", url, json=payload, params=querystring
            ).json()
            if response["code"] == 0:
                fids += response["data"]
                file_paths = file_paths[50:]
            else:
                print(f"获取目录ID：失败, {response['message']}")
                break
            if len(file_paths) == 0:
                break
        return fids

    def ls_dir(self, pdir_fid, **kwargs):
        list_merge = []
        page = 1
        while True:
            url = f"{self.BASE_URL}/1/clouddrive/file/sort"
            querystring = {
                "pr": "ucpro",
                "fr": "pc",
                "uc_param_str": "",
                "pdir_fid": pdir_fid,
                "_page": page,
                "_size": "50",
                "_fetch_total": "1",
                "_fetch_sub_dirs": "0",
                "_sort": "file_type:asc,updated_at:desc",
                "_fetch_full_path": kwargs.get("fetch_full_path", 0),
                "fetch_all_file": 1,  # 跟随Web端，作用未知
                "fetch_risk_file_name": 1,  # 如无此参数，违规文件名会被变 ***
            }
            response = self._send_request("GET", url, params=querystring).json()
            if response["code"] != 0:
                return response
            if response["data"]["list"]:
                list_merge += response["data"]["list"]
                page += 1
            else:
                break
            if len(list_merge) >= response["metadata"]["_total"]:
                break
        response["data"]["list"] = list_merge
        return response

    def save_file(self, fid_list, fid_token_list, to_pdir_fid, pwd_id, stoken):
        url = f"{self.BASE_URL}/1/clouddrive/share/sharepage/save"
        querystring = {
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
            "app": "clouddrive",
            "__dt": int(random.uniform(1, 5) * 60 * 1000),
            "__t": datetime.now().timestamp(),
        }
        payload = {
            "fid_list": fid_list,
            "fid_token_list": fid_token_list,
            "to_pdir_fid": to_pdir_fid,
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": "0",
            "scene": "link",
        }
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

    def query_task(self, task_id):
        retry_index = 0
        while True:
            url = f"{self.BASE_URL}/1/clouddrive/task"
            querystring = {
                "pr": "ucpro",
                "fr": "pc",
                "uc_param_str": "",
                "task_id": task_id,
                "retry_index": retry_index,
                "__dt": int(random.uniform(1, 5) * 60 * 1000),
                "__t": datetime.now().timestamp(),
            }
            response = self._send_request("GET", url, params=querystring).json()
            if response["status"] != 200:
                return response
            if response["data"]["status"] == 2:
                if retry_index > 0:
                    print()
                break
            else:
                if retry_index == 0:
                    print(
                        f"正在等待[{response['data']['task_title']}]执行结果",
                        end="",
                        flush=True,
                    )
                else:
                    print(".", end="", flush=True)
                retry_index += 1
                time.sleep(0.500)
        return response

    def download(self, fids):
        url = f"{self.BASE_URL}/1/clouddrive/file/download"
        querystring = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        payload = {"fids": fids}
        response = self._send_request("POST", url, json=payload, params=querystring)
        set_cookie = response.cookies.get_dict()
        cookie_str = "; ".join([f"{key}={value}" for key, value in set_cookie.items()])
        return response.json(), cookie_str

    def mkdir(self, dir_path):
        url = f"{self.BASE_URL}/1/clouddrive/file"
        querystring = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        payload = {
            "pdir_fid": "0",
            "file_name": "",
            "dir_path": dir_path,
            "dir_init_lock": False,
        }
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

    def rename(self, fid, file_name):
        url = f"{self.BASE_URL}/1/clouddrive/file/rename"
        querystring = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        payload = {"fid": fid, "file_name": file_name}
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

    def delete(self, filelist):
        url = f"{self.BASE_URL}/1/clouddrive/file/delete"
        querystring = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        payload = {"action_type": 2, "filelist": filelist, "exclude_fids": []}
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

    def recycle_list(self, page=1, size=30):
        url = f"{self.BASE_URL}/1/clouddrive/file/recycle/list"
        querystring = {
            "_page": page,
            "_size": size,
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
        }
        response = self._send_request("GET", url, params=querystring).json()
        return response["data"]["list"]

    def recycle_remove(self, record_list):
        url = f"{self.BASE_URL}/1/clouddrive/file/recycle/remove"
        querystring = {"uc_param_str": "", "fr": "pc", "pr": "ucpro"}
        payload = {
            "select_mode": 2,
            "record_list": record_list,
        }
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

    def unarchive(self, fid, to_pdir_fid):
        url = f"{self.BASE_URL}/1/clouddrive/archive/unarchive"
        querystring = {"uc_param_str": "", "fr": "pc", "pr": "ucpro"}
        payload = {
            "fid": fid,
            "to_pdir_fid": to_pdir_fid,
            "conflict_mode": 3,
            "suffix_type": 0,
            "pwd": "",
            "select_mode": 0,
        }
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

    def move_files(self, fids, to_pdir_fid):
        url = f"{self.BASE_URL}/1/clouddrive/file/move"
        querystring = {"uc_param_str": "", "fr": "pc", "pr": "ucpro"}
        payload = {
            "filelist": fids,
            "to_pdir_fid": to_pdir_fid,
            "exclude_fids": [],
            "action_type": 1,
        }
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

    # ↑ 请求函数
    # ↓ 操作函数

    def extract_url(self, url):
        # pwd_id
        match_id = re.search(r"/s/(\w+)", url)
        pwd_id = match_id.group(1) if match_id else None
        # passcode
        match_pwd = re.search(r"pwd=(\w+)", url)
        passcode = match_pwd.group(1) if match_pwd else ""
        # path: fid-name
        # Legacy 20250905
        paths = []
        matches = re.findall(r"/(\w{32})-?([^/]+)?", url)
        for match in matches:
            fid = match[0]
            name = urllib.parse.unquote(match[1]).replace("*101", "-")
            paths.append({"fid": fid, "name": name})
        pdir_fid = paths[-1]["fid"] if matches else 0
        return pwd_id, passcode, pdir_fid, paths

    def update_savepath_fid(self, tasklist):
        dir_paths = [
            re.sub(r"/{2,}", "/", f"/{item['savepath']}")
            for item in tasklist
            if not item.get("enddate")
            or (
                datetime.now().date()
                <= datetime.strptime(item["enddate"], "%Y-%m-%d").date()
            )
        ]
        if not dir_paths:
            return False
        dir_paths_exist_arr = self.get_fids(dir_paths)
        dir_paths_exist = [item["file_path"] for item in dir_paths_exist_arr]
        # 比较创建不存在的
        dir_paths_unexist = list(set(dir_paths) - set(dir_paths_exist) - set(["/"]))
        for dir_path in dir_paths_unexist:
            mkdir_return = self.mkdir(dir_path)
            if mkdir_return["code"] == 0:
                new_dir = mkdir_return["data"]
                dir_paths_exist_arr.append(
                    {"file_path": dir_path, "fid": new_dir["fid"]}
                )
                print(f"创建文件夹：{dir_path}")
            else:
                print(f"创建文件夹：{dir_path} 失败, {mkdir_return['message']}")
        # 储存目标目录的fid
        for dir_path in dir_paths_exist_arr:
            self.savepath_fid[dir_path["file_path"]] = dir_path["fid"]
        # print(dir_paths_exist_arr)

    def do_save_check(self, shareurl, savepath):
        try:
            pwd_id, passcode, pdir_fid, _ = self.extract_url(shareurl)
            stoken = self.get_stoken(pwd_id, passcode)["data"]["stoken"]
            share_file_list = self.get_detail(pwd_id, stoken, pdir_fid)["data"]["list"]
            share_file_list = [item for item in share_file_list if is_media_file(item)]
            if not any(is_video_file(item) for item in share_file_list):
                print("转存测试已跳过：当前目录没有可转存的视频")
                return False
            fid_list = [item["fid"] for item in share_file_list]
            fid_token_list = [item["share_fid_token"] for item in share_file_list]
            get_fids = self.get_fids([savepath])
            to_pdir_fid = (
                get_fids[0]["fid"] if get_fids else self.mkdir(savepath)["data"]["fid"]
            )
            save_file = self.save_file(
                fid_list, fid_token_list, to_pdir_fid, pwd_id, stoken
            )
            print(f"转存文件: {save_file}")
            if save_file["code"] == 0:
                task_id = save_file["data"]["task_id"]
                query_task = self.query_task(task_id)
                print(f"查询转存: {query_task}")
                if query_task["code"] == 0:
                    del_list = query_task["data"]["save_as"]["save_as_top_fids"]
                    if del_list:
                        delete_return = self.delete(del_list)
                        print(f"删除转存: {delete_return}")
                        recycle_list = self.recycle_list()
                        record_id_list = [
                            item["record_id"]
                            for item in recycle_list
                            if item["fid"] in del_list
                        ]
                        recycle_remove = self.recycle_remove(record_id_list)
                        print(f"清理转存: {recycle_remove}")
                        print(f"✅ 转存测试成功")
                        return True
            print(f"❌ 转存测试失败: 中断")
            return False
        except Exception as e:
            print(f"❌ 转存测试失败: {str(e)}")
            traceback.print_exc()

    def verify_resource_link(self, task, shareurl):
        from app.sdk.common import normalize_share_url

        normalized = normalize_share_url(shareurl) if isinstance(shareurl, str) and len(shareurl) <= 4096 else ""
        if not normalized or not task.get("taskname", "").strip():
            return {"success": False, "candidate": None, "code": "invalid_link",
                    "error": "请填写有效的夸克分享链接和作品名称"}
        aliases = task.get("search_aliases", [])
        aliases = [value.strip() for value in aliases if isinstance(value, str) and value.strip() and len(value) <= 200][:8] if isinstance(aliases, list) else []
        return self._find_verified_resource(
            task, [{"taskname": task["taskname"], "shareurl": normalized}], set(), aliases,
            max_checked=1, verify_index_title=False,
        )

    def find_valid_resource(self, task, config, required_episodes=None, exclude_candidates=None):
        from resource_search import search_resources, search_web_resources, search_error_reason

        title = task.get("taskname", "").strip()
        if not title:
            return {"success": False, "candidate": None, "code": "no_results", "error": "缺少资源名称"}
        aliases = task.get("search_aliases", [])
        aliases = list(dict.fromkeys(alias.strip() for alias in aliases
                                    if isinstance(alias, str) and alias.strip() and len(alias) <= 200))[:8] if isinstance(aliases, list) else []
        queries = _resource_search_queries(title, aliases)
        old_id = self.extract_url(task.get("shareurl", ""))[0]
        seen = {old_id} | {
            entry.get("from_id") for entry in task.get("repair_history", [])
            if isinstance(entry, dict)
        }
        visited = set(exclude_candidates or ())
        history = [item for item in Config.resource_history(self.resource_history_path)
                   if resource_titles_match(title, item.get("taskname", ""), allow_missing_year=True, aliases=aliases)]
        history_result = None
        if history:
            # 已保存的同名同季作品别名用于复验，避免目录服务暂时丢失别名时误拒旧分享。
            for item in history:
                names = item.get("search_aliases", [])
                if isinstance(names, list):
                    aliases.extend(value.strip() for value in names
                                   if isinstance(value, str) and value.strip() and len(value) <= 200)
            aliases = list(dict.fromkeys(aliases))[:8]
            task = dict(task, search_aliases=aliases)
            queries = _resource_search_queries(title, aliases)
            history_result = self._find_verified_resource(
                task, history, seen, aliases, visited=visited, required_episodes=required_episodes,
            )
            if history_result["success"]:
                print("已从本地资源记录重新验证到可用分享")
                return history_result
        attempted, source_errors = [], []
        failed_sources = set()
        index_deadline = time.monotonic() + 60
        interrupted = False

        def record_error(source, message):
            source = source if isinstance(source, str) and re.fullmatch(r"[\w -]{1,40}", source) else "搜索源"
            error = {"source": source, "message": search_error_reason(message)}
            if error not in source_errors:
                source_errors.append(error)

        def search_candidates(words, web=False):
            nonlocal interrupted
            had_error, had_results = False, False
            consecutive_errors = 0
            deferred = []
            for query in words:
                if not web and time.monotonic() >= index_deadline:
                    interrupted = True
                    break
                attempted.append(("web" if web else "index", query))
                try:
                    # Reuse index caches; share validity is checked live below.
                    result = search_web_resources(query, config, refresh=False) if web else search_resources(
                        query, config, refresh=False, include_web=False, failed_sources=failed_sources
                    )
                except (requests.RequestException, ValueError, TypeError) as exc:
                    record_error("网页搜索" if web else "索引搜索", type(exc).__name__)
                    had_error = True
                    consecutive_errors += 1
                    if not web and consecutive_errors == 2:
                        interrupted = True
                        break
                    continue
                errors = result.get("errors") or []
                for error in errors:
                    if isinstance(error, dict):
                        record_error(error.get("source"), error.get("message"))
                if not result.get("success"):
                    had_error = True
                    if not errors:
                        record_error("网页搜索" if web else "索引搜索", result.get("message", ""))
                    consecutive_errors += 1
                    if not web and consecutive_errors == 2:
                        interrupted = True
                        break
                    continue
                consecutive_errors = 0
                entries = result.get("data", [])
                if not entries and errors:
                    had_error = True
                had_results = had_results or bool(entries)
                checked_before = len(visited)
                for index, candidate in enumerate(entries[:50]):
                    # 已返回的结果继续验证；截止线只阻止发起下一轮查询。
                    yield candidate
                    if required_episodes and len(visited) - checked_before >= 2:
                        deferred.extend(entries[index + 1:50])
                        break  # 先给其他译名机会，再用剩余名额验证本次余下候选。
            yield from deferred
            if had_error and not had_results:
                raise requests.RequestException("搜索源不可用，请检查搜索配置")

        title_queries = _resource_search_queries(title, [])
        web_queries = title_queries[:1]
        # Some web indexes distinguish 第3季 from 第三季 even for the same work.
        if re.search(r"[\u4e00-\u9fff]", _resource_title_parts(title)[0]):
            chinese_season = next((word for word in title_queries[1:]
                                   if re.search(r"第[零一二三四五六七八九十]+季", word)), None)
            if chinese_season:
                web_queries.append(chinese_season)
        for alias in aliases:
            if re.search(r"[\u4e00-\u9fff]", alias) and not re.search(r"[\u3040-\u30ff]", alias):
                words = _resource_search_queries(alias, [])
                if words and words[0] not in web_queries:
                    web_queries.append(words[0])
                    break
        web_queries = web_queries[:2]
        if required_episodes:
            episode = min(required_episodes)
            # 先找最早的缺集，再查整季合集；其他缺集仍从同一候选中一并补齐。
            focused = [f"{word} {episode:02d}" for word in web_queries]
            queries = list(dict.fromkeys(focused + web_queries
                                        + [f"{word} 第{episode}集" for word in web_queries] + queries))
            web_queries = list(dict.fromkeys(focused + web_queries))
        failures = [history_result] if history_result else []
        for web, words in ((False, queries), (True, web_queries)):
            try:
                result = self._find_verified_resource(task, search_candidates(words, web), seen, aliases,
                                                      visited=visited, max_checked=(8 if web else 12) if required_episodes else (3 if web else 5),
                                                      verify_index_title=not web, required_episodes=required_episodes)
            except (requests.RequestException, ValueError, TypeError) as exc:
                print(f"资源搜索异常：{type(exc).__name__}")
                result = {"success": False, "candidate": None, "code": "unavailable",
                          "error": "搜索源暂不可用，请检查搜索配置或稍后重试"}
            if result["success"]:
                return result
            failures.append(result)
        result = next((result for result in failures if result["code"] == "no_match"),
                      next((result for result in failures if result["code"] == "unavailable"), failures[0]))
        result["queries"] = list(dict.fromkeys(query for _, query in attempted))
        result["source_errors"] = source_errors
        result["missing_in_candidates"] = sum(item.get("missing_in_candidates", 0) for item in failures)
        result["search_complete"] = not (source_errors or interrupted or any(item.get("validation_limited") for item in failures))
        if source_errors or interrupted:
            summary = "；".join(f"{error['source']}：{error['message']}" for error in source_errors[:3])
            prefix = f"部分来源查询失败（{summary}）" if summary else "索引查询已达到时间预算，部分关键词尚未查询"
            result["error"] = prefix + "；已查询的来源未找到可验证的匹配资源，可稍后重试或手动选择链接"
            if result["code"] == "no_results":
                result["code"] = "unavailable"
        return result

    def _find_verified_resource(self, task, candidates, seen, aliases, visited=None,
                                max_checked=5, verify_index_title=True, required_episodes=None):
        title = task["taskname"]
        checked, found_results, missing_in_candidates = 0, False, 0
        limited = False
        visited = visited if visited is not None else set()
        name, seasons, years = _resource_title_parts(title)
        names = {name} | {_resource_title_parts(alias)[0] for alias in aliases}
        names |= {first + second for first in names for second in names if first != second}

        def listing_is_complete(detail):
            nonlocal limited
            complete = share_listing_complete(detail)
            if not complete:
                limited = True
            return complete

        candidates = iter(candidates)
        while checked < max_checked:
            try:
                candidate = next(candidates)
            except StopIteration:
                break
            found_results = True
            if not isinstance(candidate, dict):
                continue
            candidate_title = candidate.get("taskname", "")
            candidate_file = candidate_title if is_video_file({"file_name": candidate_title}) else f"{candidate_title}.mp4"
            if verify_index_title and not resource_titles_match(
                title, candidate_title, allow_missing_year=True, aliases=aliases
            ) and not (required_episodes and isinstance(candidate_title, str)
                       and _resource_episode_number(task, candidate_file) in required_episodes):
                continue
            shareurl = candidate.get("shareurl", "")
            if not isinstance(shareurl, str):
                continue
            try:
                parsed = urllib.parse.urlsplit(shareurl)
            except ValueError:
                continue
            if parsed.scheme != "https" or parsed.netloc != "pan.quark.cn":
                continue
            pwd_id, passcode, pdir_fid, _ = self.extract_url(shareurl)
            identity = (pwd_id, pdir_fid, passcode)
            if not pwd_id or pwd_id in seen or identity in visited:
                continue
            visited.add(identity)
            checked += 1
            try:
                token = self.get_stoken(pwd_id, passcode)
                stoken = (token.get("data") or {}).get("stoken")
                if token.get("status") != 200 or not stoken:
                    continue
                detail = self.get_detail(pwd_id, stoken, pdir_fid, _fetch_share=1, max_pages=3)
                files = (detail.get("data") or {}).get("list", [])
                if detail.get("code") != 0 or not listing_is_complete(detail) or not files:
                    continue
                directory_evidence = []
                share = detail["data"].get("share")
                if isinstance(share, dict) and isinstance(share.get("title"), str):
                    directory_evidence.append(share["title"])
                selected_fid, ambiguous = None, False
                verified_pdir_fid = pdir_fid
                base_title = re.sub(_RESOURCE_SEASON_PATTERN, " ", title, flags=re.I)
                # Follow one verified path; sibling seasons are neither evidence nor a save target.
                for _ in range(3):
                    folders = [item for item in files if item.get("dir")]
                    named_single_folder = len(folders) == 1 and not any(is_video_file(item) for item in files) and resource_titles_match(
                        title, folders[0]["file_name"], allow_missing_year=True, aliases=aliases)
                    if len(files) == 1 and files[0].get("dir"):
                        folder = files[0]
                    elif named_single_folder:
                        folder = folders[0]
                        if not re.fullmatch(r"[A-Za-z0-9_]{32}", str(folder.get("fid", ""))):
                            ambiguous = True
                            break
                        selected_fid = folder["fid"]
                    else:
                        if len(seasons) != 1:
                            break
                        parent_named = any(resource_titles_match(base_title, value, allow_missing_year=True, aliases=aliases)
                                           or resource_titles_match(title, value, allow_missing_year=True, aliases=aliases)
                                           for value in directory_evidence)
                        matching = []
                        for item in files:
                            if not item.get("dir"):
                                continue
                            child_name, child_seasons, child_years = _resource_candidate_parts(title, item["file_name"])
                            if resource_titles_match(title, item["file_name"], allow_missing_year=True, aliases=aliases) or (
                                parent_named and not child_name and child_seasons == seasons
                                and (not years or not child_years or child_years == years)
                            ):
                                matching.append(item)
                        if len(matching) > 1:
                            ambiguous = True
                            break
                        if not matching:
                            break
                        folder = matching[0]
                        if not re.fullmatch(r"[A-Za-z0-9_]{32}", str(folder.get("fid", ""))):
                            ambiguous = True
                            break
                        selected_fid = folder["fid"]
                    directory_evidence.append(folder["file_name"])
                    verified_pdir_fid = folder["fid"]
                    detail = self.get_detail(pwd_id, stoken, folder["fid"], max_pages=3)
                    files = (detail.get("data") or {}).get("list", [])
                    if detail.get("code") != 0 or not listing_is_complete(detail) or not files:
                        break
                else:
                    if any(item.get("dir") for item in files) and not any(is_video_file(item) for item in files):
                        limited = True
                if ambiguous or detail.get("code") != 0 or not listing_is_complete(detail) or not files:
                    continue
                video_files = [item for item in files if is_video_file(item)]
                if not video_files:
                    continue
                evidence = directory_evidence + [item.get("file_name", "") for item in files
                                                  if item.get("dir") or is_video_file(item)]
                video_names = {item["file_name"] for item in video_files}
                evidence = [_resource_video_title(value) if value in video_names else value for value in evidence]
                if any(other_name in names | {""} and (
                    (other_seasons and not _resource_seasons_match(seasons, other_seasons, years))
                    or (years and other_years and years != other_years)
                ) for other_name, other_seasons, other_years in (_resource_candidate_parts(title, value) for value in evidence)):
                    continue
                exact = any(
                    resource_titles_match(title, name, aliases=aliases)
                    for name in evidence
                )
                numbered_episodes = [re.fullmatch(
                    r"s(\d{1,2})e\d{1,4}(?:[ ._-]+(?:2160p|1080p|720p|4k))?(?:-[a-z0-9]{1,20})?",
                    re.sub(r"\.[^.]+$", "", item.get("file_name", "")).strip(), re.I,
                ) for item in video_files]
                episode_seasons = {int(match.group(1)) for match in numbered_episodes if match}
                declared_seasons = {int(match.group(1)) for item in video_files
                                    if (match := re.match(r"s(\d{1,2})e\d{1,4}(?=\b|[._-])", item["file_name"], re.I))}
                if declared_seasons and not _resource_seasons_match(seasons, declared_seasons, years):
                    continue
                # A named directory and its plain SxxExx video files describe the same show.
                path_seasons = set().union(*(other_seasons for other_name, other_seasons, _ in
                                            (_resource_candidate_parts(title, value) for value in directory_evidence) if not other_name))
                combined = bool(seasons and (episode_seasons | path_seasons) == seasons and any(
                    resource_titles_match(base_title, value, aliases=aliases)
                    for value in directory_evidence
                ))
                # The actual named video and its enclosing directory can supply title and year separately.
                path_years = set().union(*(_resource_title_parts(value)[2] for value in directory_evidence))
                matched_videos = [
                    resource_titles_match(title, _resource_video_title(item["file_name"]), allow_missing_year=True, aliases=aliases)
                    for item in video_files
                ]
                consistent_videos = all(matched or numbered_episodes[index] or re.fullmatch(
                    r"\d{1,3}", _resource_video_title(video_files[index]["file_name"])
                ) for index, matched in enumerate(matched_videos))
                combined = combined or bool(years and path_years == years and any(matched_videos) and consistent_videos)
                if not exact and not combined:
                    continue
                history_url = (urllib.parse.urlunsplit(parsed._replace(fragment=f"/list/share/{selected_fid}"))
                               if selected_fid else shareurl)
                Config.remember_resources(self.resource_history_path, [{"taskname": title, "shareurl": history_url,
                                                                       "search_aliases": aliases}])
                episode_fids = None
                if required_episodes is not None:
                    episode_fids = _episode_repair_files(task, video_files, required_episodes)
                    if not episode_fids:
                        missing_in_candidates += 1
                        continue
                    pdir_fid = verified_pdir_fid
                if selected_fid:
                    pdir_fid = verified_pdir_fid if required_episodes is not None else selected_fid
                    shareurl = urllib.parse.urlunsplit(parsed._replace(fragment=f"/list/share/{selected_fid}"))
                return {"success": True, "candidate": {
                    **candidate, "shareurl": shareurl, "pwd_id": pwd_id, "passcode": passcode,
                    "pdir_fid": pdir_fid, "stoken": stoken,
                    "search_aliases": aliases,
                    **({"episode_fids": episode_fids} if episode_fids is not None else {}),
                }, "error": ""}
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                print(f"候选分享验证失败：{type(exc).__name__}")
        else:
            limited = True
        return {"success": False, "candidate": None, "code": "no_match" if found_results else "no_results", "validation_limited": limited,
                "missing_in_candidates": missing_in_candidates,
                "error": "候选验证达到数量、分页或目录深度上限，尚未完整确认可用视频" if limited else
                "未找到经夸克视频文件验证的同名、同季、同年份资源，请尝试其他译名或手动选择链接" if found_results
                else "已查询的来源未返回该名称的夸克资源，请尝试其他译名或手动搜索"}

    def repair_share(self, task, reason="分享已失效"):
        if not task.get("auto_repair", CONFIG_DATA.get("auto_repair", True)):
            return None
        if task.get("update_subdir_resave_mode"):
            print("自动换源已跳过：此任务开启了删除子目录后重存，请手工检查新资源")
            return None
        print(f"🔎《{task['taskname']}》{reason}，查找同名同季资源")
        result = self.find_valid_resource(task, CONFIG_DATA)
        if not result["success"]:
            print(f"自动换源未完成：{result['error']}")
            return None
        candidate = result["candidate"]
        old_id = self.extract_url(task["shareurl"])[0]
        task["shareurl"] = candidate["shareurl"]
        task.pop("shareurl_ban", None)
        # The old file boundary belongs to the old share and cannot be reused.
        task.pop("startfid", None)
        task["repair_history"] = (task.get("repair_history", []) + [{
            "time": datetime.now().isoformat(timespec="seconds"),
            "from_id": old_id, "to_id": candidate["pwd_id"],
            "source": candidate.get("source", ""),
        }])[-10:]
        add_notify(f"🔄《{task['taskname']}》已自动换源：{old_id} → {candidate['pwd_id']}；保留转存规则")
        return candidate

    def repair_episode_gaps(self, task, tree):
        if (not task.get("resource_id") or task.get("resource_type") not in ("anime", "tv")
                or not task.get("auto_repair", CONFIG_DATA.get("auto_repair", True))
                or task.get("shareurl_ban") or tree.root is None):
            return
        mr = MagicRename(CONFIG_DATA.get("magic_regex", {}))
        mr.set_taskname(task["taskname"])
        pattern, replace = mr.magic_regex_conv(task.get("pattern", ""), task.get("replace", ""))
        if (task.get("startfid") or task.get("update_subdir_resave_mode")
                or re.search(r"\{I+\}", replace) or _resource_episode_number(task, "01.mkv") is None):
            print("缺集检查已跳过：订阅边界、重新编号或季数无法安全确认")
            return
        try:
            savepath = re.sub(r"/{2,}", "/", f"/{task['savepath']}")
            target_fid = self.savepath_fid.get(savepath)
            if not target_fid:
                return
            response = self.ls_dir(target_fid)
            files = (response.get("data") or {}).get("list")
            if response.get("code") != 0 or not isinstance(files, list):
                print("缺集检查暂不可用：无法读取保存目录，下次运行重试")
                return
            tree_fids = {str(node.data.get("fid")) for node in tree.all_nodes() if node.data}
            for item in files:
                fid = str(item.get("fid"))
                pending = task.get("rename_pending", {}).get(fid, {})
                if (fid not in tree_fids and is_media_file(item)
                        and pending.get("original") == item.get("file_name") and pending.get("path") == savepath
                        and (not is_video_file(item) or re.search(pattern, item["file_name"]))):
                    # 补集文件可能不在主分享中，仅凭已记录的目标文件身份恢复改名和下载。
                    renamed = mr.media_name(pattern, replace, item["file_name"])
                    tree.create_node(self._get_file_icon(item) + renamed, f"episode-repair:pending:{fid}",
                                     parent=tree.root, data={"file_name": item["file_name"], "file_name_re": renamed,
                                                           "fid": fid, "path": f"{savepath}/{item['file_name']}",
                                                           "is_dir": False, "obj_category": item.get("obj_category", "")})
                    tree_fids.add(fid)
            shared_path = savepath == "/cartoon" or sum(
                other.get("savepath", "").rstrip("/") == savepath.rstrip("/")
                for other in CONFIG_DATA.get("tasklist", [])
            ) > 1
            folders = [] if shared_path else [item for item in files if item.get("dir")]
            if len(folders) > 3:
                print("缺集检查已跳过：保存目录子目录较多，请先确认同一季的目录")
                return
            for folder in folders:
                children = self.ls_dir(folder["fid"])
                child_list = (children.get("data") or {}).get("list")
                if children.get("code") != 0 or child_list != [] or (children.get("metadata") or {}).get("_total", 0):
                    print("缺集检查已跳过：子目录非空或读取失败，请先确认同一季的目录")
                    return
            names = [item["file_name"] for item in files if is_video_file(item)]
            if shared_path:
                titles = [_resource_title_parts(task["taskname"])[0]] + [
                    _resource_title_parts(alias)[0] for alias in task.get("search_aliases", []) if isinstance(alias, str)
                ]
                # 共用目录中纯数字文件无法确定作品，仅具名视频参与该订阅的缺集检查。
                names = [name for name in names if any(
                    title and title in re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", name).casefold())
                    for title in titles
                )]
            names += [node.data.get("file_name", "") for node in tree.all_nodes()
                      if node.data and is_video_file(node.data)]
            episodes = {_resource_episode_number(task, filename) for filename in names}
            if None in episodes:
                print("缺集检查已跳过：存在无法可靠识别集号的视频，不推测缺集")
                return
            if len(episodes) < 2:
                return
            missing = set(range(min(episodes), max(episodes) + 1)) - episodes
            if not missing:
                return
            print(f"🔎《{task['taskname']}》检测到中间缺集 {sorted(missing)[:20]}，查找其他同作品同季分享")
            remaining, attempted = set(missing), set()
            deadline = time.monotonic() + 120
            for _ in range(3):
                if not remaining or time.monotonic() >= deadline:
                    break
                result = self.find_valid_resource(
                    task, CONFIG_DATA, required_episodes=set(remaining),
                    exclude_candidates=set(attempted),
                )
                if not result["success"]:
                    print(f"补集未完成：{result['error']}；原订阅和已有文件保留")
                    if result.get("queries"):
                        print("补集已查询关键词：" + "、".join(result["queries"]))
                    if result.get("missing_in_candidates"):
                        print(f"已验证 {result['missing_in_candidates']} 个同作品候选，但其中没有符合转存规则的缺失集视频")
                    break
                candidate = result["candidate"]
                candidate_aliases = candidate.get("search_aliases", [])
                if isinstance(candidate_aliases, list):
                    aliases = task.get("search_aliases", [])
                    aliases = aliases if isinstance(aliases, list) else []
                    aliases = list(dict.fromkeys(value.strip() for value in aliases + candidate_aliases
                                                if isinstance(value, str) and value.strip() and len(value) <= 200))[:8]
                    if aliases:
                        task["search_aliases"] = aliases
                identity = (candidate["pwd_id"], candidate["pdir_fid"], candidate.get("passcode", ""))
                if identity in attempted:
                    break
                attempted.add(identity)
                if candidate.get("shareurl"):
                    pwd_id, passcode, pdir_fid, _ = self.extract_url(candidate["shareurl"])
                    attempted.add((pwd_id, pdir_fid, passcode))
                supplement_task = dict(task)
                try:
                    supplement = self.dir_check_and_save(
                        supplement_task, candidate["pwd_id"], candidate["stoken"], candidate["pdir_fid"],
                        allowed_episode_fids=candidate["episode_fids"],
                    )
                finally:
                    # 备用分享失效不能封禁主订阅；已保存文件的待更名状态仍须持久化。
                    if supplement_task.get("rename_pending"):
                        task.setdefault("rename_pending", {}).update(supplement_task["rename_pending"])
                saved = set()
                for node in supplement.all_nodes():
                    if node.identifier == supplement.root or not node.data or node.data.get("is_dir"):
                        continue
                    episode = _resource_episode_number(task, node.data.get("file_name", ""))
                    if episode in remaining or (is_media_file(node.data) and not is_video_file(node.data)):
                        tree.create_node(node.tag, f"episode-repair:{candidate['pwd_id']}:{node.identifier}",
                                         parent=tree.root, data=node.data)
                        if episode in remaining:
                            saved.add(episode)
                if saved:
                    print(f"补集转存成功：{sorted(saved)}；保留原订阅继续追更")
                remaining -= saved
            if remaining:
                print(f"仍有中间缺集 {sorted(remaining)[:20]}；本轮补搜结束，下次运行继续自动查找")
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            print(f"补集暂未完成（{type(exc).__name__}）；保留原订阅和已有文件")

    def do_save_task(self, task):
        apply_media_rename_defaults(task)
        # Recheck historical bans: older versions also banned temporary login errors.
        repaired = False
        pwd_id, passcode, pdir_fid, _ = self.extract_url(task["shareurl"])
        try:
            token = self.get_stoken(pwd_id, passcode)
        except (requests.RequestException, ValueError) as exc:
            print(f"跳过任务：分享验证暂不可用（{type(exc).__name__}）")
            return
        stoken = (token.get("data") or {}).get("stoken")
        if token.get("status") != 200 or not stoken:
            message = str(token.get("message") or "分享验证失败，请稍后重试")
            if is_expired_share(token):
                if task.get("shareurl_ban") != message:
                    add_notify(f"❌《{task['taskname']}》：{message}\n")
                task["shareurl_ban"] = message
                replacement = self.repair_share(task)
                if not replacement:
                    return
                pwd_id, pdir_fid, stoken = (
                    replacement["pwd_id"], replacement["pdir_fid"], replacement["stoken"]
                )
                repaired = True
            else:
                print(f"跳过任务：{message}；保留原订阅，下次重试")
                return
        else:
            task.pop("shareurl_ban", None)

        scan_state = {}
        updated_tree = self.dir_check_and_save(task, pwd_id, stoken, pdir_fid, scan_state=scan_state)
        no_video = scan_state.get("complete") and not scan_state.get("has_video")
        if not repaired and (no_video or is_expired_share({"message": task.get("shareurl_ban", "")})):
            replacement = self.repair_share(task, reason="分享不含视频") if no_video else self.repair_share(task)
            if replacement:
                updated_tree = self.dir_check_and_save(
                    task, replacement["pwd_id"], replacement["stoken"], replacement["pdir_fid"]
                )
        self.do_rename(updated_tree, task=task)
        self.repair_episode_gaps(task, updated_tree)
        if updated_tree.size(1) > 0:
            self.do_rename(updated_tree, task=task)
            print()
            add_notify(f"✅《{task['taskname']}》添加追更：\n{updated_tree}")
            return updated_tree
        else:
            print(f"任务结束：没有新的转存任务")
            return False

    def _scan_share_media(self, task, pwd_id, stoken, pdir_fid, subdir_path=""):
        state = {"complete": False, "has_video": False, "directories": []}
        visited, count = set(), 0

        def visit(fid, path, depth=0, include=True):
            nonlocal count
            if depth > 8 or len(visited) >= 100 or str(fid) in visited:
                raise ValueError("目录检查达到限制")
            visited.add(str(fid))
            response = self.get_detail(pwd_id, stoken, fid, max_pages=20)
            if response.get("code") != 0:
                if is_expired_share(response):
                    task["shareurl_ban"] = response["message"]
                raise ValueError("分享目录读取失败")
            data = response.get("data") or {}
            files = data.get("list")
            if not share_listing_complete(response):
                raise ValueError("分享目录未完整读取")
            count += len(files)
            if count > 10000:
                raise ValueError("文件检查达到限制")
            # 延续单层包装目录直接展开到任务保存路径的行为。
            if depth == 0 and not path and len(files) == 1 and files[0].get("dir"):
                visit(files[0]["fid"], path, depth + 1, include)
                return
            state["has_video"] |= any(is_video_file(item) for item in files)
            if include:
                state["directories"].append((fid, path, files))
            for item in files:
                if item.get("dir"):
                    name = item["file_name"]
                    if name in (".", "..") or re.search(r"[\\/\x00-\x1f]", name):
                        raise ValueError("目录名称不安全")
                    folder_pattern = task.get("update_subdir") or ".*"
                    if folder_pattern in (MEDIA_PATTERN, task.get("pattern")):
                        folder_pattern = ".*"
                    selected = include and bool(re.search(folder_pattern, name))
                    visit(item["fid"], path + "/" + name, depth + 1, selected)

        try:
            visit(pdir_fid, subdir_path)
            state["complete"] = True
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            print(f"媒体检查未完成（{type(exc).__name__}）；本次不转存，也不据此判断分享无视频")
        return state

    def dir_check_and_save(self, task, pwd_id, stoken, pdir_fid="", subdir_path="", allowed_episode_fids=None, scan_state=None):
        tree = Tree()
        state = self._scan_share_media(task, pwd_id, stoken, pdir_fid, subdir_path)
        if scan_state is not None:
            scan_state.update(state)
        if not state["complete"]:
            return tree
        if not state["has_video"]:
            print("分享内没有视频文件，跳过其中的字幕和其他文件")
            return tree
        rootpath = re.sub(r"/{2,}", "/", f"/{task['savepath']}")
        tree.create_node(rootpath, pdir_fid, data={"is_dir": True})
        mr = MagicRename(CONFIG_DATA.get("magic_regex", {}))
        mr.set_taskname(task["taskname"])
        pattern, replace = mr.magic_regex_conv(task.get("pattern", ""), task.get("replace", ""))
        if task.get("update_subdir_resave_mode"):
            print("媒体白名单模式按文件检查子目录，不整目录删除重存")
        planned_names = set()
        shared_path = rootpath == "/cartoon" or sum(
            other.get("savepath", "").rstrip("/") == rootpath.rstrip("/")
            for other in CONFIG_DATA.get("tasklist", [])
        ) > 1

        for source_fid, path, source_files in state["directories"]:
            verified = (_episode_repair_files(task, source_files, set(allowed_episode_fids.values()))
                        if allowed_episode_fids is not None else None)
            eligible = []
            for item in source_files:
                if is_media_file(item):
                    video = is_video_file(item)
                    allowed = not video or re.search(pattern, item["file_name"])
                    if video and verified is not None:
                        allowed = item["fid"] in verified and verified[item["fid"]] == allowed_episode_fids.get(item["fid"])
                    if allowed:
                        eligible.append(deepcopy(item))
                if item.get("fid") == task.get("startfid", ""):
                    break
            if not eligible:
                continue
            savepath = rootpath
            if savepath not in self.savepath_fid:
                existing = self.get_fids([savepath])
                if existing:
                    self.savepath_fid[savepath] = existing[0]["fid"]
                else:
                    created = self.mkdir(savepath)
                    if created.get("code") != 0:
                        print(f"无法创建媒体目录：{savepath}；下次重试")
                        continue
                    self.savepath_fid[savepath] = created["data"]["fid"]
            destination_fid = self.savepath_fid[savepath]
            listing = self.ls_dir(destination_fid)
            if listing.get("code") != 0:
                print(f"无法读取保存目录：{savepath}；下次重试")
                continue
            existing_files = listing["data"]["list"]
            names = [item["file_name"] for item in existing_files]
            video_names = [item["file_name"] for item in existing_files if is_video_file(item)]
            for item in eligible:
                item["file_name_re"] = mr.media_name(pattern, replace, item["file_name"])
            if re.search(r"\{I+\}", replace):
                mr.set_dir_file_list(existing_files, replace)
                mr.sort_file_list(eligible)

            def add_saved(item, fid):
                if tree.contains(str(fid)):
                    return
                if item["file_name"] != item["file_name_re"]:
                    task.setdefault("rename_pending", {})[str(fid)] = {
                        "original": item["file_name"], "renamed": item["file_name_re"], "path": savepath}
                tree.create_node(self._get_file_icon(item) + item["file_name_re"], str(fid), parent=pdir_fid,
                                 data={"file_name": item["file_name"], "file_name_re": item["file_name_re"],
                                       "fid": str(fid), "path": f"{savepath}/{item['file_name']}",
                                       "is_dir": False, "obj_category": item.get("obj_category", "")})

            pending = []
            reserved = set(names) | planned_names
            for item in eligible:
                original, renamed = item["file_name"], item["file_name_re"]
                ignore_extension = bool(task.get("ignore_extension") and is_video_file(item))
                comparable_names = video_names if ignore_extension else names
                # 字幕必须按完整文件名判断，不能被同名视频的不同后缀吞掉。
                present = next((f for f in existing_files if not f.get("dir") and f["file_name"] == original), None)
                if present and renamed not in reserved:
                    recorded = task.get("rename_pending", {}).get(str(present["fid"]), {})
                    owned = (not shared_path or
                             (item.get("md5") and item["md5"] == present.get("md5")) or
                             (recorded.get("original") == original and recorded.get("path") == savepath))
                    if not owned:
                        print(f"保存目录已有同名文件 {original}，无法确认属于《{task['taskname']}》；跳过，避免改名或覆盖其他作品")
                        continue
                if present and renamed != original and renamed not in planned_names and not mr.is_exists(renamed, comparable_names, ignore_extension):
                    add_saved(item, present["fid"])
                    reserved.add(renamed)
                    planned_names.add(renamed)
                elif mr.is_exists(original, comparable_names, ignore_extension) or renamed in reserved or mr.is_exists(renamed, comparable_names, ignore_extension):
                    continue
                else:
                    pending.append(item)
                    reserved.add(renamed)
                    planned_names.add(renamed)
            for offset in range(0, len(pending), 100):
                batch = pending[offset:offset + 100]
                response = self.save_file([item["fid"] for item in batch], [item["share_fid_token"] for item in batch],
                                          destination_fid, pwd_id, stoken)
                if response.get("code") == 0:
                    response = self.query_task(response["data"]["task_id"])
                    saved = ((response.get("data") or {}).get("save_as") or {}).get("save_as_top_fids", [])
                    if response.get("code") == 0 and len(saved) == len(batch):
                        for item, fid in zip(batch, saved):
                            add_saved(item, fid)
                        continue
                add_notify(f"❌《{task['taskname']}》媒体转存失败或结果不完整，下次重试")
        return tree

    def do_rename(self, tree, node_id=None, task=None):
        for node in tree.all_nodes():
            file = node.data or {}
            if file.get("is_dir", True) or not file.get("file_name_re"):
                continue
            original, renamed = file["file_name"], file["file_name_re"]
            if original != renamed:
                if file.get("renamed") or file.get("rename_failed"):
                    continue
                try:
                    response = self.rename(file["fid"], renamed)
                except (requests.RequestException, ValueError):
                    response = {"code": 1}
                if response.get("code") != 0:
                    file["rename_failed"] = True
                    print(f"重命名失败：{original} → {renamed}；保留原文件，下次运行重试")
                    continue
                print(f"重命名：{original} → {renamed}")
                file["renamed"] = True
            file["path"] = file["path"].rsplit("/", 1)[0] + "/" + renamed
            node.tag = self._get_file_icon({"obj_category": file.get("obj_category")}) + renamed
            if task is not None:
                task.get("rename_pending", {}).pop(str(file["fid"]), None)
                if not task.get("rename_pending"):
                    task.pop("rename_pending", None)

    def _get_file_icon(self, f):
        if f.get("dir"):
            return "📁"
        ico_maps = {
            "video": "🎞️",
            "image": "🖼️",
            "audio": "🎵",
            "doc": "📄",
            "archive": "📦",
            "default": "",
        }
        return ico_maps.get(f.get("obj_category"), "")


def verify_account(account):
    # 验证账号
    print(f"▶️ 验证第{account.index}个账号")
    if "__uid" not in account.cookie:
        print(f"💡 不存在cookie必要参数，判断为仅签到")
        return False
    else:
        account_info = account.init()
        if not account_info:
            add_notify(f"👤 第{account.index}个账号登录失败，cookie无效❌")
            return False
        else:
            print(f"👤 账号昵称: {account_info['nickname']}✅")
            return True


def format_bytes(size_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = 0
    while size_bytes >= 1024 and i < len(units) - 1:
        size_bytes /= 1024
        i += 1
    return f"{size_bytes:.2f} {units[i]}"


def do_sign(account):
    if not account.mparam:
        print("⏭️ 移动端参数未设置，跳过签到")
        print()
        return
    # 每日领空间
    growth_info = account.get_growth_info()
    if growth_info:
        VIP_MAP = {
            "NORMAL": "普通用户",
            "EXP_SVIP": "88VIP",
            "SUPER_VIP": "SVIP",
            "Z_VIP": "SVIP+",
        }
        growth_message = f"💾 {VIP_MAP.get(growth_info['member_type'], growth_info['member_type'])} 总空间：{format_bytes(growth_info['total_capacity'])}，签到累计获得：{format_bytes(growth_info['cap_composition'].get('sign_reward', 0))}"
        if growth_info["cap_sign"]["sign_daily"]:
            sign_message = f"📅 签到记录: 今日已签到+{int(growth_info['cap_sign']['sign_daily_reward']/1024/1024)}MB，连签进度({growth_info['cap_sign']['sign_progress']}/{growth_info['cap_sign']['sign_target']})✅"
            message = f"{sign_message}\n{growth_message}"
            print(message)
        else:
            sign, sign_return = account.get_growth_sign()
            if sign:
                sign_message = f"📅 执行签到: 今日签到+{int(sign_return/1024/1024)}MB，连签进度({growth_info['cap_sign']['sign_progress']+1}/{growth_info['cap_sign']['sign_target']})✅"
                message = f"{sign_message}\n{growth_message}"
                if (
                    str(
                        CONFIG_DATA.get("push_config", {}).get("QUARK_SIGN_NOTIFY")
                    ).lower()
                    == "false"
                    or os.environ.get("QUARK_SIGN_NOTIFY") == "false"
                ):
                    print(message)
                else:
                    message = message.replace("今日", f"[{account.nickname}]今日")
                    add_notify(message)
            else:
                print(f"📅 签到异常: {sign_return}")
    else:
        print("⏭️ 签到进度读取异常，可能登录失效，跳过签到")
    print()


def do_save(account, tasklist=[]):
    print(f"🧩 载入插件")
    plugins, CONFIG_DATA["plugins"], task_plugins_config = Config.load_plugins(
        CONFIG_DATA.get("plugins", {})
    )
    print()
    print(f"转存账号: {account.nickname}")
    # 获取全部保存目录fid
    for task in tasklist:
        apply_media_rename_defaults(task)
    account.update_savepath_fid(tasklist)

    def is_time(task):
        return (
            not task.get("enddate")
            or (
                datetime.now().date()
                <= datetime.strptime(task["enddate"], "%Y-%m-%d").date()
            )
        ) and (
            "runweek" not in task
            # 星期一为0，星期日为6
            or (datetime.today().weekday() + 1 in task.get("runweek"))
        )

    for plugin_name, plugin in plugins.items():
        if plugin.is_active and hasattr(plugin, "task_before"):
            tasklist = (
                plugin.task_before(tasklist=tasklist, account=account) or tasklist
            )

    # 执行任务
    for index, task in enumerate(tasklist):
        print()
        print(f"#{index+1}------------------")
        print(f"任务名称: {task['taskname']}")
        print(f"分享链接: {task['shareurl']}")
        print(f"保存路径: {task['savepath']}")
        if task.get("pattern"):
            print(f"正则匹配: {task['pattern']}")
        if task.get("replace"):
            print(f"正则替换: {task['replace']}")
        if task.get("update_subdir"):
            print(f"更子目录: {task['update_subdir']}")
        if task.get("runweek") or task.get("enddate"):
            print(
                f"运行周期: WK{task.get('runweek',[])} ~ {task.get('enddate','forever')}"
            )
        print()
        # 判断任务周期
        if not is_time(task):
            print(f"任务不在运行周期内，跳过")
        else:
            is_new_tree = account.do_save_task(task)

            # 补充任务的插件配置
            def merge_dicts(a, b):
                result = a.copy()
                for key, value in b.items():
                    if (
                        key in result
                        and isinstance(result[key], dict)
                        and isinstance(value, dict)
                    ):
                        result[key] = merge_dicts(result[key], value)
                    elif key not in result:
                        result[key] = value
                return result

            task["addition"] = merge_dicts(
                task.get("addition", {}), task_plugins_config
            )
            # 调用插件
            aria2_config = task["addition"].get("aria2", {})
            retry_download = aria2_config.get("auto_download") and (
                aria2_config.get("download_existing") or task.get("aria2_pending") or task.get("aria2_pending_dirs")
            )
            if is_new_tree or retry_download:
                print(f"🧩 调用插件")
                if aria2_config.get("auto_download") and "aria2" not in plugins:
                    print("Aria2下载: 插件未加载，请检查全局 Aria2 配置和 PLUGIN_FLAGS；下次运行将补交")
                    aria2_config["download_existing"] = True
                for plugin_name, plugin in plugins.items():
                    if hasattr(plugin, "run") and (
                        plugin_name == "aria2" or (is_new_tree and plugin.is_active)
                    ):
                        options = {}
                        if plugin_name == "aria2":
                            options["shared_path"] = task["savepath"].rstrip("/") == "/cartoon" or sum(
                                other.get("savepath", "").rstrip("/") == task["savepath"].rstrip("/")
                                for other in CONFIG_DATA.get("tasklist", tasklist)
                            ) > 1
                        task = (
                            plugin.run(task, account=account, tree=is_new_tree, **options) or task
                        )
    print()
    print(f"===============插件收尾===============")
    for plugin_name, plugin in plugins.items():
        if plugin.is_active and hasattr(plugin, "task_after"):
            data = plugin.task_after(tasklist=tasklist, account=account)
            if data.get("tasklist"):
                CONFIG_DATA["tasklist"] = data["tasklist"]
            if data.get("config"):
                CONFIG_DATA["plugins"][plugin_name] = data["config"]
    print()


def main():
    global CONFIG_DATA
    start_time = datetime.now()
    print(f"===============程序开始===============")
    print(f"⏰ 执行时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    # 读取启动参数
    config_path = sys.argv[1] if len(sys.argv) > 1 else "quark_config.json"
    # 推送测试
    if os.environ.get("QUARK_TEST", "").lower() == "true":
        print(f"===============通知测试===============")
        CONFIG_DATA["push_config"] = json.loads(os.environ.get("PUSH_CONFIG"))
        send_ql_notify(
            "【夸克自动转存】",
            f"通知测试\n\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        )
        print()
        if cookies := json.loads(os.environ.get("COOKIE", "[]")):
            print(f"===============转存测试===============")
            accounts = Quark(cookies[0])
            accounts.do_save_check("https://pan.quark.cn/s/1ed94d530d63", "/来自：分享")
            print()
        return
    # 从环境变量中获取 TASKLIST
    tasklist_from_env = []
    if tasklist_json := os.environ.get("TASKLIST"):
        try:
            tasklist_from_env = json.loads(tasklist_json)
        except Exception as e:
            print(f"从环境变量解析任务列表失败 {e}")
    # 检查本地文件是否存在，如果不存在就下载
    if not os.path.exists(config_path):
        if os.environ.get("QUARK_COOKIE"):
            print(
                f"⚙️ 读取到 QUARK_COOKIE 环境变量，仅签到领空间。如需执行转存，请删除该环境变量后配置 {config_path} 文件"
            )
            cookie_val = os.environ.get("QUARK_COOKIE")
            cookie_form_file = False
        else:
            print(f"⚙️ 配置文件 {config_path} 不存在❌，正远程从下载配置模版")
            config_url = f"{GH_PROXY}https://raw.githubusercontent.com/Cp0204/quark_auto_save/main/quark_config.json"
            if Config.download_file(config_url, config_path):
                print("⚙️ 配置模版下载成功✅，请到程序目录中手动配置")
            return
    else:
        print(f"⚙️ 正从 {config_path} 文件中读取配置")
        CONFIG_DATA = Config.read_json(config_path)
        original_config = deepcopy(CONFIG_DATA)
        Config.breaking_change_update(CONFIG_DATA)
        cookie_val = CONFIG_DATA.get("cookie")
        cookie_form_file = True
    # 获取cookie
    cookies = Config.get_cookies(cookie_val)
    if not cookies:
        print("❌ cookie 未配置")
        return
    accounts = [Quark(cookie, index) for index, cookie in enumerate(cookies)]
    if cookie_form_file:
        history_path = Config.resource_history_path(config_path)
        Config.remember_resources(history_path, CONFIG_DATA.get("tasklist", []))
        for account in accounts:
            account.resource_history_path = history_path
    # 签到
    print(f"===============签到任务===============")
    if tasklist_from_env:
        verify_account(accounts[0])
    else:
        for account in accounts:
            verify_account(account)
            do_sign(account)
    print()
    # 转存
    if accounts[0].is_active and cookie_form_file:
        print(f"===============转存任务===============")
        # 任务列表
        if tasklist_from_env:
            originals = deepcopy(tasklist_from_env)
            do_save(accounts[0], tasklist_from_env)
            for original, updated in zip(originals, tasklist_from_env):
                Config.merge_task_state(CONFIG_DATA, original, updated)
        else:
            do_save(accounts[0], CONFIG_DATA.get("tasklist", []))
        print()
    # 通知
    if NOTIFYS:
        notify_body = "\n".join(NOTIFYS)
        print(f"===============推送通知===============")
        send_ql_notify("【夸克自动转存】", notify_body)
        print()
    if cookie_form_file:
        # 网络任务执行期间用户可能新增或修改订阅，仅合并未发生冲突的运行状态。
        with Config.locked(config_path):
            latest = Config.read_json(config_path)
            Config.merge_runtime_state(latest, original_config, CONFIG_DATA)
            Config.write_json(config_path, latest)
            CONFIG_DATA = latest

    print(f"===============程序结束===============")
    duration = datetime.now() - start_time
    print(f"😃 运行时长: {round(duration.total_seconds(), 2)}s")
    print()


if __name__ == "__main__":
    main()
