"""转存、候选验证和下载共用的文件类型白名单。"""

import re

VIDEO_EXTENSIONS = ("mp4", "mkv", "mov", "m4v", "avi", "ts", "m2ts", "mts", "wmv", "flv", "webm", "mpg", "mpeg", "rmvb", "rm", "vob", "ogv", "3gp")
SUBTITLE_EXTENSIONS = ("srt", "ass", "ssa", "vtt", "sub", "idx", "sup", "smi", "sami", "ttml", "dfxp", "scc")
MEDIA_PATTERN = r"(?i).*\.(?:" + "|".join(VIDEO_EXTENSIONS + SUBTITLE_EXTENSIONS) + r")$"


def media_title(title):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")


def is_video_file(item):
    name, dot, extension = str(item.get("file_name", "")).rpartition(".")
    return not item.get("dir", item.get("is_dir", False)) and bool(name and dot) and extension.casefold() in VIDEO_EXTENSIONS


def is_media_file(item):
    name, dot, extension = str(item.get("file_name", "")).rpartition(".")
    return not item.get("dir", item.get("is_dir", False)) and bool(name and dot) and extension.casefold() in VIDEO_EXTENSIONS + SUBTITLE_EXTENSIONS
