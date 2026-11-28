#!/usr/bin/env python3
"""把旧版 cli_delegate.py 的放行规则合并进 Claude Code 的 settings.json。

用法:
  python3 setup_permissions.py            # 只预览要加、要删的规则，不写文件
  python3 setup_permissions.py --apply    # 写入（先备份为 settings.json.bak-<时间>）
  [--settings <路径>]                      # 默认 ~/.claude/settings.json

规则清单在 ../references/permissions.json：
  allow  —— 按子命令逐条放行；
  remove —— 旧的宽泛写法（如 `cli_delegate.py *`）和旧 skill 名的规则，合并时删掉；
  never_allow —— undo/merge/discard/prune/rules/unsafe/queue cancel/models --set/models --clear，脚本会报告已存在的这类放行，但不替你删。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

SPEC = Path(__file__).resolve().parent.parent / "references" / "permissions.json"
ENTRY = "python3 ~/.claude/skills/cli-manager/scripts/cli_delegate.py "


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", default="~/.claude/settings.json")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    path = Path(a.settings).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError as e:
        print(f"{path} 不是合法 JSON（{e}），请先修好再运行", file=sys.stderr)
        return 1
    perms = data.setdefault("permissions", {})
    cur = list(perms.get("allow") or [])
    removed = [r for r in cur if r in spec["remove"]]
    keep = [r for r in cur if r not in spec["remove"]]
    added = [r for r in spec["allow"] if r not in keep]
    risky = [r for r in keep for s in spec["never_allow"] if r.startswith(f"Bash({ENTRY}{s}")]
    for r in added:
        print("+", r)
    for r in removed:
        print("-", r)
    if not added and not removed:
        print("放行规则已是最新，无需改动")
    for r in risky:
        print("! 这条放行了应逐次确认的子命令，建议手动删除:", r)
    if not a.apply or not (added or removed):
        if added or removed:
            print("\n以上为预览；确认后加 --apply 写入")
        return 0
    perms["allow"] = keep + added
    if path.exists():
        bak = path.with_name(path.name + time.strftime(".bak-%Y%m%d-%H%M%S"))
        shutil.copy2(path, bak)
        print("已备份:", bak)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    print(f"已写入 {path}：新增 {len(added)} 条，删除 {len(removed)} 条。新开的会话生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
