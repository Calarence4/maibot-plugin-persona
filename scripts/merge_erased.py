"""恢复备份前合并 erased 屏障。

用法::

    python scripts/merge_erased.py \
        --current /root/my-maibot/data/plugins/com.calarence.persona/memory/erased.json \
        --backup  /root/my-maibot/data/plugins/com.calarence.persona/memory/backups/daily/2026-11-07/erased.json

规则（《方案.md》§1.1）：

- 先读当前屏障，再与备份屏障按 ``erasureId`` 求并集；当前条目优先。
- 禁止用较旧备份覆盖或删减当前屏障。
- 干净目录恢复纯旧备份时，无法知道备份之后的擦除：默认拒绝写入并提示风险，
  确认接受风险后才允许用 ``--accept-missing-risk`` 安装备份屏障。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "entries": {}}
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SystemExit(f"{path} 不是有效 JSON 对象")
    entries = data.get("entries")
    if not isinstance(entries, dict):
        data["entries"] = {}
    return data


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="合并 erased 防复活屏障")
    parser.add_argument("--current", required=True, help="当前 data/memory/erased.json")
    parser.add_argument("--backup", required=True, help="备份里的 erased.json")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写入")
    parser.add_argument(
        "--accept-missing-risk",
        action="store_true",
        help="当前屏障为空时，仍用备份屏障（会丢失备份之后的擦除记录）",
    )
    args = parser.parse_args()

    current_path = Path(args.current)
    backup_path = Path(args.backup)
    current = load(current_path)
    backup = load(backup_path)
    current_entries: dict[str, Any] = dict(current.get("entries") or {})
    backup_entries: dict[str, Any] = dict(backup.get("entries") or {})

    if not current_entries and backup_entries and not args.accept_missing_risk:
        print("拒绝写入：当前屏障为空，备份之后发生的擦除未知，恢复可能复活旧数据。", file=sys.stderr)
        print("确认接受风险后再加 --accept-missing-risk。", file=sys.stderr)
        return 2

    merged = dict(backup_entries)
    merged.update(current_entries)
    added = len(set(merged) - set(current_entries))
    payload = {
        "version": 1,
        "count": len(merged),
        "entries": merged,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    print(f"当前屏障 {len(current_entries)} 条，备份屏障 {len(backup_entries)} 条，合并后 {len(merged)} 条（新增 {added}）")
    if args.dry_run:
        print("dry-run：未写入")
        return 0
    atomic_write(current_path, payload)
    print(f"已写入 {current_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
