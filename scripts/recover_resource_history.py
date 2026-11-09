"""从安装目录与更新备份恢复分享线索；不恢复订阅、Cookie 或其他设置。"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quark_auto_save import Config


def recover_resource_history(root, config_path):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError()
    candidates = []
    seen = set()
    for directory in [root, *root.glob("backup-v*")]:
        try:
            directory.resolve(strict=True).relative_to(root)
            path = (directory / "config" / "quark_config.json").resolve(strict=True)
            path.relative_to(root)
            if path.is_file() and path not in seen:
                seen.add(path)
                candidates.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError, ValueError) as error:
            print(f"跳过旧配置（{type(error).__name__}）", file=sys.stderr)

    tasks, files_read = [], 0
    for _, path in sorted(candidates, key=lambda item: item[0], reverse=True)[:10]:
        try:
            with path.open(encoding="utf-8-sig") as stream:
                data = json.load(stream)
            if not isinstance(data, dict) or not isinstance(data.get("tasklist", []), list):
                raise ValueError()
            tasks.extend(data.get("tasklist", []))
            files_read += 1
        except (OSError, ValueError) as error:
            print(f"跳过旧配置（{type(error).__name__}）", file=sys.stderr)

    history_path = Config.resource_history_path(config_path)
    before = Config.resource_history(history_path)
    Config.remember_resources(history_path, tasks)
    after = Config.resource_history(history_path)
    return {"files_read": files_read, "imported_candidates": sum(item not in before for item in after)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="只读挂载的 NAS 安装目录")
    parser.add_argument("--config", default="./config/quark_config.json", help="当前活动配置路径")
    args = parser.parse_args(argv)
    try:
        result = recover_resource_history(args.root, args.config)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"资源线索恢复失败（{type(error).__name__}）", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
