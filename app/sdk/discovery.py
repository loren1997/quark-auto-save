"""Public metadata for the weekly discovery panel; no share links are invented."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from threading import Event, Lock, Thread
from time import monotonic
import logging
import re
import unicodedata
from urllib.parse import urlsplit

import requests


CHINA_TZ = timezone(timedelta(hours=8))
USER_AGENT = "quark-auto-save/1.0 (+https://github.com/Cp0204/quark-auto-save)"
TV_LIMIT = 50
DONGHUA_LIMIT = 60
BILIBILI_CALENDAR = "https://api.bilibili.com/pgc/web/timeline/v2?season_type=4&day_before=6&day_after=6"
BILIBILI_SEASON = "https://api.bilibili.com/pgc/view/web/season?season_id="
_cache = None
_expires = 0
_lock = Lock()
_aliases = {}
_bilibili_media = {}
_source_data = {}
_snapshot_week = None
_preview = None
_refreshing = False
_refresh_thread = None
_refresh_done = Event()
_refresh_done.set()
_requested_week = None
_retry_at = 0


def _request_json(url, deadline=None):
    for attempt in range(2 if deadline is not None else 1):
        timeout = (2, 4)
        if deadline is not None:
            remaining = deadline - monotonic()
            if remaining <= 0.2:
                raise requests.Timeout("别名查询已达本轮时限")
            connect = min(2, remaining / 3)
            timeout = (connect, min(4, remaining - connect))
        try:
            with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout) as response:
                response.raise_for_status()
                return response.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            if deadline is None or attempt or isinstance(exc, requests.exceptions.SSLError):
                raise
            logging.info("作品别名连接暂时失败，在剩余时限内重试一次")


def _get_json(url, deadline=None):
    data = _request_json(url, deadline=deadline)
    if url == BILIBILI_CALENDAR:
        if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(data.get("result"), dict):
            raise ValueError("哔哩哔哩国创周历暂不可用")
        data = data["result"].get("timeline")
    elif url.startswith(BILIBILI_SEASON):
        if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(data.get("result"), dict):
            raise ValueError("哔哩哔哩作品详情暂不可用")
        data = [data["result"]]
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("数据格式不是条目列表")
    return data


def _fetch_many(urls, deadline=None, on_result=None):
    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = {executor.submit(_get_json, url, **({"deadline": deadline} if deadline is not None else {})): key
                   for key, url in urls.items()}
        for future in as_completed(pending):
            key = pending[future]
            try:
                results[key] = future.result()
            except (requests.RequestException, ValueError) as exc:
                logging.warning("Calendar source %s failed: %s", key, exc)
                errors[key] = "连接超时，请检查 NAS 网络或 HTTPS_PROXY" if isinstance(exc, requests.Timeout) else str(exc)
            if on_result is not None:
                on_result(key, results.get(key), errors.get(key))
    return results, errors


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else 0


def _poster(images):
    if not isinstance(images, dict):
        return ""
    url = images.get("medium") or images.get("large") or ""
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else ""


def _title_key(title):
    return "".join(char.casefold() for char in unicodedata.normalize("NFKC", title) if char.isalnum())


def _bilibili_catalog_id(url):
    try:
        parsed = urlsplit(url if isinstance(url, str) else "")
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("bilibili.com", "www.bilibili.com"):
            return None
        match = re.fullmatch(r"/bangumi/(?:media/(md)|play/(ss))(\d+)/?", parsed.path)
        return ("media" if match[1] else "season", int(match[3])) if match else None
    except ValueError:
        return None


def _prefer_official_bilibili(days, warnings, fetch=True, deadline=None):
    pairs = []
    for day in days:
        official = [item for item in day["donghua"] if item["source"] == "Bilibili" and not item.get("stale")]
        for item in day["donghua"]:
            if item["source"] != "TVmaze":
                continue
            identity = _bilibili_catalog_id(item.get("official_site"))
            if not identity:
                continue
            for candidate in official:
                if _title_key(item.get("series_title", item["title"])) == _title_key(candidate["title"]):
                    pairs.append((day["date"], item["id"], int(candidate["id"].split(":")[1]), identity))
    now = monotonic()
    needed = list(dict.fromkeys(season for _, _, season, identity in pairs if identity[0] == "media"
                               and _bilibili_media.get(season, (None, 0))[1] <= now))[:12]
    details, errors = _fetch_many({season: BILIBILI_SEASON + str(season) for season in needed}, deadline=deadline) if fetch else ({}, {})
    for season in needed if fetch else []:
        entries = details.get(season) or [{}]
        data = entries[0]
        media = data.get("media_id") if data.get("season_id") == season else None
        valid = isinstance(media, int) and not isinstance(media, bool) and media > 0
        _bilibili_media[season] = (media if valid else None, now + (3600 if valid else 300))
    if errors:
        warnings.append("部分哔哩哔哩作品身份暂无法核对，已保留 TVmaze 对应条目")
    duplicates = set()
    for day, key, season, identity in pairs:
        media, expires = _bilibili_media.get(season, (None, 0))
        if identity == ("season", season) or identity[0] == "media" and expires > now and media == identity[1]:
            duplicates.add((day, key))
    for day in days:
        day["donghua"] = [item for item in day["donghua"] if (day["date"], item["id"]) not in duplicates]
    return any(identity[0] == "media" and _bilibili_media.get(season, (None, 0))[0] is None
               for _, _, season, identity in pairs)


def _chinese_aliases(entries):
    return list(dict.fromkeys(item["name"] for item in entries if isinstance(item, dict)
                              and isinstance(item.get("name"), str)
                              and re.search(r"[\u4e00-\u9fff]", item["name"])
                              and (not item.get("country") or isinstance(item["country"], dict)
                                   and item["country"].get("code") == "CN")))


def get_search_aliases(entry):
    """Fetch trusted names on subscription, including the same-named source book."""
    known = entry.get("search_aliases")
    aliases = [entry.get("search_title") or entry.get("title"), entry.get("title"), *(known if isinstance(known, list) else [])]
    key = str(entry.get("id", ""))
    if re.fullmatch(r"bangumi:\d+", key):
        deadline = monotonic() + 10
        try:
            subject_id = int(key.split(":")[1])
            data = _request_json(f"https://api.bgm.tv/v0/subjects/{subject_id}", deadline=deadline)
            if isinstance(data, dict) and data.get("id") == subject_id and data.get("type") == 2:
                aliases.extend((data.get("name_cn"), data.get("name")))
                for field in data.get("infobox", []) if isinstance(data.get("infobox"), list) else []:
                    if not isinstance(field, dict) or field.get("key") not in ("别名", "中文名"):
                        continue
                    value = field.get("value")
                    if isinstance(value, list):
                        aliases.extend(item.get("v") for item in value if isinstance(item, dict))
                    else:
                        aliases.append(value)
                original_name = data.get("name")
                if isinstance(original_name, str) and original_name.strip():
                    related = _get_json(f"https://api.bgm.tv/v0/subjects/{subject_id}/subjects", deadline=deadline)
                    book_names = [item.get("name_cn") for item in related[:50]
                                  if item.get("type") == 1 and item.get("relation") in ("书籍", "原作")
                                  and isinstance(item.get("name"), str)
                                  and unicodedata.normalize("NFKC", item["name"]).strip()
                                  == unicodedata.normalize("NFKC", original_name).strip()
                                  and isinstance(item.get("name_cn"), str)
                                  and re.search(r"[\u4e00-\u9fff]", item["name_cn"])]
                    # Put alternate Chinese titles before romanized names in the bounded search.
                    aliases[1:1] = book_names[:3]
        except (requests.RequestException, ValueError) as exc:
            logging.warning("Bangumi aliases unavailable for %s: %s", key, exc)
    elif (match := re.fullmatch(r"tvmaze:(\d+):s(\d+|unknown)", key)):
        show_id, season = int(match[1]), match[2]
        try:
            if show_id not in _aliases:
                entries = _get_json(f"https://api.tvmaze.com/shows/{show_id}/akas", deadline=monotonic() + 10)
                fallback = entry.get("series_title") or re.sub(r"\s*第\d+季$", "", entry.get("title") or "")
                _aliases[show_id] = _chinese_aliases(entries) or [fallback]
            numbered_season = season.isdigit() and int(season) > 1 and (
                int(season) < 1900 or str(entry.get("search_title") or entry.get("title") or "").endswith(f"第{season}季"))
            suffix = f" 第{season}季" if numbered_season else ""
            aliases.extend(name + suffix for name in _aliases[show_id])
        except (requests.RequestException, ValueError) as exc:
            logging.warning("TVmaze aliases unavailable for %s: %s", key, exc)
    return list(dict.fromkeys(value.strip() for value in aliases
                              if isinstance(value, str) and value.strip() and len(value) <= 200))[:8]


def _anime_items(groups, days, today):
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("weekday"), dict):
            continue
        weekday = group["weekday"].get("id")
        if not isinstance(weekday, int) or weekday not in range(1, 8) or not isinstance(group.get("items"), list):
            continue
        seen = {item["id"] for item in days[weekday - 1]["anime"]}
        for item in group["items"]:
            if not isinstance(item, dict) or item.get("type") != 2:
                continue
            subject_id = item.get("id")
            title = item.get("name_cn") or item.get("name")
            if not isinstance(subject_id, int) or not isinstance(title, str) or not title.strip():
                continue
            air_date = str(item.get("air_date") or "")
            try:
                if date.fromisoformat(air_date) > today:
                    continue
            except ValueError:
                pass
            key = f"bangumi:{subject_id}"
            if key in seen:
                continue
            seen.add(key)
            collection, rating = item.get("collection") or {}, item.get("rating") or {}
            days[weekday - 1]["anime"].append({
                "id": key, "title": title, "search_title": title,
                "search_aliases": [name for name in (item.get("name_cn"), item.get("name")) if isinstance(name, str) and name],
                "source": "Bangumi", "source_url": f"https://bgm.tv/subject/{subject_id}",
                "poster": _poster(item.get("images")),
                "heat": _number(collection.get("doing")) if isinstance(collection, dict) else 0,
                "heat_label": "Bangumi 在看人数",
                "rating": _number(rating.get("score")) if isinstance(rating, dict) else 0,
                "year": str(item.get("air_date") or "")[:4],
                "schedule_note": "当季周播表；具体停播、调档以来源为准",
            })


def _bilibili_items(groups, days):
    days_by_date = {day["date"]: day for day in days}
    for group in groups:
        timestamp = group.get("date_ts")
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            continue
        try:
            day = days_by_date.get(datetime.fromtimestamp(timestamp, CHINA_TZ).date().isoformat())
        except (OverflowError, OSError, ValueError):
            continue
        if day is None or not isinstance(group.get("episodes"), list):
            continue
        for entry in group["episodes"]:
            if not isinstance(entry, dict):
                continue
            title, season = entry.get("title"), entry.get("season_id")
            if not isinstance(title, str) or not title.strip() or not isinstance(season, int) or isinstance(season, bool):
                continue
            follows = str(entry.get("follows") or "")
            match = re.fullmatch(r"([\d.]+)(万|亿)?(?:系列)?追番", follows)
            try:
                heat = float(match[1]) * {None: 1, "万": 10000, "亿": 100000000}[match[2]] if match else 0
            except ValueError:
                heat = 0
            delay = entry.get("delay_reason") if entry.get("delay") else ""
            status = f"延期：{delay or '以官方通知为准'}" if entry.get("delay") else (
                "已发布" if entry.get("published") == 1 else "已排期，尚未发布")
            day["donghua"].append({
                "id": f"bilibili:{season}", "title": title.strip(), "search_title": title.strip(),
                "source": "Bilibili", "source_url": f"https://www.bilibili.com/bangumi/play/ss{season}",
                "poster": _poster({"medium": entry.get("cover")}), "heat": _number(heat),
                "heat_label": "哔哩哔哩系列追番人数" if "系列" in follows else "哔哩哔哩追番人数",
                "rating": 0, "year": "", "airdate": day["date"],
                "schedule_note": f"{entry.get('pub_time') or ''} {entry.get('pub_index') or ''}；{status}；哔哩哔哩国创官方排期",
            })


def _tv_items(results, days, warnings, resource_id=None, stale_keys=(), fetch_aliases=True, alias_limit=None, deadline=None):
    shows, episodes, stale_episodes = {}, {}, set()
    day_dates = {day["date"] for day in days}
    for key, entries in results.items():
        if key in ("anime", "bilibili"):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("airdate"), str) or entry["airdate"] not in day_dates:
                continue
            embedded = entry.get("_embedded")
            show = entry.get("show") or (embedded.get("show") if isinstance(embedded, dict) else None)
            if not isinstance(show, dict) or show.get("type") not in ("Scripted", "Animation"):
                continue
            show_id, title = show.get("id"), show.get("name")
            if not isinstance(show_id, int) or not isinstance(title, str) or not title.strip():
                continue
            if resource_id is not None and not resource_id.startswith(f"tvmaze:{show_id}:s"):
                continue
            shows[show_id] = show
            season, number = entry.get("season"), entry.get("number")
            if any(value is not None and not isinstance(value, int) for value in (season, number)):
                continue
            episodes.setdefault((entry["airdate"], show_id), set()).add((season, number))
            if key in stale_keys:
                stale_episodes.add((entry["airdate"], show_id, season, number))
    dramas = sorted((key for key in shows if shows[key]["type"] == "Scripted"),
                    key=lambda key: (-_number(shows[key].get("weight")), key))
    popular = dramas[:TV_LIMIT]
    alias_pending = [key for key in popular if key not in _aliases][:10]
    if len(dramas) > TV_LIMIT:
        warnings.append(f"电视剧 TVmaze 来源本周超过 {TV_LIMIT} 部，已展示热度最高的 {TV_LIMIT} 部")
    animations = sorted((key for key in shows if shows[key]["type"] == "Animation"
                         and shows[key].get("language") in (None, "Chinese", "Mandarin", "Cantonese")),
                        key=lambda key: (-_number(shows[key].get("weight")), key))
    popular += animations[:DONGHUA_LIMIT]
    alias_pending += [key for key in animations[:DONGHUA_LIMIT] if key not in _aliases]
    if alias_limit is not None:
        alias_pending = alias_pending[:alias_limit]
    if len(animations) > DONGHUA_LIMIT:
        warnings.append(f"国漫 TVmaze 来源本周超过 {DONGHUA_LIMIT} 部，已展示热度最高的 {DONGHUA_LIMIT} 部")
    aliases, failures = _fetch_many({
        key: f"https://api.tvmaze.com/shows/{key}/akas" for key in alias_pending
    }, deadline=deadline) if fetch_aliases else ({}, {})
    for key, entries in aliases.items():
        _aliases[key] = _chinese_aliases(entries) or [shows[key]["name"]]
    if failures:
        warnings.append(f"{len(failures)} 部作品的中文别名暂不可用，已保留原名")
    for day in days:
        for key in popular:
            aired = episodes.get((day["date"], key))
            if not aired:
                continue
            show = shows[key]
            title = _aliases.get(key, [show["name"]])[0]
            rating = show.get("rating") or {}
            category = "donghua" if show["type"] == "Animation" else "tv"
            for season in sorted({value[0] for value in aired}, key=lambda value: value or 0):
                numbers = sorted({number for current, number in aired if current == season}, key=lambda value: value or 0)
                # Only year-numbered seasons are unsuitable for Chinese season matching.
                numbered_season = season and season > 1 and (category == "tv" or season < 1900)
                suffix = f" 第{season}季" if numbered_season else ""
                search_title = title + suffix
                season_label = f"第{season}季" if season is not None else "季数未标注"
                episode_label = "、".join(str(number) for number in numbers if number is not None)
                day[category].append({
                    "id": f"tvmaze:{key}:s{season if season is not None else 'unknown'}",
                    "title": search_title, "search_title": search_title,
                    "series_title": title, "official_site": show.get("officialSite"),
                    "search_aliases": [name + suffix for name in _aliases.get(key, [])] if category == "donghua" else [],
                    "source": "TVmaze", "source_url": f"https://www.tvmaze.com/shows/{key}",
                    "poster": _poster(show.get("image")), "heat": _number(show.get("weight")),
                    "heat_label": "TVmaze 热度（0–100）",
                    "rating": _number(rating.get("average")) if isinstance(rating, dict) else 0,
                    "year": str(show.get("premiered") or "")[:4] if category == "tv" and season == 1 else "",
                    "airdate": day["date"],
                    "episodes": [{"season": season, "number": number} for number in numbers],
                    "stale": any((day["date"], key, season, number) in stale_episodes for number in numbers),
                    "schedule_note": f"TVmaze {season_label} 第{episode_label or '?'}集；中国播出排期，不代表网盘资源已更新",
                })


def _week(now):
    return (now.date() - timedelta(days=now.weekday())).isoformat()


def _days(now):
    monday = now.date() - timedelta(days=now.weekday())
    return [{"weekday": index + 1, "label": f"星期{label}",
             "date": (monday + timedelta(days=index)).isoformat(), "anime": [], "donghua": [], "tv": []}
            for index, label in enumerate("一二三四五六日")]


def _calendar_urls(now, resource_id=None):
    source = None if resource_id is None else (
        resource_id.split(":")[0] if isinstance(resource_id, str)
        and re.fullmatch(r"(?:bangumi|bilibili):\d+|tvmaze:\d+:s(?:\d+|unknown)", resource_id) else "")
    # 两个单请求来源先发布，不排在整周 TVmaze 请求后面。
    urls = {}
    if source in (None, "bangumi"):
        urls["anime"] = "https://api.bgm.tv/calendar"
    if source in (None, "bilibili"):
        urls["bilibili"] = BILIBILI_CALENDAR
    if source in (None, "tvmaze"):
        for day in _days(now):
            urls[day["date"]] = f"https://api.tvmaze.com/schedule?country=CN&date={day['date']}"
            urls[f"web:{day['date']}"] = f"https://api.tvmaze.com/schedule/web?country=CN&date={day['date']}"
    return urls


def _select_resource(calendar, resource_id):
    if resource_id is not None:
        for day in calendar["days"]:
            for category in ("anime", "donghua", "tv"):
                day[category] = [item for item in day[category] if item["id"] == resource_id]
    return calendar


def _assemble_calendar(now, results, errors, stale_keys=(), resource_id=None, enrich=True, deadline=None, alias_limit=None):
    days = _days(now)
    source_names = {"anime": "Bangumi 周播表", "bilibili": "哔哩哔哩国创周历"}
    warnings = [f"{source_names.get(key, 'TVmaze ' + key)} 获取失败：{error}" for key, error in sorted(errors.items())]
    _anime_items(results.get("anime", []), days, now.date())
    _bilibili_items(results.get("bilibili", []), days)
    for day in days:
        for category, source in (("anime", "anime"), ("donghua", "bilibili")):
            if source in stale_keys:
                for item in day[category]:
                    item["stale"] = True
    _tv_items(results, days, warnings, resource_id, stale_keys, enrich, alias_limit, deadline)
    if stale_keys:
        warnings.append("部分来源沿用本周上次成功数据，已标注缓存；排期可能已变更")
    identity_pending = _prefer_official_bilibili(days, warnings, fetch=enrich, deadline=deadline) if resource_id is None else False
    for day in days:
        unique = {}
        for item in sorted(day["donghua"], key=lambda entry: (bool(entry.get("stale")), entry["source"] != "Bilibili")):
            unique.setdefault(_title_key(item["title"]), item)
        day["donghua"] = sorted(unique.values(), key=lambda item: (item["source"] != "TVmaze", -item["heat"], item["title"]))
        for category in ("anime", "tv"):
            day[category].sort(key=lambda item: (-item["heat"], item["title"]))
    tv_items = {item["id"]: item for day in days for category in ("donghua", "tv") for item in day[category]
                if item["source"] == "TVmaze"}
    untranslated = sum(not re.search(r"[\u4e00-\u9fff]", item["series_title"]) for item in tv_items.values())
    notes = ["动漫：Bangumi 当季周播表，已排除首播日期在未来的作品；不保证本周每一部都更新。",
             "国漫：哔哩哔哩官方国创日历与 TVmaze 中国网络/电视播出排期，按本周真实日期归日；覆盖范围以来源收录为准。",
             f"电视剧：TVmaze 中国播出数据，本周热度前 {TV_LIMIT} 部，按实际集日期归日、按 TVmaze 热度排序。",
             "热度口径按条目标注，按来源分组、组内热度降序；不同来源数值不直接比较。TVmaze 数据按 CC BY-SA 4.0 使用。"]
    if untranslated:
        notes.append(f"{untranslated} 部作品尚无可用中文名，先显示原名；订阅会按需查询官方别名，不影响浏览。")
    calendar = {"week_start": _week(now), "week_end": days[-1]["date"], "timezone": "Asia/Shanghai",
                "updated_at": now.isoformat(), "cached": False, "refreshing": False, "stale": bool(stale_keys),
                "days": days, "warnings": warnings, "notes": notes}
    alias_pending = any(int(key.split(":")[1]) not in _aliases for key in tv_items)
    return _select_resource(calendar, resource_id), identity_pending or alias_pending


def _refresh_calendar(now, old_sources, done, background):
    global _cache, _expires, _source_data, _snapshot_week, _preview, _refreshing, _refresh_thread, _retry_at
    results, errors = dict(old_sources), {}
    stale_keys = set(old_sources)
    week_start = _week(now)

    def publish(key, data, error):
        global _preview
        if error is None:
            results[key] = data
            stale_keys.discard(key)
        else:
            errors[key] = error
        preview, _ = _assemble_calendar(now, results, errors, stale_keys, enrich=False)
        preview["refreshing"] = True
        with _lock:
            if _requested_week == week_start:
                _preview = preview

    try:
        _fetch_many(_calendar_urls(now), deadline=monotonic() + 30 if background else None, on_result=publish)
        # 主数据已可浏览，别名和身份补充共用额外 10 秒；不阻塞 API。
        calendar, pending = _assemble_calendar(
            now, results, errors, stale_keys, deadline=monotonic() + 10 if background else None,
            alias_limit=10 if background else None)
        with _lock:
            if _requested_week == week_start:
                _cache, _source_data, _snapshot_week = calendar, results, week_start
                _expires = monotonic() + (300 if errors or pending else 3600)
                _retry_at = 0
    except Exception as error:
        logging.warning("周历后台刷新未完成 (%s)", type(error).__name__)
        with _lock:
            if _requested_week == week_start:
                _preview["refreshing"] = False
                _preview["warnings"].append("本轮周历刷新未完成，将稍后重试")
                _retry_at = monotonic() + 300
    finally:
        with _lock:
            _refreshing = False
            _refresh_thread = None
            done.set()


def get_calendar(config=None, resource_id=None, background=False, refresh=False):
    """UI reads a progressive snapshot; synchronous callers only use complete calendars."""
    global _preview, _refreshing, _refresh_thread, _refresh_done, _requested_week, _retry_at
    now, start, wait = datetime.now(CHINA_TZ), False, None
    week_start = _week(now)
    with _lock:
        _requested_week = week_start
        current = _cache if _cache and _cache["week_start"] == week_start else None
        fresh = current is not None and monotonic() < _expires
        if (resource_id is None and not refresh and not _refreshing
                and _preview and _preview["week_start"] == week_start and monotonic() < _retry_at):
            return deepcopy(_preview)
        if not refresh and fresh and not (background and _refreshing):
            cached = deepcopy(current)
            cached["cached"] = True
            _select_resource(cached, resource_id)
            if resource_id is None or any(day[kind] for day in cached["days"] for kind in ("anime", "donghua", "tv")):
                return cached
        if resource_id is None:
            if not _refreshing:
                old_sources = deepcopy(_source_data) if current and _snapshot_week == week_start else {}
                _preview = deepcopy(current) if current else _assemble_calendar(now, {}, {}, enrich=False)[0]
                _preview.update(refreshing=True, cached=bool(current), stale=bool(current))
                if current:
                    for day in _preview["days"]:
                        for category in ("anime", "donghua", "tv"):
                            for item in day[category]:
                                item["stale"] = True
                _refresh_done = Event()
                _refreshing, start = True, True
                _retry_at = 0
                if background:
                    _refresh_thread = Thread(target=_refresh_calendar, args=(now, old_sources, _refresh_done, True),
                                             name="calendar-refresh", daemon=True)
            if background:
                snapshot = deepcopy(_preview) if _preview and _preview["week_start"] == week_start else _assemble_calendar(now, {}, {}, enrich=False)[0]
                snapshot["refreshing"] = True
                worker = _refresh_thread if start else None
            elif not start:
                wait = _refresh_done
    if resource_id is not None:
        # A partial UI preview must never turn an unfinished source into “作品不存在”.
        results, errors = _fetch_many(_calendar_urls(now, resource_id))
        return _assemble_calendar(now, results, errors, resource_id=resource_id)[0]
    if background:
        if worker:
            try:
                worker.start()
            except RuntimeError:
                with _lock:
                    _refreshing = False
                    _refresh_thread = None
                    _retry_at = monotonic() + 300
                    _refresh_done.set()
                snapshot.update(refreshing=False)
                snapshot["warnings"].append("无法启动周历刷新，请稍后重试")
                with _lock:
                    _preview = deepcopy(snapshot)
        return snapshot
    if wait is not None:
        wait.wait()
        return get_calendar(config=config)
    _refresh_calendar(now, old_sources, _refresh_done, False)
    with _lock:
        return deepcopy(_preview if monotonic() < _retry_at else _cache)
