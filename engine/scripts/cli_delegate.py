#!/usr/bin/env python3
"""cli-delegate: one entry point for delegating tasks to the Claude Code CLI (backend "claude",
`claude -p`, isolated from the Desktop session) or to the local Codex CLI (backend "codex").

  quota | pick            Quota of both backends (no model tokens) / which backend would be used.
  run [--backend auto|claude|codex] [--isolate | --group NAME] [--verify CMD] [--no-snapshot] [--max-parallel N] ...
                          Pick the backend, snapshot the directory, run, verify, write the ledger.
  resume <run> ...        Continue a run on the same backend/session (also wrapped).
  handoff <run> --id NEW (--prepare | --brief-file F)
                          Continue on the OTHER backend: --prepare extracts the material, an
                          agent writes the brief, --brief-file starts the other backend.
  watch|decide|show|progress <run> ...   Forwarded to the owning backend.
  queue add|work|watch|list|cancel|ack   Task queue with per-backend concurrency limits and
                          automatic resume after the quota resets.
  diff|undo <run>         Changes made by a run / restore the directory to before the run.
  merge|discard <run>     Apply or drop the result of an --isolate (git worktree) run.
  ledger [<run>]          Usage ledger (model, tokens, quota before/after, limit hit, switch).
  rules                   Show the project's .cli-delegate.toml.
  prune [--days N] [--yes]  Delete old run records.
  image | image-batch | image-sheet   Bitmap generation, always on codex (GPT image tool).
  result <run>            Path + sha256 of the run's final report (verbatim relay).
  models [--list | --set B -m M -e E | --clear B]
                          Default model/effort per backend (~/.cli-delegate/config.json); used when
                          a run gives no -m. --list queries the available models (no tokens).
  list | doctor

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extras as X  # noqa: E402

ME = "cli-delegate"
BASE = Path(__file__).resolve().parent.parent
BACKEND_FILES = {
    "claude": BASE / "backends" / "claude" / "delegate.py",
    "codex": BASE / "backends" / "codex" / "codex_delegate.py",
}
STATE_DIR = Path(os.environ.get("CLI_DELEGATE_STATE", Path.home() / ".cli-delegate")).expanduser()
CLAUDE_MODEL_HINTS = ("opus", "sonnet", "haiku", "fable", "best", "opusplan", "claude")
CODEX_MODEL_HINTS = ("gpt", "o1", "o3", "o4", "codex")
_mods: dict = {}


def die(msg: str, code: int = 2) -> None:
    print(f"[{ME}] 错误: {msg}", file=sys.stderr)
    sys.exit(code)


def note(msg: str) -> None:
    print(f"[{ME}] {msg}", file=sys.stderr, flush=True)


def backend_mod(name: str):
    """Import a backend script as a module (its main() is guarded, so nothing runs)."""
    if name not in _mods:
        path = BACKEND_FILES[name]
        spec = importlib.util.spec_from_file_location(f"cli_delegate_backend_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        argv = sys.argv
        try:
            sys.argv = [str(path)]
            spec.loader.exec_module(mod)
        finally:
            sys.argv = argv
        _mods[name] = mod
    return _mods[name]


def exec_backend(name: str, args: list[str]) -> None:
    """Replace this process with the backend script (keeps stdin/stdout and the exit code)."""
    path = str(BACKEND_FILES[name])
    os.execv(sys.executable, [sys.executable, path, *args])


# --------------------------------------------------------------------- runs ----
def runs_dir(name: str) -> Path:
    return backend_mod(name).RUNS_DIR


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def valid_id(x: str | None, what: str = "运行名") -> str:
    """Run / job identifiers become file names under the state directories: no separators, no '..'."""
    if not x or not _ID_RE.match(x) or ".." in x:
        die(f"{what} 不合法: {x!r}（只能用字母、数字、. _ -，不能含 / 或 ..）")
    return x


def find_run(arg: str | None) -> tuple[str, Path]:
    """Return (backend, run_dir) for a run name/path, or the newest run of both for 'last'."""
    if arg in (None, "", "last"):
        best = None
        for b in BACKEND_FILES:
            runs = backend_mod(b).all_runs()
            if runs:
                key = backend_mod(b).run_order(runs[-1])
                if best is None or key > best[0]:
                    best = (key, b, runs[-1])
        if not best:
            die("还没有任何运行记录")
        return best[1], best[2]
    p = Path(arg).expanduser()
    hits = []
    for b in BACKEND_FILES:
        rd = runs_dir(b)
        if p.is_dir() and rd.resolve() in p.resolve().parents:
            return b, p.resolve()
    if "/" in arg and not p.exists() and _ID_RE.match(p.name or ""):
        arg = p.name  # an old absolute path (e.g. before the run records moved): look it up by name
    valid_id(arg)
    for b in BACKEND_FILES:
        rd = runs_dir(b)
        if (rd / arg).is_dir():
            hits.append((backend_mod(b).run_order(rd / arg), b, rd / arg))
    if not hits:
        die(f"找不到运行记录: {arg}")
    hits.sort()
    return hits[-1][1], hits[-1][2]


def claim_id(run_id: str) -> None:
    """Atomically reserve a run name (O_EXCL) before any side effect; concurrent runs with the
    same name cannot both pass."""
    valid_id(run_id)
    if id_taken(run_id):
        die(f"--id {run_id} 已被占用")
    X.RMETA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(X.rmeta_path(run_id), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        die(f"--id {run_id} 已被占用")
    os.write(fd, json.dumps({"id": run_id, "status": "claimed", "router_pid": os.getpid()}).encode())
    os.close(fd)
    import atexit

    def _release():  # the run never started (died before launch saved its record): free the name
        if X.load_rmeta(run_id).get("status") == "claimed":
            X.rmeta_path(run_id).unlink(missing_ok=True)
    atexit.register(_release)


def id_taken(run_id: str) -> str | None:
    valid_id(run_id)
    for b in BACKEND_FILES:
        if (runs_dir(b) / run_id).exists():
            return b
    return None


# -------------------------------------------------------------------- quota ----
_USAGE_LINE = re.compile(r"^\s*Current ([^:]+):\s*(\d+)%\s*used(?:\s*·\s*(resets .+))?\s*$", re.I | re.M)


def claude_quota() -> dict:
    m = backend_mod("claude")
    env = m.clean_env({}, [])
    try:
        r = subprocess.run([m.find_bin(), "-p", "/usage", "--output-format", "json", "--no-session-persistence"],
                           capture_output=True, text=True, env=env, timeout=120, input="", cwd=str(Path.home()))
        res = json.loads(r.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        return {"ok": False, "error": f"/usage 查询失败: {e}"}
    text = res.get("result") or ""
    wins = []
    for name, used, resets in _USAGE_LINE.findall(text):
        at = m.parse_reset(resets) if resets else None
        wins.append({"name": name.strip(), "used": int(used),
                     "resets_at": at.isoformat(timespec="minutes") if at else None})
    if not wins:
        return {"ok": False, "error": "无法从 /usage 输出中解析额度", "raw": text[:400]}
    return {"ok": True, "windows": wins}


def codex_quota(home: str | None = None) -> dict:
    m = backend_mod("codex")
    env = m.clean_env(home or os.environ.get("CODEXCLI_HOME"), {}, [])
    tmp = STATE_DIR / "quota-probe"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "events.jsonl").write_text("")
    srv = m.AppServer([m.find_bin(), "app-server"], str(Path.home()), env, tmp, lambda a, b: None, lambda a, b: None)
    try:
        srv.request("initialize", {"clientInfo": {"name": "cli-delegate-quota", "version": "1.0.0"},
                                   "capabilities": {"experimentalApi": True}}, timeout=60)
        srv.notify("initialized")
        rl = srv.request("account/rateLimits/read", {}, timeout=60).get("rateLimits") or {}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    finally:
        srv.close()
    wins = []
    for key in ("primary", "secondary"):
        w = rl.get(key) or {}
        if not w:
            continue
        ra = w.get("resetsAt")
        at = _dt.datetime.fromtimestamp(ra / 1000 if ra > 1e12 else ra).astimezone() if ra else None
        mins = w.get("windowDurationMins")
        label = f"{key}（{mins // 60 if mins and mins >= 60 else mins}{'小时' if mins and mins >= 60 else '分钟'}窗口）" if mins else key
        wins.append({"name": label, "used": int(w.get("usedPercent") or 0),
                     "resets_at": at.isoformat(timespec="minutes") if at else None})
    return {"ok": True, "windows": wins}


QUOTA_FUNCS = {"claude": claude_quota, "codex": codex_quota}


def quota(name: str, home: str | None = None) -> dict:
    try:
        q = codex_quota(home) if (name == "codex" and home) else QUOTA_FUNCS[name]()
    except SystemExit:  # find_bin() of a backend that is not installed
        q = {"ok": False, "missing": True, "error": "未安装或找不到 CLI"}
    q["backend"] = name
    return q


def available(q: dict, threshold: int) -> tuple[bool, str | None]:
    """(usable, blocking reset time). Unknown quota counts as usable; a missing CLI never does."""
    if q.get("missing"):
        return False, None
    if not q.get("ok"):
        return True, None
    blocked = [w for w in q["windows"] if w["used"] >= threshold]
    if not blocked:
        return True, None
    resets = [w["resets_at"] for w in blocked if w.get("resets_at")]
    return False, max(resets) if resets else None


def fmt_quota(q: dict) -> str:
    if not q.get("ok"):
        return f"{q['backend']}: 未知（{q.get('error')}）"
    parts = [f"{w['name']} 已用 {w['used']}%" + (f"，{w['resets_at']} 重置" if w.get("resets_at") else "")
             for w in q["windows"]]
    return f"{q['backend']}: " + "；".join(parts)


# ---------------------------------------------------------- default models ----
# Skill-level defaults in ~/.cli-delegate/config.json:
#   "default_models": {"claude": {"model": "opus", "effort": "high"}, "codex": {"model": "gpt-…", "effort": "medium"}}
# An explicit -m always wins (the saved default effort is then ignored too); without -m the chosen
# backend's saved model/effort is used. Nothing saved -> exit 6 so the main session asks the user.
CONFIG_FILE = STATE_DIR / "config.json"
MODEL_SETUP_HINT = ("由主会话执行 `cli_delegate.py models --list` 查可用模型和思考强度，让用户选定后用 "
                    "`cli_delegate.py models --set <后端> --model <模型> --effort <强度>` 写入")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def load_cfg() -> dict:
    cfg = X.read_json(CONFIG_FILE, {})
    return cfg if isinstance(cfg, dict) else {}


def _dm(cfg: dict) -> dict:
    dm = cfg.get("default_models")
    return dm if isinstance(dm, dict) else {}


def default_model(backend: str) -> dict:
    d = _dm(load_cfg()).get(backend)
    return d if isinstance(d, dict) and d.get("model") else {}


def update_default_models(fn) -> None:
    """Read-modify-write default_models under an exclusive lock (parallel --set calls must not
    lose each other's backend); other keys of config.json are kept."""
    import fcntl
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE.with_name("config.json.lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        if CONFIG_FILE.exists() and not isinstance(X.read_json(CONFIG_FILE, None), dict):
            die(f"{CONFIG_FILE} 无法解析为 JSON 对象；为免覆盖其中的其他设置（如 claude_config_dir），请先修复该文件再写入")
        cfg = load_cfg()
        dm = _dm(cfg)
        fn(dm)
        cfg["default_models"] = dm
        tmp = CONFIG_FILE.with_name(f"config.json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)


def missing_defaults(backends) -> list[str]:
    return [b for b in backends if not default_model(b)]


def apply_default_model(args: list[str], backend: str) -> tuple[list[str], str]:
    """Returns (args, model source). Explicit / project-default -m: untouched. Otherwise add the
    backend's saved default model (and its effort unless -e was given)."""
    if opt_value(args, ("--model", "-m")):
        return args, "explicit"
    d = default_model(backend)
    if not d:
        die(f"{backend} 后端还没有设置默认模型，本次也没有用 -m 指定模型。{MODEL_SETUP_HINT}", 6)
    args = [*args, "--model", d["model"]]
    eff = opt_value(args, ("--effort", "-e"))
    if d.get("effort") and eff is None:
        args += ["--effort", d["effort"]]
    note(f"使用默认模型: {backend} {d['model']}，强度 {eff or d.get('effort') or 'CLI 默认'}"
         f"{'（本次另行指定，未用默认强度）' if eff else ''}（{CONFIG_FILE}）")
    return args, "default"


def claude_models() -> dict:
    """Model aliases and effort levels from `claude -p /model` and `/effort` (local commands: no tokens)."""
    m = backend_mod("claude")
    env = m.clean_env({}, [])
    out = {}
    for cmd in ("/model", "/effort"):
        try:
            r = subprocess.run([m.find_bin(), "-p", cmd, "--output-format", "json", "--no-session-persistence"],
                               capture_output=True, text=True, env=env, timeout=120, input="", cwd=str(Path.home()))
            out[cmd] = _ANSI.sub("", json.loads(r.stdout or "{}").get("result") or "")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            return {"ok": False, "backend": "claude", "error": f"{cmd} 查询失败: {e}"}
    txt = out["/model"]
    am = re.search(r"Available:\s*(.+?)(?:,?\s*or a full model ID|\.?\s*$)", txt, re.S)
    if not am:
        return {"ok": False, "backend": "claude", "error": "无法从 /model 输出中解析可用模型", "raw": txt[:400]}
    names = [x.strip().strip("`") for x in am.group(1).split(",") if x.strip()]
    cm = re.search(r"Current model:\s*`?([^`\n]+)`?", txt)
    em = re.search(r"<([a-z|]+)", out["/effort"])
    efforts = [e for e in (em.group(1).split("|") if em else []) if e in m.EFFORTS] or list(m.EFFORTS)
    models = [{"model": n, "efforts": efforts, "default_effort": None,
               "note": "账号默认模型" if n == "default" else ""} for n in names]
    return {"ok": True, "backend": "claude", "current": cm.group(1).strip() if cm else None,
            "models": models, "efforts": efforts, "full_id_ok": True}


def codex_models(home: str | None = None) -> dict:
    """Models with their supported reasoning efforts from the app-server `model/list` (no tokens)."""
    m = backend_mod("codex")
    env = m.clean_env(home or os.environ.get("CODEXCLI_HOME"), {}, [])
    tmp = STATE_DIR / "models-probe"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "events.jsonl").write_text("")
    srv = m.AppServer([m.find_bin(), "app-server"], str(Path.home()), env, tmp, lambda a, b: None, lambda a, b: None)
    try:
        srv.request("initialize", {"clientInfo": {"name": "cli-delegate-models", "version": "1.0.0"},
                                   "capabilities": {"experimentalApi": True}}, timeout=60)
        srv.notify("initialized")
        ml = srv.request("model/list", {}, timeout=60)
    except RuntimeError as e:
        return {"ok": False, "backend": "codex", "error": str(e)}
    finally:
        srv.close()
    models = []
    for x in ml.get("data") or []:
        models.append({"model": x.get("model") or x.get("id"), "default": bool(x.get("isDefault")),
                       "efforts": [e.get("reasoningEffort") for e in x.get("supportedReasoningEfforts") or []
                                   if e.get("reasoningEffort")],
                       "default_effort": x.get("defaultReasoningEffort"),
                       "note": x.get("description") or ""})
    if not models:
        return {"ok": False, "backend": "codex", "error": "model/list 没有返回模型"}
    return {"ok": True, "backend": "codex", "models": models, "full_id_ok": False}


def list_models(backend: str) -> dict:
    try:
        return claude_models() if backend == "claude" else codex_models()
    except SystemExit:
        return {"ok": False, "backend": backend, "missing": True, "error": "未安装或找不到 CLI"}


def cmd_models(a) -> int:
    names = list(BACKEND_FILES) if a.backend in (None, "all") else [a.backend]
    if a.clear:
        update_default_models(lambda dm: [dm.pop(b, None) for b in
                                          (list(BACKEND_FILES) if a.clear == "all" else [a.clear])])
        print(f"已清除默认模型: {a.clear}")
        return 0
    if a.set:
        b = a.set
        if not a.model:
            die("--set 需要 --model <模型>")
        if model_backend(a.model) not in (None, b):
            die(f"模型 {a.model} 属于 {model_backend(a.model)}，不能设为 {b} 的默认模型")
        eff = map_effort(a.effort, b) if a.effort else None
        if not a.no_check:
            info = list_models(b)
            if not info.get("ok"):
                die(f"无法查询 {b} 的可用模型（{info.get('error')}）；确认模型名无误后可加 --no-check 跳过校验")
            hit = next((x for x in info["models"] if x["model"] == a.model), None)
            if not hit and not (info.get("full_id_ok") and a.model.lower().startswith("claude-")):
                die(f"{b} 没有模型 {a.model}；可用: {', '.join(x['model'] for x in info['models'])}")
            allowed = (hit or {}).get("efforts") or info.get("efforts") or []
            if eff and allowed and eff not in allowed:
                die(f"{a.model} 不支持思考强度 {eff}；可选: {', '.join(allowed)}")
        entry = {"model": a.model, "effort": eff, "set_at": _dt.datetime.now().isoformat(timespec="seconds")}
        update_default_models(lambda dm: dm.__setitem__(b, entry))
        print(f"已保存 {b} 默认模型: {a.model}，思考强度 {eff or 'CLI 默认'}（{CONFIG_FILE}）")
        return 0
    if a.list:
        res = [list_models(b) for b in names]
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if all(r.get("ok") for r in res) else 4
        for r in res:
            print(f"===== {r['backend']} =====")
            if not r.get("ok"):
                print(f"查询失败: {r.get('error')}")
                continue
            if r.get("current"):
                print(f"CLI 当前默认: {r['current']}")
            for x in r["models"]:
                tag = "（默认）" if x.get("default") else ""
                de = f"，默认 {x['default_effort']}" if x.get("default_effort") else ""
                nt = f"  {x['note']}" if x.get("note") else ""
                print(f"  - {x['model']}{tag}  强度: {', '.join(x['efforts']) or '?'}{de}{nt}")
            if r.get("full_id_ok"):
                print("  （也可以填完整模型 ID，如 claude-opus-…）")
        return 0 if all(r.get("ok") for r in res) else 4
    dm = {b: default_model(b) for b in names}
    miss = [b for b, d in dm.items() if not d]
    if a.json:
        print(json.dumps({"config": str(CONFIG_FILE), "defaults": dm, "missing": miss}, ensure_ascii=False, indent=2))
    else:
        for b, d in dm.items():
            print(f"{b}: " + (f"{d['model']}，思考强度 {d.get('effort') or 'CLI 默认'}" if d else "未设置"))
        if miss:
            print(f"未设置默认模型: {', '.join(miss)}。{MODEL_SETUP_HINT}")
    return 6 if miss else 0


def model_backend(model: str | None) -> str | None:
    if not model:
        return None
    m = model.lower()
    if m.startswith(CLAUDE_MODEL_HINTS):
        return "claude"
    if m.startswith(CODEX_MODEL_HINTS):
        return "codex"
    return None


def pick_backend(backend: str, model: str | None, prefer: str, threshold: int, home: str | None = None) -> dict:
    hinted = model_backend(model)
    if backend != "auto":
        if hinted and hinted != backend:
            die(f"模型 {model} 属于 {hinted}，与 --backend {backend} 不符")
        return {"backend": backend, "reason": "指定后端", "quotas": []}
    if hinted:
        return {"backend": hinted, "reason": f"模型 {model} 属于 {hinted}", "quotas": []}
    order = [prefer] + [b for b in BACKEND_FILES if b != prefer]
    quotas, waits = [], []
    for b in order:
        q = quota(b, home if b == "codex" else None)
        quotas.append(q)
        ok, reset = available(q, threshold)
        if ok:
            why = "额度可用" if b == order[0] else f"{order[0]} 额度不足（已达 {threshold}%），改用 {b}"
            return {"backend": b, "reason": why, "quotas": quotas}
        waits.append((reset or "未知", b))
    waits.sort()
    return {"backend": None, "reason": f"两个后端额度都不足，最早 {waits[0][0]} 恢复（{waits[0][1]}）",
            "earliest": waits[0], "quotas": quotas}


# ---------------------------------------------------------------- mapping ----
def map_effort(effort: str | None, target: str) -> str | None:
    if not effort:
        return None
    e = effort.lower()
    if target == "claude":
        e = {"ultra": "ultracode", "minimal": "low", "none": "low"}.get(e, e)
        if e not in backend_mod("claude").EFFORTS:
            die(f"claude 后端不支持思考强度 {effort}（可选 {', '.join(backend_mod('claude').EFFORTS)}）")
        return e
    return {"ultracode": "ultra"}.get(e, e)


BRIEF_SECTIONS = ("## 任务目标", "## 已完成", "## 剩余工作", "## 关键判断与约束")

_TIER_RANK = {"read": 0, "strict": 1, "edit": 2, "auto": 3, "full": 4}


def no_wider(orig: str | None, fallback: str = "edit") -> str:
    """When a tier's authorization is gone, fall back to `fallback` but never above the original."""
    o = orig or fallback
    return o if _TIER_RANK.get(o, 2) <= _TIER_RANK[fallback] else fallback


HANDOFF_ACCESS = {  # source access -> target access when switching backends
    ("claude", "codex"): {"read": "read", "edit": "strict", "strict": "strict", "auto": "auto", "full": "full"},
    ("codex", "claude"): {"read": "read", "edit": "edit", "strict": "strict", "auto": "auto", "full": "full"},
}


def take_opt(args: list[str], names: tuple[str, ...], has_value: bool = True):
    """Remove an option (and its value) from args; return its value (or True)."""
    for i, a in enumerate(args):
        for n in names:
            if a == n:
                if not has_value:
                    del args[i]
                    return True
                if i + 1 >= len(args):
                    die(f"{n} 缺少值")
                v = args[i + 1]
                del args[i:i + 2]
                return v
            if has_value and a.startswith(n + "="):
                del args[i]
                return a.split("=", 1)[1]
    return None


# --------------------------------------------------------------- commands ----
def cmd_quota(a) -> int:
    names = list(BACKEND_FILES) if a.backend == "all" else [a.backend]
    out = [quota(b) for b in names]
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for q in out:
            ok, reset = available(q, a.threshold)
            print(fmt_quota(q) + ("" if ok else f"  → 不可用，{reset or '未知时间'} 恢复"))
    return 0


def cmd_pick(a) -> int:
    r = pick_backend(a.backend, a.model, a.prefer, a.threshold)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        for q in r["quotas"]:
            print(fmt_quota(q))
        print(f"选择: {r['backend'] or '无可用后端'}（{r['reason']}）")
    return 0 if r["backend"] else 5


def run_chain(backend: str, run_dir: Path) -> list[tuple[str, Path]]:
    """Follow resumed_from links back to the original run (oldest first)."""
    chain, seen = [], set()
    cur_b, cur = backend, run_dir
    while cur and cur not in seen:
        seen.add(cur)
        chain.append((cur_b, cur))
        try:
            meta = json.loads((cur / "meta.json").read_text())
        except (OSError, json.JSONDecodeError):
            break
        nxt = meta.get("resumed_from")
        if not nxt:
            marker = STATE_DIR / "handoffs" / f"{cur.name}.json"
            if marker.exists():
                try:
                    nxt = json.loads(marker.read_text()).get("from")
                except (OSError, json.JSONDecodeError):
                    nxt = None
        if not nxt:
            break
        cur_b, cur = find_run(nxt)
    return list(reversed(chain))


def git_state(cwd: str) -> str:
    try:
        st = subprocess.run(["git", "-C", cwd, "status", "--short"], capture_output=True, text=True, timeout=30)
        if st.returncode != 0:
            return "（不是 git 仓库）"
        df = subprocess.run(["git", "-C", cwd, "diff", "--stat"], capture_output=True, text=True, timeout=30)
        return f"git status --short:\n{st.stdout.strip() or '（无改动）'}\n\ngit diff --stat:\n{df.stdout.strip() or '（无）'}"
    except (OSError, subprocess.TimeoutExpired):
        return "（无法读取 git 状态）"


def decisions(run_dir: Path) -> list[str]:
    out = []
    ap = run_dir / "approvals"
    if not ap.is_dir():
        return out
    for p in sorted(ap.glob("*.decision.json")):
        rid = p.name.split(".")[0]
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            req = json.loads((ap / f"{rid}.request.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        inp = req.get("input") or {}
        what = inp.get("command") or inp.get("file_path") or json.dumps(inp, ensure_ascii=False)[:160]
        extra = d.get("message") or (json.dumps(d.get("answers"), ensure_ascii=False) if d.get("answers") else "")
        out.append(f"- {req.get('tool_name')}: {str(what)[:200]} → {d.get('behavior')}"
                   f"（{d.get('by')}）{('：' + extra) if extra else ''}")
    return out



def _brief(name: str, inp) -> str:
    if not isinstance(inp, dict):
        return name
    for k in ("command", "file_path", "path", "pattern", "url", "query", "description"):
        if inp.get(k):
            return f"{name}: {' '.join(str(inp[k]).split())[:140]}"
    return name


def extract_progress(backend: str, run_dir: Path, max_actions: int = 30) -> dict:
    """Read what the previous executor did from its event log (no model needed)."""
    actions, texts, plan = [], [], []
    by_id: dict = {}
    ev_path = run_dir / "events.jsonl"
    if not ev_path.exists():
        return {"actions": [], "progress": [], "texts": [], "plan": []}
    with ev_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if backend == "claude":
                if ev.get("parent_tool_use_id"):
                    continue
                if ev.get("type") == "user":  # tool results: attach outcome to the action
                    for b in (ev.get("message") or {}).get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                            c = b.get("content")
                            if isinstance(c, list):
                                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                            c = " ".join(str(c or "").split())
                            tag = "错误/被拒" if b.get("is_error") else "完成"
                            actions[by_id[b["tool_use_id"]]] += f"  → [{tag}] {c[:160]}" if c else f"  → [{tag}]"
                    continue
                if ev.get("type") != "assistant":
                    continue
                for b in (ev.get("message") or {}).get("content") or []:
                    if b.get("type") == "tool_use":
                        by_id[b.get("id")] = len(actions)
                        actions.append(_brief(b.get("name", "?"), b.get("input")))
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        texts.append(b["text"].strip())
            else:
                if ev.get("dir") != "in":
                    continue
                m, p = ev.get("method"), ev.get("params") or {}
                if m == "item/completed":
                    it = p.get("item") or {}
                    t = it.get("type")
                    if t == "commandExecution":
                        code = it.get("exitCode")
                        out = " ".join(str(it.get("aggregatedOutput") or "").split())
                        actions.append(f"[{'未执行/被拒' if code is None else code}] {' '.join(str(it.get('command')).split())[:140]}"
                                       + (f"  → {out[:160]}" if out else ""))
                    elif t == "fileChange":
                        actions.append("改文件: " + ", ".join(f"{(c.get('kind') or {}).get('type')} {c.get('path')}"
                                                         for c in it.get("changes") or []))
                    elif t == "agentMessage" and it.get("text"):
                        texts.append(it["text"].strip())
                elif m == "turn/plan/updated":
                    plan = [f"[{x.get('status')}] {x.get('step')}" for x in p.get("plan") or []]
    progress = [m.group(1).strip() for t in texts for m in re.finditer(r"^\s*PROGRESS:\s*(.+)$", t, re.M)]
    return {"actions": actions[-max_actions:], "n_actions": len(actions), "progress": progress[-10:],
            "texts": [t[-1200:] for t in texts[-2:]], "plan": plan}


def fmt_progress(chain) -> str:
    parts = []
    for b, rd in chain:
        pr = extract_progress(b, rd)
        if not (pr["actions"] or pr["progress"] or pr["texts"] or pr["plan"]):
            continue
        sec = [f"### [{b}:{rd.name}]"]
        if pr["plan"]:
            sec.append("计划（最后一次更新）:\n" + "\n".join(f"- {x}" for x in pr["plan"]))
        if pr["progress"]:
            sec.append("进度标记（PROGRESS 行）:\n" + "\n".join(f"- {x}" for x in pr["progress"]))
        if pr["actions"]:
            head = f"操作记录（共 {pr['n_actions']} 个，列出最后 {len(pr['actions'])} 个）:"
            sec.append(head + "\n" + "\n".join(f"- {x}" for x in pr["actions"]))
        if pr["texts"]:
            sec.append("最后的说明文字:\n" + "\n---\n".join(pr["texts"]))
        parts.append("\n\n".join(sec))
    return "\n\n".join(parts) if parts else "（日志里没有可用的进度记录）"


def cmd_handoff(argv: list[str]) -> int:
    args = list(argv)
    guard_sensitive(args, "handoff")
    if not args or args[0].startswith("-"):
        die("用法: handoff <运行名|last> --id <新运行名> [--to claude|codex] (--prepare | --brief-file <简报>) [--access ...] [-m 模型] [-e 强度] [--dry-run] [其他 run 参数]")
    src_b, src = find_run(args.pop(0))
    to = take_opt(args, ("--to",)) or ("codex" if src_b == "claude" else "claude")
    if to not in BACKEND_FILES:
        die("--to 取 claude|codex")
    dry = bool(take_opt(args, ("--dry-run",), has_value=False))
    prepare = bool(take_opt(args, ("--prepare",), has_value=False))
    if opt_value(args, ("--cwd", "--add-dir")) is not None:
        die("交接的工作目录与附加目录由原运行决定，不能用 --cwd / --add-dir 覆盖")
    brief_file = take_opt(args, ("--brief-file",))
    new_id = take_opt(args, ("--id",))
    if not new_id:
        die("handoff 需要 --id <新运行名>")
    valid_id(new_id)
    if id_taken(new_id) or X.rmeta_path(new_id).exists():
        die(f"--id {new_id} 已被占用")
    if backend_mod(src_b).pid_alive(src):
        die(f"{src.name} 还在运行，先等它结束（或用 decide all deny 结束）再交接")
    meta = json.loads((src / "meta.json").read_text())
    chain = run_chain(src_b, src)
    root_b, root = chain[0]
    original = (root / "prompt.md").read_text(encoding="utf-8") if (root / "prompt.md").exists() else "（未找到）"
    follow = []
    for b, rd in chain[1:]:
        t = (rd / "prompt.md").read_text(encoding="utf-8") if (rd / "prompt.md").exists() else ""
        if t.strip():
            follow.append(f"[{b}:{rd.name}] {t.strip()}")
    reports = []
    for b, rd in chain:
        t = (rd / "result.md").read_text(encoding="utf-8") if (rd / "result.md").exists() else ""
        if re.match(r"\s*(You've hit your|You have hit your|Usage limit)", t, re.I) and len(t) < 300:
            t = ""  # only the quota error, no real progress report
        if t.strip():
            reports.append(f"[{b}:{rd.name}]\n{t.strip()[-4000:]}")
    approvals = [x for _, rd in chain for x in decisions(rd)]
    cwd = meta["cwd"]
    hdir = STATE_DIR / "handoffs"
    hdir.mkdir(parents=True, exist_ok=True)
    stop = '额度用尽' if meta.get('usage_limited') else '超时' if meta.get('timed_out') else '已结束或中断'
    if prepare:
        # Step 1: extract the raw material only. The brief itself is written by an agent
        # (the caller), not concatenated by this script.
        material = f"""# 交接素材：{src_b}:{src.name} → {to}:{new_id}

> 这是脚本从运行记录中**机械提取**的素材，供撰写交接简报使用，不能直接交给 {to}。
> 上一个执行者状态：{stop}。工作目录：{cwd}

## A. 原任务简报（原文）
{original.strip()}
""" + (("\n## B. 后续追加的指示（按时间先后，原文）\n" + "\n\n".join(follow) + "\n") if follow else "\n## B. 后续追加的指示\n（无）\n") + f"""
## C. 执行记录（计划 / PROGRESS 进度行 / 命令与退出码 / 文件改动 / 最后的说明）
{fmt_progress(chain)}

## D. 上一个执行者的最终报告
{chr(10).join(reports) if reports else '（没有留下报告，可能在中途被打断）'}

## E. 审批决定（被拒绝的不能再尝试）
{chr(10).join(approvals) if approvals else '（无）'}

## F. 工作目录当前状态
{git_state(cwd)}
"""
        mp = hdir / f"{new_id}.material.md"
        mp.write_text(material, encoding="utf-8")
        print(f"交接素材: {mp}")
        print(f"请据此撰写交接简报，写到: {hdir / (new_id + '.brief.md')}")
        print(f"（必须包含小节 {', '.join(BRIEF_SECTIONS)}），然后执行: handoff {src.name} --id {new_id} --to {to} --brief-file <简报路径> ...")
        return 0
    if not brief_file:
        die("交接需要先写简报：先执行 handoff <运行> --id <新运行名> --prepare 取素材，"
            "由主会话据此撰写简报，再用 --brief-file <简报路径> 启动")
    bfp = Path(brief_file).expanduser()
    if not bfp.is_file():
        die(f"简报文件不存在: {bfp}")
    body = bfp.read_text(encoding="utf-8").strip()
    missing = [h for h in BRIEF_SECTIONS if h not in body]
    if missing or len(body) < 150:
        die(f"简报不完整：缺少小节 {missing or '（内容过短）'}。请按模板补全后重试")
    brief = f"""# 交接任务：从 {src_b} 切换到 {to} 继续同一个任务

上一个执行者（{src_b}）已停止（{stop}）。你接手**同一个任务**，在**同一个工作目录**（{cwd}）里继续。下面的简报由调用方根据上一个执行者的运行记录整理；以工作目录里的实际文件为准。

{body}

## 交接规则
1. 先核对工作目录的实际状态（读文件、git status / git diff）是否与简报一致；不一致时以实际文件为准，并在报告中说明。
2. 不要重复已完成的工作，不要回滚或覆盖上一个执行者的改动（除非明显错误，需在报告中说明）；简报中列为“被拒绝”的操作不要再尝试。
3. 最终报告覆盖整个任务：哪些是上一个执行者完成的、哪些是你完成的、改动的文件、验证结果、未完成项。
"""
    bp = hdir / f"{new_id}.md"
    bp.write_text(brief, encoding="utf-8")
    opts = meta.get("opts") or {}
    srm0 = X.load_rmeta(src.name)
    explicit_access = take_opt(args, ("--access", "-a"))
    src_access, h_from_defaults, h_defaults_sha = meta.get("access") or "edit", [], None
    if not explicit_access and "access" in (srm0.get("from_defaults") or []):
        cur = X.load_project_config(srm0.get("cwd") or meta["cwd"])
        if cur.get("sha256") == srm0.get("defaults_sha") and not (cur.get("untrusted") or cur.get("error")):
            h_from_defaults, h_defaults_sha = ["access"], srm0.get("defaults_sha")  # still authorized
        else:
            src_access = no_wider(src_access)
            note(f"原运行的档位来自项目默认参数，配置已变化或失去确认：交接按 {src_access} 换算（不会放宽）")
    access = explicit_access or HANDOFF_ACCESS[(src_b, to)].get(src_access, "strict")
    if access == "full" and "--confirm-full" not in args:
        access = "strict"
        note("原运行是 full 档；交接后降为 strict（需要 full 请显式加 --access full --confirm-full）")
    model_source = "explicit"
    if not opt_value(args, ("--model", "-m")):  # the source model belongs to the other backend
        d = default_model(to)
        if not d:
            die(f"交接目标 {to} 还没有设置默认模型，也没有用 -m 指定模型。{MODEL_SETUP_HINT}", 6)
        args += ["--model", d["model"]]
        model_source = "default"
        note(f"交接使用 {to} 的默认模型: {d['model']}")
    # the target's default model comes with its own default effort; the source effort (of another
    # backend's model) is only inherited when the caller chose the target model explicitly
    eff = take_opt(args, ("--effort", "-e")) or (default_model(to).get("effort") if model_source == "default"
                                                 else opts.get("effort"))
    run_args = ["run", "--id", new_id, "--cwd", cwd, "--prompt-file", str(bp), "--access", access]
    if eff:
        run_args += ["--effort", map_effort(eff, to)]
    if "--approval-timeout" not in args and opts.get("approval_timeout"):
        run_args += ["--approval-timeout", str(opts["approval_timeout"])]
    for d in meta.get("add_dirs") or []:
        run_args += ["--add-dir", d]
    run_args += args
    if dry:
        print(f"交接简报: {bp}\n命令: {to} " + " ".join(run_args))
        return 0
    # record the link for later chains / reports
    note(f"交接: {src_b}:{src.name} → {to}:{new_id}（档位 {access}，简报 {bp}）")
    claim_id(new_id)
    marker = STATE_DIR / "handoffs" / f"{new_id}.json"
    marker.write_text(json.dumps({"from": str(src), "from_backend": src_b, "to": to, "id": new_id,
                                  "at": _dt.datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False))
    srm = X.load_rmeta(src.name)
    orig = srm.get("cwd") or cwd
    ov = project_overlay(orig, to, access)
    bargs = overlay_args(ov, run_args[1:], srm.get("isolated"))
    verify, source = inherited_verify(srm, ov, opt_value(bargs, ("--access",)))
    return launch(to, "run", bargs, new_id, orig, cwd,
                  {"from": f"{src_b}:{src.name}", "switch": f"{src_b} → {to}", "isolated": srm.get("isolated"),
                   "group": srm.get("group"),
                   "verify": verify, "verify_source": source, "verify_timeout": ov["verify_timeout"],
                   "verify_sha": ov["sha256"] if source == "project" else None,
                   "verify_host": srm.get("verify_host") and source == "explicit",
                   "from_defaults": h_from_defaults, "defaults_sha": h_defaults_sha,
                   "access": opt_value(bargs, ("--access",)), "effort": eff, "model_source": model_source})




# ================================================================ launching ====
def opt_value(args: list[str], names: tuple[str, ...]):
    for i, x in enumerate(args):
        if x in names and i + 1 < len(args):
            return args[i + 1]
        for n in names:
            if x.startswith(n + "="):
                return x.split("=", 1)[1]
    return None


def set_opt(args: list[str], name: str, value: str) -> list[str]:
    take_opt(args, (name,))
    return [*args, name, value]


def safe_quota(b: str, home: str | None = None):
    try:
        return quota(b, home)
    except Exception as e:  # never let quota bookkeeping break a run
        return {"ok": False, "error": str(e), "backend": b}


def running_count(backend: str) -> int:
    n = 0
    if X.RMETA_DIR.exists():
        for p in X.RMETA_DIR.glob("*.json"):
            m = X.read_json(p, {}) or {}
            if m.get("backend") == backend and m.get("status") == "running":
                try:
                    os.kill(int(m.get("router_pid")), 0)
                    n += 1
                except (OSError, TypeError, ValueError):
                    pass
    return n


def backend_facts(backend: str, run_id: str) -> dict:
    """Model, tokens, cost, quota state and approval counts from the backend's run record."""
    rd = runs_dir(backend) / run_id
    meta = X.read_json(rd / "meta.json", {}) or {}
    f = {"usage_limited": bool(meta.get("usage_limited")), "reset_at": meta.get("reset_at"),
         "session": meta.get("session_id") or meta.get("thread_id"), "access": meta.get("access"),
         "effort": meta.get("effort"), "model_requested": meta.get("model"), "tokens": {}, "cost_usd": None}
    if backend == "claude":
        res = X.read_json(rd / "result.json", {}) or {}
        mu = res.get("modelUsage") or {}
        f["model_actual"] = ", ".join(mu) or None
        f["tokens"] = {"input": sum(u.get("inputTokens", 0) for u in mu.values()),
                       "cached": sum(u.get("cacheReadInputTokens", 0) for u in mu.values()),
                       "output": sum(u.get("outputTokens", 0) for u in mu.values())}
        f["cost_usd"] = res.get("total_cost_usd")
    else:
        tu = (meta.get("token_usage") or {}).get("total") or {}
        f["model_actual"] = meta.get("actual_model")
        f["tokens"] = {"input": tu.get("inputTokens"), "cached": tu.get("cachedInputTokens"),
                       "output": tu.get("outputTokens"), "reasoning": tu.get("reasoningOutputTokens")}
    ap = rd / "approvals"
    counts = {"批准": 0, "拒绝": 0, "规则自动": 0, "用户决定": 0, "超时": 0}
    if ap.is_dir():
        for p in ap.glob("*.decision.json"):
            d = X.read_json(p, {}) or {}
            counts["批准" if d.get("behavior") in ("allow", "answer") else "拒绝"] += 1
            counts["规则自动"] += d.get("by") == "rule"
            counts["用户决定"] += d.get("by") == "user"
            counts["超时"] += d.get("by") == "timeout"
        log = ap / "log.jsonl"
        if log.exists():  # claude approval bridge logs rule hits instead of decision files
            for line in log.read_text(encoding="utf-8").splitlines():
                if '"auto": true' in line:
                    counts["规则自动"] += 1
                    counts["拒绝" if '"denied": true' in line else "批准"] += 1
    f["approvals"] = counts
    return f


STATUS = {0: "成功", 1: "出错", 2: "参数错误", 3: "超时", 4: "CLI/登录问题", 5: "额度用尽", 6: "待确认", 7: "验证失败"}


def project_overlay(cwd: str, backend: str, access: str | None) -> dict:
    """The CURRENT trusted project config, re-read for every run / resume / handoff (never inherited)."""
    cfg = X.load_project_config(cwd)
    out = {"rules": [], "verify": None, "verify_timeout": None, "defaults": {}, "force_access": None,
           "path": cfg.get("path"), "sha256": cfg.get("sha256")}
    if cfg.get("error"):
        note(f"项目配置 {cfg['path']} 未生效: {cfg['error']}")
        return out
    if cfg.get("untrusted"):
        note(f"项目配置 {cfg['path']} 尚未确认，本次不生效（规则、验证命令、默认参数都不用）。"
             f"用户同意后执行 rules --cwd <目录> --trust --sha <预览时显示的 sha256>；文件改动后需要重新确认")
        return out
    if not cfg:
        return out
    out.update(rules=cfg.get("rules") or [], defaults=cfg.get("defaults") or {},
               verify=(cfg.get("verify") or {}).get("command"), verify_timeout=(cfg.get("verify") or {}).get("timeout"))
    has_deny = any(r.get("decision") == "deny" for r in out["rules"])
    file_deny = any(r.get("decision") == "deny" and r.get("tool") == "FILE" for r in out["rules"])
    if backend == "claude" and file_deny and (access or "edit") in ("edit", "auto", "full"):
        # acceptEdits/auto let shell commands write without a prompt: protected paths need every
        # write and side-effecting command to go through approval
        out["force_access"] = "strict"
        note(f"项目有受保护路径，claude 的 {access or 'edit'} 档无法完全拦截命令写入，本次改用 strict 档")
    if backend == "codex" and has_deny and (access or "edit") in ("edit", "auto", "full"):
        # codex lets workspace commands/writes run without an approval request, so deny rules could not
        # be enforced there; strict routes every write and side-effecting command through approval.
        out["force_access"] = "strict"
        note(f"项目有拒绝规则，codex 的 {access or 'edit'} 档无法强制执行它们，本次改用 strict 档")
    return out


def map_rules_to_worktree(rules: list, iso: dict | None) -> list:
    """FILE rules are absolute paths of the original repo; an isolated run works in the worktree,
    so add the same rule for the corresponding worktree path (originals are kept as well)."""
    if not iso:
        return rules
    root, wt = str(Path(iso["root"]).resolve()), str(Path(iso["worktree"]).resolve())
    out = list(rules)
    for r in rules:
        d = r.get("dir")
        if r.get("tool") != "FILE" or not d:
            continue
        if d == root or d.startswith(root + os.sep):
            out.append({**r, "dir": wt + d[len(root):]})
        elif root.startswith(d.rstrip(os.sep) + os.sep):  # rule covers an ancestor of the project root
            out.append({**r, "dir": wt})
    return out


def check_dry_run(args: list[str]) -> bool:
    return "--dry-run" in args


def launch(backend: str, sub: str, bargs: list[str], run_id: str, orig_cwd: str, exec_cwd: str, info: dict) -> int:
    """Run a backend subcommand as a child so snapshots, verification and the ledger wrap it."""
    iso = info.get("isolated")
    snap_dir = iso["worktree"] if iso else exec_cwd
    rm = {"id": run_id, "backend": backend, "sub": sub, "cwd": orig_cwd, "exec_cwd": exec_cwd,
          "snap_dir": snap_dir, "started": X.now_iso(), "router_pid": os.getpid(), "status": "running",
          "isolated": iso, "group": info.get("group"), "from": info.get("from"), "switch": info.get("switch"),
          "verify_cmd": info.get("verify"), "verify_source": info.get("verify_source"),
          "verify_host": bool(info.get("verify_host")), "queue_job": os.environ.get("CLI_DELEGATE_QUEUE_JOB"),
          "from_defaults": info.get("from_defaults") or [], "defaults_sha": info.get("defaults_sha"),
          "model_source": info.get("model_source")}
    # snapshot or not: one writer per directory (undo / merge rely on this)
    holder = X.dir_lock(snap_dir, run_id)
    if holder:
        die(f"{snap_dir} 正被运行 {holder} 使用；同一目录同时只能跑一个任务"
            "（否则快照和撤销会混入彼此的改动）。请等它结束、用 queue 排队，或加 --isolate", 8)
    locked = True
    if not info.get("no_snapshot"):
        sb = X.snapshot(snap_dir, run_id, "before")
        rm["snapshot"] = "ok" if sb.get("ok") else f"未拍（{sb.get('error')}）"
        if not sb.get("ok"):
            note(f"快照跳过: {sb.get('error')}")
    else:
        rm["snapshot"] = "已关闭"
    try:
        home = opt_value(bargs, ("--codex-home",)) if backend == "codex" else None
        rm["codex_home"] = home
        rm["quota_before"] = info.get("quota_before") or safe_quota(backend, home)
        X.save_rmeta(run_id, rm)
        t0 = time.time()
        fd = X.lock_fd(snap_dir) if locked else None
        try:
            # the backend inherits the lock descriptor: the lock lasts while the backend lives,
            # even if this wrapper dies first
            benv = {k: v for k, v in os.environ.items() if not k.startswith("CLI_DELEGATE_QUEUE_")}
            if fd is not None:
                benv["CLI_DELEGATE_LOCK_FD"] = str(fd)
            rc = subprocess.run([sys.executable, str(BACKEND_FILES[backend]), sub, *bargs],
                                pass_fds=(fd,) if fd is not None else (), env=benv).returncode
        except KeyboardInterrupt:
            rc = 130
        facts = backend_facts(backend, run_id)
        verify = None
        if rc == 0 and info.get("verify") and info.get("verify_source") == "project":
            cur = X.load_project_config(orig_cwd)
            if cur.get("sha256") != info.get("verify_sha") or cur.get("untrusted") or cur.get("error"):
                note("项目配置在运行期间变化或失去确认：项目验证未执行，本次结果记为验证失败")
                verify = {"command": info["verify"], "rc": -4, "ok": False, "where": "未执行", "seconds": 0,
                          "tail": "项目配置在运行期间被修改或失去确认，验证命令未执行"}
                info = {**info, "verify": None}
                rc = 7
        if rc == 0 and info.get("verify"):
            note(f"运行验证命令: {info['verify']}")
            verify = X.run_verify(info["verify"], exec_cwd, int(info.get("verify_timeout") or 900),
                                  codex_bin=_codex_bin(), host=bool(info.get("verify_host")))
            if not verify["ok"]:
                rc = 7
        if rm["snapshot"] == "ok":  # after verification, so files it writes are part of this run's change set
            sa = X.snapshot(snap_dir, run_id, "after")
            if not sa.get("ok"):
                rm["snapshot"] = f"只有运行前快照（{sa.get('error')}）"
    finally:
        if locked:
            X.dir_unlock(snap_dir, run_id)
    rm.update({"status": "finished", "rc": rc, "finished": X.now_iso(), "seconds": round(time.time() - t0, 1),
               "quota_after": safe_quota(backend, rm.get("codex_home")), "verify": verify})
    X.save_rmeta(run_id, rm)
    entry = {"id": run_id, "time": rm["finished"], "backend": backend, "sub": sub, "rc": rc,
             "status": STATUS.get(rc, f"exit {rc}"), "cwd": orig_cwd, "exec_cwd": exec_cwd,
             "from": rm["from"], "switch": rm["switch"], "seconds": rm["seconds"],
             "quota_before": rm["quota_before"], "quota_after": rm["quota_after"], "verify": verify,
             "isolated": iso, "snapshot": rm["snapshot"], **facts}
    entry["access"] = facts.get("access") or info.get("access")
    entry["effort"] = facts.get("effort") or info.get("effort")
    entry["model_source"] = info.get("model_source")
    X.ledger_append(entry)
    if verify:
        print(f"\n=== 验证 ===\n`{verify['command']}` → {'通过' if verify['ok'] else '失败'}（rc {verify['rc']}，{verify.get('where')}）\n{verify['tail'][-1500:]}")
    print("\n" + X.fmt_ledger(entry), flush=True)
    return rc


def _codex_bin() -> str | None:
    try:
        return backend_mod("codex").find_bin()
    except SystemExit:
        return None


def overlay_args(ov: dict, args: list[str], iso: dict | None = None) -> list[str]:
    """Replace any inherited rules with the current ones and apply a forced tier."""
    take_opt(args, ("--rules-json",))
    args += ["--rules-json", json.dumps(map_rules_to_worktree(ov["rules"], iso), ensure_ascii=False)]
    if ov["force_access"]:
        take_opt(args, ("--access", "-a"))
        args += ["--access", ov["force_access"]]
    return args


# ============================================================ run / resume ====
SENSITIVE = ("--verify-host", "--confirm-full", "--allow-push")


def guard_sensitive(args: list[str], where: str) -> None:
    """Options that widen what runs on the host or what the child may do are accepted only via the
    `unsafe` prefix, so a permission allow-rule for `cli_delegate.py run *` cannot cover them."""
    if os.environ.get("CLI_DELEGATE_UNSAFE_OK") == "1":
        return
    bad = [x for x in args if x.split("=")[0] in SENSITIVE] + \
        (["--access full"] if (opt_value(args, ("--access", "-a")) == "full") else [])
    if bad:
        die(f"{where} 带有敏感参数 {', '.join(bad)}：须用户同意后改用 `cli_delegate.py unsafe {where} ...` 执行")


def cmd_run(argv: list[str]) -> int:
    args = list(argv)
    guard_sensitive(args, "run")
    backend = take_opt(args, ("--backend", "-b"))
    prefer = take_opt(args, ("--prefer",)) or "claude"
    threshold = int(take_opt(args, ("--threshold",)) or 100)
    isolate = bool(take_opt(args, ("--isolate",), has_value=False))
    group = take_opt(args, ("--group",))  # one lane of a parallel batch: always its own worktree
    if group:
        valid_id(group, "并发组名")
        isolate = True
    verify = take_opt(args, ("--verify",))
    verify_host = bool(take_opt(args, ("--verify-host",), has_value=False))
    no_snap = bool(take_opt(args, ("--no-snapshot",), has_value=False))
    max_par = int(take_opt(args, ("--max-parallel",)) or 0)
    cwd = str(Path(opt_value(args, ("--cwd",)) or os.getcwd()).expanduser().resolve())
    dry = check_dry_run(args)
    pre = X.load_project_config(cwd)
    dfl = (pre.get("defaults") or {}) if not (pre.get("untrusted") or pre.get("error")) else {}
    backend = backend or dfl.get("backend") or "auto"
    from_defaults = []
    for opt, key in (("--access", "access"), ("--model", "model"), ("--effort", "effort"),
                     ("--approval-timeout", "approval_timeout")):
        short = {"--access": "-a", "--model": "-m", "--effort": "-e"}.get(opt)
        if dfl.get(key) is not None and opt_value(args, (opt, short) if short else (opt,)) is None:
            args += [opt, str(dfl[key])]
            from_defaults.append(key)
    if backend not in ("auto", *BACKEND_FILES) or prefer not in BACKEND_FILES:
        die("--backend 取 auto|claude|codex，--prefer 取 claude|codex")
    model = opt_value(args, ("--model", "-m"))
    home = opt_value(args, ("--codex-home",))
    if dry:  # no quota probe, worktree, verify or state: show what the backend would run
        if isolate:
            note(f"（dry-run 不创建 worktree）真实运行会在隔离 worktree 里执行"
                 f"{f'，并发组 {group}' if group else ''}，不是原目录 {cwd}")
        b = backend if backend != "auto" else (model_backend(model) or prefer)
        args, _ = apply_default_model(args, b)
        e = take_opt(args, ("--effort", "-e"))
        if e:
            args += ["--effort", map_effort(e, b)]
        args = overlay_args(project_overlay(cwd, b, opt_value(args, ("--access", "-a"))), args)
        exec_backend(b, ["run", *args])
    fixed = model_backend(model) or (backend if backend != "auto" else None)
    if not model and fixed and missing_defaults([fixed]):  # fail before the quota probes
        apply_default_model(args, fixed)
    r = pick_backend(backend, model, prefer, threshold, home)
    for q in r["quotas"]:
        note(fmt_quota(q))
    if not r["backend"]:
        die(r["reason"], 5)
    b = r["backend"]
    note(f"后端: {b}（{r['reason']}）")
    args, model_source = apply_default_model(args, b)
    if model_source == "explicit" and "model" in from_defaults:
        model_source = "project"
    eff = take_opt(args, ("--effort", "-e"))
    if eff:
        args += ["--effort", map_effort(eff, b)]
    ov = project_overlay(cwd, b, opt_value(args, ("--access", "-a")))
    if max_par and running_count(b) >= max_par:
        die(f"{b} 后端已有 {running_count(b)} 个任务在运行（上限 {max_par}），请改用 queue add 排队", 8)
    rid = take_opt(args, ("--id",)) or f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    claim_id(rid)
    if ov["rules"]:
        note(f"项目规则: {ov['path']}（{len(ov['rules'])} 条）")
    source = "explicit" if verify else None
    if not verify and (opt_value(args, ("--access", "-a")) or "edit") != "read" and ov["verify"]:
        verify, source = ov["verify"], "project"  # project verify only for runs that can change files
    iso, exec_cwd = None, cwd
    if isolate:
        try:
            iso = X.make_worktree(cwd, rid)
        except RuntimeError as e:
            die(str(e))
        exec_cwd = iso["exec_cwd"]
        note(f"隔离运行: worktree {iso['worktree']}（分支 {iso['branch']}）")
    args = overlay_args(ov, args, iso)
    args = set_opt(args, "--cwd", exec_cwd)
    q_before = next((q for q in r["quotas"] if q.get("backend") == b), None)
    return launch(b, "run", ["--id", rid, *args], rid, cwd, exec_cwd,
                  {"isolated": iso, "group": group, "verify": verify, "verify_source": source, "verify_host": verify_host,
                   "verify_sha": ov["sha256"] if source == "project" else None,
                   "from_defaults": from_defaults, "defaults_sha": pre.get("sha256") if from_defaults else None,
                   "verify_timeout": ov["verify_timeout"], "no_snapshot": no_snap, "quota_before": q_before,
                   "access": opt_value(args, ("--access", "-a")), "effort": opt_value(args, ("--effort",)),
                   "model_source": model_source})


def inherited_verify(srm: dict, ov: dict, access: str | None):
    """Explicit --verify of the original run is kept; a project verify is re-read from the current config."""
    if srm.get("verify_source") == "explicit":
        return srm.get("verify_cmd"), "explicit"
    if ov["verify"] and (access or "edit") != "read":
        return ov["verify"], "project"
    return None, None


def cmd_resume(argv: list[str]) -> int:
    args = list(argv)
    guard_sensitive(args, "resume")
    wait = bool(take_opt(args, ("--wait",), has_value=False))
    max_waits = int(take_opt(args, ("--max-waits",)) or 3)
    if not wait:
        return resume_once(args)
    # every attempt is a registered run of its own (snapshot, lock, current rules, ledger)
    run = args[0] if args and not args[0].startswith("-") else None
    base = take_opt(args, ("--id",))
    rest = args[1:] if run else args
    src = run
    for attempt in range(max_waits + 1):
        b, rd = find_run(src)
        bmeta = X.read_json(rd / "meta.json", {}) or {}
        if attempt > 0 and not bmeta.get("reset_at"):
            note("没有可用的额度重置时间，停止自动重试；请稍后手动 resume")
            return 5
        if bmeta.get("reset_at"):
            target = _dt.datetime.fromisoformat(bmeta["reset_at"]) + _dt.timedelta(seconds=90)
            secs = (target - _dt.datetime.now().astimezone()).total_seconds()
            if secs > 0:
                note(f"等待额度重置：{target.isoformat(timespec='minutes')}（约 {secs / 60:.0f} 分钟）")
                time.sleep(secs)
        new_id = (base or f"{rd.name}-r1") if attempt == 0 else f"{base or rd.name}-w{attempt}"
        print(f"[{ME}] 本次续跑运行名: {new_id}（watch / decide 用这个名字）", flush=True)
        rc = resume_once([rd.name, "--id", new_id, *rest])
        if rc != 5:
            return rc
        src = new_id
    return 5


def resume_once(argv: list[str]) -> int:
    args = list(argv)
    run = args.pop(0) if args and not args[0].startswith("-") else None
    b, rd = find_run(run)
    if check_dry_run(args):
        exec_backend(b, ["resume", rd.name, *args])
    new_id = take_opt(args, ("--id",))
    if not new_id:
        k = 1
        while id_taken(f"{rd.name}-r{k}"):
            k += 1
        new_id = f"{rd.name}-r{k}"
    claim_id(new_id)
    eff = take_opt(args, ("--effort", "-e"))
    if eff:
        args += ["--effort", map_effort(eff, b)]
    srm = X.load_rmeta(rd.name)
    bmeta = X.read_json(rd / "meta.json", {}) or {}
    exec_cwd = bmeta.get("cwd") or srm.get("exec_cwd")
    orig = srm.get("cwd") or exec_cwd
    access = opt_value(args, ("--access",)) or bmeta.get("access")
    from_defaults, defaults_sha = [], None
    if "access" in (srm.get("from_defaults") or []) and not opt_value(args, ("--access",)):
        from_defaults, defaults_sha = ["access"], srm.get("defaults_sha")  # inherited: keep its origin
        cur = X.load_project_config(orig)
        if cur.get("sha256") != srm.get("defaults_sha") or cur.get("untrusted") or cur.get("error"):
            # the tier came from project defaults that changed or lost trust: do not inherit it
            cand = ((cur.get("defaults") or {}).get("access") if not (cur.get("untrusted") or cur.get("error"))
                    else None) or "edit"
            new_access = cand if _TIER_RANK.get(cand, 2) <= _TIER_RANK.get(access or "edit", 2) else no_wider(access)
            note(f"原运行的档位来自项目默认参数，配置已变化：续跑改用 {new_access}")
            args += ["--access", new_access]
            access = new_access
            from_defaults = ["access"] if new_access != "edit" or (cur.get("defaults") or {}).get("access") else []
            defaults_sha = cur.get("sha256") if from_defaults else None
    ov = project_overlay(orig, b, access)
    args = overlay_args(ov, args, srm.get("isolated"))
    verify, source = inherited_verify(srm, ov, access)
    msrc = "explicit" if opt_value(args, ("--model", "-m")) else srm.get("model_source")  # resume keeps the model
    return launch(b, "resume", [rd.name, "--id", new_id, *args], new_id, orig, exec_cwd,
                  {"from": f"{b}:{rd.name}", "isolated": srm.get("isolated"), "group": srm.get("group"), "verify": verify,
                   "verify_source": source, "verify_host": srm.get("verify_host") and source == "explicit",
                   "verify_timeout": ov["verify_timeout"], "verify_sha": ov["sha256"] if source == "project" else None,
                   "from_defaults": from_defaults, "defaults_sha": defaults_sha, "model_source": msrc})


def cmd_forward(sub: str, argv: list[str]) -> int:
    args = list(argv)
    run = args[0] if args and not args[0].startswith("-") else None
    if sub == "watch" and run and run != "last":
        for _ in range(120):  # a run started a moment ago may not have its directory yet
            if Path(run).expanduser().is_dir() or any((runs_dir(b) / run).is_dir() for b in BACKEND_FILES):
                break
            time.sleep(0.5)
    b, rd = find_run(run)
    if run is None:
        args = [rd.name, *args]
    else:
        args[0] = rd.name
    if sub != "watch":
        exec_backend(b, [sub, *args])
        return 0
    rc = subprocess.run([sys.executable, str(BACKEND_FILES[b]), "watch", *args]).returncode
    if rc in (0, 1) and X.rmeta_path(rd.name).exists():
        # the backend is done; wait for the wrapper's verification + ledger, then show the ledger
        for _ in range(1800):
            rm = X.load_rmeta(rd.name)
            if rm.get("status") != "running":
                break
            try:
                os.kill(int(rm.get("router_pid")), 0)
            except (OSError, TypeError, ValueError):
                break
            time.sleep(0.5)
        rm = X.load_rmeta(rd.name)
        hit = [e for e in X.ledger_entries() if e.get("id") == rd.name]
        if rm.get("verify"):
            v = rm["verify"]
            print(f"\n=== 验证 ===\n`{v['command']}` → {'通过' if v['ok'] else '失败'}（rc {v['rc']}）\n{v['tail'][-1500:]}")
        if hit:
            print("\n" + X.fmt_ledger(hit[-1]))
        if rm.get("status") == "finished" and rm.get("rc") is not None:
            rc = 0 if rm["rc"] == 0 else 1  # the wrapper's final status (incl. verification) decides
        elif rm.get("status") == "running":
            print("包装进程已退出但没有结算（可能被中断）；结果以后端摘要为准，验证与账本缺失")
            rc = 1
    return rc


# ======================================================= ledger / snapshots ====
def cmd_ledger(a) -> int:
    es = X.ledger_entries()
    if a.run:
        hit = [e for e in es if e.get("id") == a.run]
        if not hit:
            die(f"账本里没有 {a.run}（运行可能还没结束）")
        print(json.dumps(hit[-1], ensure_ascii=False, indent=2) if a.json else X.fmt_ledger(hit[-1]))
        return 0
    since = _dt.datetime.now() - _dt.timedelta(days=a.days)
    es = [e for e in es if e.get("time", "") >= since.isoformat()]
    if a.json:
        print(json.dumps(es, ensure_ascii=False, indent=2))
        return 0
    if not es:
        print(f"最近 {a.days} 天没有记录")
        return 0
    groups: dict = {}
    for e in es:
        g = groups.setdefault((e["time"][:10], e["backend"]), {"n": 0, "ok": 0, "lim": 0, "sw": 0, "in": 0, "out": 0, "usd": 0.0, "sec": 0.0})
        t = e.get("tokens") or {}
        g["n"] += 1
        g["ok"] += e.get("rc") == 0
        g["lim"] += bool(e.get("usage_limited"))
        g["sw"] += bool(e.get("switch"))
        g["in"] += t.get("input") or 0
        g["out"] += t.get("output") or 0
        g["usd"] += e.get("cost_usd") or 0
        g["sec"] += e.get("seconds") or 0
    print(f"{'日期':10} {'后端':6} {'次数':>4} {'成功':>4} {'撞限':>4} {'切换':>4} {'输入tok':>10} {'输出tok':>9} {'折算$':>8} {'耗时min':>7}")
    for (day, b), g in sorted(groups.items()):
        print(f"{day:10} {b:6} {g['n']:>4} {g['ok']:>4} {g['lim']:>4} {g['sw']:>4} {g['in']:>10} {g['out']:>9} "
              f"{g['usd']:>8.2f} {g['sec'] / 60:>7.1f}")
    last = {}
    for e in es:
        last[e["backend"]] = e.get("quota_after")
    for b, q in last.items():
        print(f"最近一次运行后 {b} 额度: {X.quota_brief(q)}")
    return 0


def cmd_result(a) -> int:
    """Path + sha256 of a run's final report, so the caller can relay it verbatim (e.g. a review)."""
    import hashlib
    b, rd = find_run(a.run)
    rp = rd / "result.md"
    if not rp.exists():
        die(f"{rd.name} 还没有最终报告（运行未结束或没有产出）")
    data = rp.read_bytes()
    print(f"后端: {b}\n报告文件: {rp}\nsha256: {hashlib.sha256(data).hexdigest()}\n字节数: {len(data)}")
    return 0


def _rmeta_or_die(run: str) -> dict:
    m = X.load_rmeta(valid_id(run))
    if not m:
        die(f"{run} 不是经统一入口运行的（没有快照/隔离记录）")
    return m


def cmd_diff(a) -> int:
    m = _rmeta_or_die(a.run)
    try:
        print(X.snap_diff(m["snap_dir"], a.run, stat=a.stat) or "（没有改动）")
    except RuntimeError as e:
        die(str(e))
    return 0


def cmd_undo(a) -> int:
    m = _rmeta_or_die(a.run)
    if m.get("isolated"):
        die("这是隔离运行，原目录没有被改动；不要它的结果就用 discard")
    if m.get("status") == "running":
        die("运行还没结束")
    holder = X.dir_lock(m["snap_dir"], f"undo-{a.run}")
    if holder:
        die(f"{m['snap_dir']} 正被运行 {holder} 使用，等它结束再撤销")
    try:
        since = _dt.datetime.fromisoformat(m["started"]).timestamp() if m.get("started") else None
        r = X.snap_undo(m["snap_dir"], a.run, force=a.force, since=since)
    except RuntimeError as e:
        die(str(e))
    finally:
        X.dir_unlock(m["snap_dir"])
    print(f"已撤销 {a.run} 的改动：恢复 {len(r['restored'])} 个文件，删除 {len(r['removed'])} 个新增文件")
    for x in r["restored"][:30]:
        print("  恢复 " + x)
    for x in r["removed"][:30]:
        print("  删除 " + x)
    for x in r.get("kept", [])[:30]:
        print("  保留（运行前就存在，只是当时被忽略） " + x)
    print(f"撤销前的状态保存在影子仓库 {r['redo_ref']}，需要时可以找回")
    return 0


def active_users(worktree: str) -> list:
    """Runs (original, resumes, handoffs) still working in this isolated worktree."""
    out = []
    if X.RMETA_DIR.exists():
        for p in X.RMETA_DIR.glob("*.json"):
            m = X.read_json(p, {}) or {}
            if (m.get("isolated") or {}).get("worktree") == worktree and m.get("status") == "running":
                try:
                    os.kill(int(m.get("router_pid")), 0)
                    out.append(m.get("id"))
                except (OSError, TypeError, ValueError):
                    pass
    return out


def cmd_merge(a) -> int:
    m = _rmeta_or_die(a.run)
    iso = m.get("isolated")
    if not iso:
        die("这不是隔离运行，改动已经在原目录里")
    holder = X.dir_lock(iso["worktree"], f"merge-{a.run}")  # same lock a run in this worktree holds
    if holder:
        die(f"这个隔离 worktree 正被运行 {holder} 使用，等它结束再合并")
    tholder = X.dir_lock(iso["root"], f"merge-{a.run}")  # and the target: no run / undo there meanwhile
    if tholder:
        X.dir_unlock(iso["worktree"])
        die(f"目标目录 {iso['root']} 正被运行 {tholder} 使用，等它结束再合并")
    try:
        print(X.merge_worktree(iso, a.run, three_way=a.three_way))
        if not a.keep:
            probs = X.remove_worktree(iso)
            if probs:
                print("改动已合并，但清理未完成（可以稍后执行 discard 重试）：" + "；".join(probs))
            else:
                print(f"已删除 worktree 与分支 {iso['branch']}")
    except RuntimeError as e:
        die(str(e))
    finally:
        X.dir_unlock(iso["worktree"])
        X.dir_unlock(iso["root"])
    return 0


def cmd_discard(a) -> int:
    m = _rmeta_or_die(a.run)
    iso = m.get("isolated")
    if not iso:
        die("这不是隔离运行；要回退改动用 undo")
    holder = X.dir_lock(iso["worktree"], f"discard-{a.run}")
    if holder:
        die(f"这个隔离 worktree 正被运行 {holder} 使用，不能丢弃")
    try:
        probs = X.remove_worktree(iso)
    finally:
        X.dir_unlock(iso["worktree"])
    if probs:
        die("丢弃未完成：" + "；".join(probs))
    print(f"已丢弃 {a.run} 的隔离结果（删除 worktree {iso['worktree']} 与分支 {iso['branch']}）")
    return 0


def cmd_rules(a) -> int:
    cfg = X.load_project_config(a.cwd, require_trust=False)
    if not cfg:
        print(f"{a.cwd} 及上级目录没有 .cli-delegate.toml")
        return 0
    path = Path(cfg["path"])
    if a.trust:
        if not a.sha:
            die("--trust 需要 --sha <预览时显示的 sha256（至少 12 位）>，确保确认的就是用户看过的那一版")
        if cfg.get("error"):
            die(f"配置有错误，不能确认: {cfg['error']}")
        try:
            print(f"已确认 {path}（sha256 {X.trust_config(path, a.sha)[:12]}…）；文件再改动需要重新确认")
        except RuntimeError as e:
            die(str(e))
    trusted = X.is_trusted(path, cfg.get("sha256"))
    print(f"配置: {path}\nsha256: {cfg.get('sha256')}\n状态: "
          + ("已确认，生效中" if trusted else "未确认，不生效（给用户看过下面的内容、用户同意后：rules --cwd <目录> --trust --sha <上面的 sha256>）"))
    if cfg.get("error"):
        print("错误:", cfg["error"])
        return 1
    for r in cfg["rules"]:
        what = r.get("prefix") or r.get("dir")
        print(f"  {'自动批准' if r['decision'] == 'allow' else '自动拒绝'} {r['tool']}: {what}")
    if cfg.get("verify"):
        print(f"  验证命令: {cfg['verify'].get('command')}")
    if cfg.get("defaults"):
        print(f"  默认参数: {json.dumps(cfg['defaults'], ensure_ascii=False)}")
    return 0


# ==================================================================== queue ====
QDIR = STATE_DIR / "queue"
Q_TERMINAL = {"done", "failed", "cancelled", "needs_handoff", "needs_answer"}


class QLock:
    """Exclusive lock around every read-modify-write of queue job files."""

    def __enter__(self):
        import fcntl
        QDIR.mkdir(parents=True, exist_ok=True)
        self.f = (QDIR / "queue.lock").open("a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def q_jobs() -> list:
    QDIR.mkdir(parents=True, exist_ok=True)
    jobs = [X.read_json(p, None) for p in QDIR.glob("*.job.json")]
    return sorted([j for j in jobs if j], key=lambda j: j.get("created", ""))


def q_load(job_id: str):
    return X.read_json(QDIR / f"{job_id}.job.json", None)


def q_save(job: dict) -> None:
    X.write_json(QDIR / f"{job['id']}.job.json", job)


def q_worker_alive() -> bool:
    """The worker holds an flock on worker.lock for its whole life; probing it is race-free."""
    import fcntl
    QDIR.mkdir(parents=True, exist_ok=True)
    with (QDIR / "worker.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def q_alive(job: dict) -> bool:
    pid = job.get("pid")
    if pid:
        try:  # reap our own finished children first, otherwise they linger as zombies
            done, status = os.waitpid(int(pid), os.WNOHANG)
            if done == int(pid):
                job["_exit"] = os.waitstatus_to_exitcode(status)
                return False
        except ChildProcessError:
            pass  # not our child (started by an earlier worker)
        except (TypeError, ValueError):
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    rm = X.load_rmeta(job["runs"][-1]) if job.get("runs") else {}  # follow-up registered via `queue ack --run`
    if rm.get("status") != "running":
        return False
    try:
        os.kill(int(rm.get("router_pid")), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def job_model_backend(args: list[str]) -> str | None:
    """Backend a job is bound to, resolved like a direct run: explicit --backend, else the trusted
    project default backend, else the backend of the explicit / default model."""
    b = opt_value(args, ("--backend", "-b"))
    if b and b != "auto":
        return b
    cfg = X.load_project_config(opt_value(args, ("--cwd",))) if opt_value(args, ("--cwd",)) else {}
    dfl = (cfg.get("defaults") or {}) if cfg and not (cfg.get("untrusted") or cfg.get("error")) else {}
    hinted = model_backend(opt_value(args, ("--model", "-m")) or dfl.get("model"))
    if hinted:
        return hinted  # the model fixes the backend (a default backend must not contradict it)
    if b == "auto":
        return None  # explicitly auto: no project default backend
    return dfl.get("backend") if dfl.get("backend") in BACKEND_FILES else None


def job_home(job: dict) -> str | None:
    """Codex account of the job's LATEST run (a handoff may have changed it), else of the job args."""
    if job.get("runs") and job.get("chosen") == "codex":
        bm = X.read_json(runs_dir("codex") / job["runs"][-1] / "meta.json", {}) or {}
        if bm:  # the latest run's ACTUAL account dir (incl. a CODEX_HOME taken from the environment)
            return bm.get("codex_home") or (bm.get("opts") or {}).get("codex_home")
    return opt_value(job.get("args") or [], ("--codex-home",))


def cmd_queue(argv: list[str]) -> int:
    if not argv:
        die("用法: queue add|work|watch|list|cancel|ack ...")
    sub, args = argv[0], list(argv[1:])
    if sub == "add":
        if backend_mod("claude").current_depth() >= 1:
            die("已经处在委派的子会话里，拒绝再排队新任务（禁止嵌套）")
        allow_switch = take_opt(args, ("--allow-switch",)) or "先问"
        if "--" in args:
            args = args[args.index("--") + 1:]
        rid = opt_value(args, ("--id",))
        if not rid:
            rid = f"q{_dt.datetime.now().strftime('%m%d-%H%M%S')}-{secrets.token_hex(2)}"
            args += ["--id", rid]
        if not opt_value(args, ("--cwd",)):
            die("排队任务必须写 --cwd")
        backend = opt_value(args, ("--backend", "-b")) or "auto"
        hinted = job_model_backend(args)
        if hinted and backend not in ("auto", hinted):
            die(f"模型属于 {hinted}，与 --backend {backend} 不符")
        guard_sensitive(args, "queue add")
        cfg = X.load_project_config(opt_value(args, ("--cwd",)))
        pdfl = (cfg.get("defaults") or {}) if not (cfg.get("untrusted") or cfg.get("error")) else {}
        if not (opt_value(args, ("--model", "-m")) or pdfl.get("model")):
            fixed = hinted or (backend if backend != "auto" else None)  # hinted incl. a trusted project default backend
            miss = missing_defaults([fixed] if fixed in BACKEND_FILES else BACKEND_FILES)
            if miss:
                die(f"{'、'.join(miss)} 还没有设置默认模型，任务也没有用 -m 指定模型。{MODEL_SETUP_HINT}", 6)
        with QLock():
            if id_taken(rid) or (QDIR / f"{rid}.job.json").exists():
                die(f"--id {rid} 已被占用")
            q_save({"id": rid, "args": args, "backend": backend, "allow_switch": allow_switch, "status": "pending",
                    "created": X.now_iso(), "runs": [],
                    "unsafe": os.environ.get("CLI_DELEGATE_UNSAFE_OK") == "1"})
        print(f"已排队: {rid}（后端 {backend}，允许切换 {allow_switch}）。处理器: queue work（后台运行）")
        return 0
    if sub == "list":
        for j in q_jobs():
            print(f"{j['id']:28} [{j['status']}] 后端 {j.get('chosen') or j['backend']}  运行 {', '.join(j['runs']) or '-'}"
                  + (f"  等到 {j['wait_until']}" if j.get("wait_until") else ""))
        print("处理器:", "运行中" if q_worker_alive() else "未运行")
        return 0
    if sub == "cancel":
        if not args:
            die("queue cancel <任务名>")
        with QLock():
            j = q_load(valid_id(args[0], "任务名"))
            if not j:
                die("没有这个任务")
            if j["status"] == "running":
                die(f"任务正在运行；先用 decide {j['runs'][-1]} all deny -m ... 让它结束，再取消")
            j["status"] = "cancelled"
            q_save(j)
        print("已取消")
        return 0
    if sub == "ack":
        run = take_opt(args, ("--run",))
        if not args:
            die("queue ack <任务名> [--run <续跑或交接的新运行名>]")
        with QLock():
            j = q_load(valid_id(args[0], "任务名"))
            if not j:
                die("没有这个任务")
            if run:  # keep tracking the follow-up run under the same job
                b, rd = find_run(run)
                j["runs"].append(rd.name)
                j.update({"status": "running", "pid": None, "chosen": b, "acked": False})
            else:
                j["acked"] = True
            q_save(j)
        print("已登记后续运行，继续跟踪" if run else "已标记为已处理")
        return 0
    if sub == "watch":
        max_wait = int(take_opt(args, ("--max-wait",)) or 540)
        t0 = time.time()
        while True:
            jobs = q_jobs()
            pend = []
            for j in jobs:
                if j["status"] == "running" and j["runs"] and j.get("chosen"):
                    rd = runs_dir(j["chosen"]) / j["runs"][-1]
                    if (rd / "approvals").is_dir():
                        for p in sorted((rd / "approvals").glob("*.request.json")):
                            if not (rd / "approvals" / p.name.replace(".request.", ".decision.")).exists():
                                pend.append((j, rd.name, p.name.split(".")[0]))
            if pend:
                print(f"=== 队列: {len(pend)} 个待批请求 ===")
                for j, rn, rq in pend:
                    print(f"- 任务 {j['id']} 运行 {rn} 请求 {rq}（用 watch {rn} 查看详情，decide {rn} {rq} ... 批复）")
                return 10
            act = [j for j in jobs if j["status"] in ("needs_handoff", "needs_answer") and not j.get("acked")]
            if act:
                print("=== 队列: 需要处理的任务 ===")
                for j in act:
                    what = ("额度用尽，按允许切换=是 需要交接（handoff 流程）" if j["status"] == "needs_handoff"
                            else "子会话在等答复（resume --answer）")
                    print(f"- {j['id']}（最后运行 {j['runs'][-1]}）：{what}。后续运行启动后执行 "
                          f"queue ack {j['id']} --run <新运行名>；不再继续则 queue ack {j['id']}")
                return 12
            live = [j for j in jobs if j["status"] in ("pending", "running", "waiting_reset", "dispatching")]
            if not live:
                print("=== 队列全部结束 ===")
                for j in jobs:
                    print(f"- {j['id']} [{j['status']}] 运行 {', '.join(j['runs']) or '-'}")
                return 0
            if not q_worker_alive():  # also settles follow-up runs registered by `ack --run`
                print("队列里还有任务，但处理器没在运行：后台执行 queue work")
                return 13
            if time.time() - t0 > max_wait:
                print("队列仍在处理中：" + ", ".join(f"{j['id']}[{j['status']}]" for j in live))
                return 11
            time.sleep(2)
    if sub == "work":
        return q_work(args)
    die(f"未知 queue 子命令: {sub}")
    return 2


def q_work(args: list[str]) -> int:
    import fcntl
    limits = {"claude": 2, "codex": 2}
    for kv in (take_opt(args, ("--max-parallel",)) or "").split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            limits[k.strip()] = int(v)
    poll = int(take_opt(args, ("--poll",)) or 15)
    threshold = int(take_opt(args, ("--threshold",)) or 100)
    QDIR.mkdir(parents=True, exist_ok=True)
    lockf = (QDIR / "worker.lock").open("a")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)  # held until exit: only one worker can run
    except BlockingIOError:
        die("已有一个队列处理器在运行")
    me = str(Path(__file__).resolve())
    qcache: dict = {}

    def usable(b: str, home: str | None) -> bool:
        key = (b, home)
        if key not in qcache or time.time() - qcache[key][0] > 120:
            q = safe_quota(b, home if b == "codex" else None)
            qcache[key] = (time.time(), q)
        return available(qcache[key][1], threshold)[0]

    def start(job_id: str, expect: str, cmd: list[str], backend: str, run_name: str) -> bool:
        with QLock():
            job = q_load(job_id)
            if not job or job["status"] != expect:  # cancelled or changed meanwhile: do not dispatch
                return False
            # persist the claim BEFORE starting, so a crash in between is reconciled instead of re-dispatched
            token = secrets.token_hex(8)
            job.update({"status": "dispatching", "prev_status": expect, "chosen": backend,
                        "intended_run": run_name, "claimed": X.now_iso(), "token": token})
            q_save(job)
            log = (QDIR / f"{job_id}.log").open("a")
            env = {**os.environ, "CLI_DELEGATE_QUEUE_JOB": job_id, "CLI_DELEGATE_QUEUE_RUN": run_name,
                   "CLI_DELEGATE_QUEUE_TOKEN": token}
            if job.get("unsafe"):
                env["CLI_DELEGATE_UNSAFE_OK"] = "1"
            p = subprocess.Popen([sys.executable, me, *cmd], stdout=log, stderr=subprocess.STDOUT, env=env,
                                 start_new_session=True, stdin=subprocess.DEVNULL)
            job.update({"status": "running", "pid": p.pid, "started": X.now_iso(), "wait_until": None})
            job["runs"].append(run_name)
            q_save(job)
        note(f"队列启动 {job_id} → {backend}:{run_name}")
        return True

    with QLock():  # reconcile claims left by a worker that died between claim and start
        for j in q_jobs():
            if j["status"] != "dispatching":
                continue
            rn = j.get("intended_run")
            ack = X.read_json(QDIR / f"{j['id']}.started.json", {}) or {}
            child_alive = False
            try:
                os.kill(int(ack.get("pid")), 0)
                child_alive = ack.get("run") == rn
            except (OSError, TypeError, ValueError):
                pass
            if child_alive:  # the child acknowledged the claim and still runs: keep tracking it
                if rn not in j["runs"]:
                    j["runs"].append(rn)
                j.update({"status": "running", "pid": ack["pid"]})
            elif rn and X.rmeta_path(rn).exists():  # the router did start: track it through its record
                if rn not in j["runs"]:
                    j["runs"].append(rn)
                j.update({"status": "running", "pid": None})
            else:
                j["status"] = j.get("prev_status") or "pending"
                j["token"] = None  # invalidates a child that was started but never acknowledged
            q_save(j)

    while True:
        with QLock():  # finished runs -> next state, decided on fresh job files
            for j in q_jobs():
                if j["status"] != "running" or q_alive(j):
                    continue
                rmj = X.load_rmeta(j["runs"][-1])
                started_rm = bool(rmj) and rmj.get("status") not in (None, "claimed")
                rc = rmj.get("rc") if started_rm else j.get("_exit")
                if rc == 5:
                    if j["allow_switch"] == "是":
                        j["status"] = "needs_handoff"
                    else:
                        bm = X.read_json(runs_dir(j["chosen"]) / j["runs"][-1] / "meta.json", {}) or {}
                        until = bm.get("reset_at") or (_dt.datetime.now().astimezone()
                                                       + _dt.timedelta(minutes=30)).isoformat(timespec="minutes")
                        j["status"], j["wait_until"] = "waiting_reset", until
                elif rc == 6:
                    j["status"] = "needs_answer"
                elif rc == 8 and not started_rm:
                    # never started because the directory/backend was busy: retry later instead of failing
                    j["runs"].pop()
                    j["status"] = j.get("prev_status") or "pending"
                else:
                    j["status"] = "done" if rc == 0 else "failed"
                j["rc"], j["pid"] = rc, None
                q_save(j)
        jobs = q_jobs()
        busy = {b: sum(1 for j in jobs if j["status"] == "running" and j.get("chosen") == b) for b in BACKEND_FILES}
        for j in jobs:  # resume after the quota reset
            if j["status"] == "waiting_reset":
                b, home = j["chosen"], job_home(j)
                ready = not j.get("wait_until") or _dt.datetime.now().astimezone() >= \
                    _dt.datetime.fromisoformat(j["wait_until"]) + _dt.timedelta(seconds=90)
                if ready and busy[b] < limits.get(b, 2) and usable(b, home):
                    new = f"{j['id']}-r{len(j['runs'])}"
                    if start(j["id"], "waiting_reset", ["resume", j["runs"][-1], "--id", new], b, new):
                        busy[b] += 1
        for j in jobs:  # pending, FIFO
            if j["status"] != "pending":
                continue
            hinted = job_model_backend(j["args"])
            cands = [hinted] if hinted else ["claude", "codex"]
            home = job_home(j)
            jcwd = str(Path(opt_value(j["args"], ("--cwd",)) or ".").expanduser().resolve())
            if not {"--isolate", "--group", "--no-snapshot"} & set(j["args"]):
                if X.dir_lock(jcwd, f"probe-{j['id']}"):
                    continue  # another run holds this directory: keep the job pending
                X.dir_unlock(jcwd)
            chosen = next((b for b in cands if busy[b] < limits.get(b, 2) and usable(b, home)), None)
            if chosen:
                a2 = list(j["args"])
                take_opt(a2, ("--backend", "-b"))
                if start(j["id"], "pending", ["run", "--backend", chosen, *a2], chosen, j["id"]):
                    busy[chosen] += 1
        live = [j for j in q_jobs() if j["status"] in ("pending", "running", "waiting_reset", "dispatching")]
        if not live:
            note("队列已清空，处理器退出")
            return 0
        time.sleep(poll)


# ==================================================================== images ====
IMAGE_DIR = BASE / "backends" / "codex" / "image"


def _image_usage(jsonl: Path) -> dict:
    tok = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
    model = None
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = ev.get("usage") if ev.get("type") == "turn.completed" else None
            if u:
                tok["input"] += u.get("input_tokens") or 0
                tok["cached"] += u.get("cached_input_tokens") or 0
                tok["output"] += u.get("output_tokens") or 0
                tok["reasoning"] += u.get("reasoning_output_tokens") or 0
            model = model or ev.get("model") or (ev.get("thread") or {}).get("model")
    return {"tokens": tok, "model": model}


def cmd_image(sub: str, argv: list[str]) -> int:
    """Image generation always runs on codex (GPT image tool), never through the claude backend."""
    script = {"image": IMAGE_DIR / "codex_image.sh", "image-batch": IMAGE_DIR / "codex_batch.sh",
              "image-sheet": IMAGE_DIR / "contact_sheet.py"}[sub]
    m = backend_mod("codex")
    env = m.clean_env(os.environ.get("CODEXCLI_HOME"), {}, [])
    env["CODEX_BIN"] = m.find_bin()
    env["PATH"] = os.environ.get("PATH", "")
    if sub == "image-sheet":
        return subprocess.run([sys.executable, str(script), *argv], env=env).returncode
    out = opt_value(argv, ("-o",))
    if not out:
        die(f"{sub} 需要 -o（单张为输出文件，批量为输出目录）")
    if "-n" in argv:  # dry run: no ledger
        return subprocess.run(["bash", str(script), *argv], env=env).returncode
    q_before = safe_quota("codex")
    t0 = time.time()
    outp0 = Path(out).expanduser()
    blog = outp0 / "_batch.log"
    own_log = None
    expected = []
    if sub == "image-batch":
        force = "-f" in argv
        for pf in sorted(Path(opt_value(argv, ("-d",)) or ".").expanduser().glob("*.txt")):
            if force or not (outp0 / f"{pf.stem}.png").exists():
                expected.append(pf.stem)  # existing outputs are skipped by the batch script  # each batch gets its own result log; concurrent batches cannot mix
        tag = f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        own_log = outp0 / f".batch-{tag}.log"
        env["BATCH_LOG"] = str(own_log)
        env["BATCH_LOG_DIR"] = str(outp0 / ".codex-image-logs" / f"batch-{tag}")  # per-batch usage logs
    rc = subprocess.run(["bash", str(script), *argv], env=env).returncode
    seconds = round(time.time() - t0, 1)
    outp = Path(out).expanduser()
    if sub == "image":
        logdir = Path(opt_value(argv, ("-l",)) or (outp.parent / ".codex-image-logs"))
        jsonls = [logdir / f"{outp.stem}.jsonl"]
        produced = [str(outp)] if rc == 0 and outp.exists() else []
        failed = [] if produced else [outp.stem]
    else:
        logdir = Path(env["BATCH_LOG_DIR"])
        produced, failed, names = [], [], []
        # structured per image: every prompt this batch ran has its own event log in the per-batch
        # log dir; success = its PNG exists, is a regular file and was written during this batch
        if own_log and own_log.exists():
            with blog.open("a", encoding="utf-8") as f:  # keep the human-readable cumulative log too
                f.write(own_log.read_text(encoding="utf-8", errors="replace"))
        ran = {j.stem for j in logdir.glob("*.jsonl")} if logdir.exists() else set()
        for name in sorted(set(expected) | ran):
            png = outp / f"{name}.png"
            if png.is_file() and not png.is_symlink() and png.stat().st_mtime >= t0 - 1:
                produced.append(str(png))
            else:
                failed.append(name)  # incl. prompts that failed before writing any log
            if name in ran:
                names.append(name)
        jsonls = [logdir / f"{n}.jsonl" for n in names]  # only images actually generated by this batch
    tok = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
    model = None
    for j in jsonls:
        u = _image_usage(j)
        model = model or u["model"]
        for k in tok:
            tok[k] += u["tokens"][k]
    rid = f"img-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    entry = {"id": rid, "time": X.now_iso(), "backend": "codex", "sub": sub, "rc": rc,
             "status": f"成功 {len(produced)} 张" + (f"，失败 {len(failed)} 张" if failed else "") if produced or failed else STATUS.get(rc, f"exit {rc}"),
             "cwd": os.getcwd(), "seconds": seconds, "quota_before": q_before, "quota_after": safe_quota("codex"),
             "tokens": tok, "model_actual": model or "codex 默认模型 + image_generation 工具", "access": "read-only 沙箱",
             "effort": None, "usage_limited": False, "switch": None, "snapshot": "不适用",
             "images": produced, "failed": failed}
    X.ledger_append(entry)
    print("\n" + X.fmt_ledger(entry) + "\n图片: " + (", ".join(produced) if produced else "无"), flush=True)
    return rc


# ==================================================================== prune ====
def cmd_prune(a) -> int:
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=a.days)).isoformat()
    plan = []
    for b in BACKEND_FILES:
        m = backend_mod(b)
        for rd in m.all_runs():
            if m.run_order(rd) < cutoff and not m.pid_alive(rd):
                plan.append((b, rd))
    keep_iso = []
    for b, rd in plan:
        rm = X.load_rmeta(rd.name)
        if rm.get("isolated") and Path(rm["isolated"]["worktree"]).exists():
            keep_iso.append(rd.name)
    plan = [(b, rd) for b, rd in plan if rd.name not in keep_iso]
    print(f"{'将' if a.yes else '可'}清理 {len(plan)} 个 {a.days} 天前的运行记录（含快照引用、账本外的中间文件）：")
    for b, rd in plan[:50]:
        print(f"  [{b}] {rd.name}")
    if keep_iso:
        print("保留（隔离 worktree 尚未合并或丢弃）: " + ", ".join(keep_iso))
    if not a.yes:
        print("这是预览；确认清理请加 --yes。账本 ledger.jsonl 不会被删除。")
        return 0
    shadows = set()
    for b, rd in plan:
        if rd.parent.resolve() != runs_dir(b).resolve():
            continue  # safety: only delete inside the backend's runs directory
        rm = X.load_rmeta(rd.name)
        if rm.get("snap_dir"):
            X.snap_drop(rm["snap_dir"], rd.name)
            shadows.add(rm["snap_dir"])
        shutil.rmtree(rd, ignore_errors=True)
        X.rmeta_path(rd.name).unlink(missing_ok=True)
        for f in (STATE_DIR / "handoffs").glob(f"{rd.name}.*") if (STATE_DIR / "handoffs").exists() else []:
            f.unlink(missing_ok=True)
        (QDIR / f"{rd.name}.job.json").unlink(missing_ok=True)
        (QDIR / f"{rd.name}.log").unlink(missing_ok=True)
    for d in shadows:
        X._git(["gc", "--prune=now", "-q"], {"GIT_DIR": str(X._shadow(d))}, check=False)
    print("清理完成")
    return 0


# ===================================================================== misc ====
def cmd_list(a) -> int:
    rows = []
    for b in BACKEND_FILES:
        m = backend_mod(b)
        for r in m.all_runs():
            meta = X.read_json(r / "meta.json", {}) or {}
            if m.pid_alive(r):
                state = "运行中"
            elif meta.get("timed_out"):
                state = "超时"
            elif meta.get("usage_limited"):
                state = f"额度用尽 {meta.get('reset_at') or ''}".strip()
            elif meta.get("needs_confirmation"):
                state = "待确认"
            else:
                state = "成功" if meta.get("exit_code") == 0 else f"exit={meta.get('exit_code')}"
            rm = X.load_rmeta(r.name)
            if a.group and rm.get("group") != a.group:
                continue
            tags = ((" 隔离" if rm.get("isolated") else "") + (f" 组:{rm['group']}" if rm.get("group") else "")
                    + (f" ←{rm['switch']}" if rm.get("switch") else ""))
            sid = meta.get("session_id") or meta.get("thread_id") or "-"
            rows.append((m.run_order(r), b, r.name, state + tags, sid, meta.get("cwd")))
    rows.sort()
    for _, b, name, state, sid, cwd in rows[-a.n:]:
        print(f"[{b:6}] {name}  [{state}]  {str(sid)[:13]}  {cwd}")
    if not rows:
        print("还没有任何运行记录")
    return 0


def cmd_doctor(a) -> int:
    rc = 0
    for b in BACKEND_FILES:
        print(f"===== {b} =====", flush=True)
        rc = rc or subprocess.run([sys.executable, str(BACKEND_FILES[b]), "doctor"]).returncode
    print("===== 额度 =====")
    for b in BACKEND_FILES:
        print(fmt_quota(safe_quota(b)))
    print("git:", shutil.which("git") or "未安装（快照、撤销、隔离不可用）")
    return rc


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if os.environ.get("CLI_DELEGATE_QUEUE_JOB"):  # handshake: verify + acknowledge the claim atomically
        jid = os.environ["CLI_DELEGATE_QUEUE_JOB"]
        if not _ID_RE.match(jid):
            die("队列任务名不合法")
        with QLock():
            j = q_load(jid) or {}
            if j.get("token") != os.environ.get("CLI_DELEGATE_QUEUE_TOKEN"):
                die("这次派发已失效（任务被重新认领或取消），不执行", 9)
            X.write_json(QDIR / f"{jid}.started.json", {"pid": os.getpid(), "run": os.environ.get("CLI_DELEGATE_QUEUE_RUN"),
                                                         "token": j.get("token"), "at": X.now_iso()})
    if "--dry-run" not in argv and argv[0] not in ("quota", "pick", "rules", "models"):
        backend_mod("claude").migrate_runs()  # one-time move of old claude run records (never in dry runs)
    sub, rest = argv[0], argv[1:]
    if sub == "unsafe":  # explicit, separately-approvable entry for sensitive options
        if not rest or rest[0] not in ("run", "resume", "handoff", "queue"):
            die("用法: unsafe run|resume|handoff|queue add ...（只在用户同意敏感参数后使用）")
        os.environ["CLI_DELEGATE_UNSAFE_OK"] = "1"
        sub, rest = rest[0], rest[1:]
    direct = {"run": cmd_run, "resume": cmd_resume, "handoff": cmd_handoff, "queue": cmd_queue}
    if sub in direct:
        return direct[sub](rest)
    if sub in ("image", "image-batch", "image-sheet"):
        return cmd_image(sub, rest)
    if sub in ("watch", "decide", "show", "progress"):
        return cmd_forward(sub, rest)
    p = argparse.ArgumentParser(prog="cli_delegate.py")
    sp = p.add_subparsers(dest="sub")
    q = sp.add_parser("quota")
    q.add_argument("--backend", default="all", choices=["all", *BACKEND_FILES])
    q.add_argument("--threshold", type=int, default=100)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_quota)
    pk = sp.add_parser("pick")
    pk.add_argument("--backend", default="auto", choices=["auto", *BACKEND_FILES])
    pk.add_argument("--model", "-m")
    pk.add_argument("--prefer", default="claude", choices=list(BACKEND_FILES))
    pk.add_argument("--threshold", type=int, default=100)
    pk.add_argument("--json", action="store_true")
    pk.set_defaults(func=cmd_pick)
    lg = sp.add_parser("ledger")
    lg.add_argument("run", nargs="?")
    lg.add_argument("--days", type=int, default=7)
    lg.add_argument("--json", action="store_true")
    lg.set_defaults(func=cmd_ledger)
    for name, fn in (("diff", cmd_diff), ("undo", cmd_undo), ("merge", cmd_merge), ("discard", cmd_discard)):
        x = sp.add_parser(name)
        x.add_argument("run")
        if name == "diff":
            x.add_argument("--stat", action="store_true")
        if name == "undo":
            x.add_argument("--force", action="store_true")
        if name == "merge":
            x.add_argument("--3way", dest="three_way", action="store_true")
            x.add_argument("--keep", action="store_true", help="合并后保留 worktree")
        x.set_defaults(func=fn)
    rs_ = sp.add_parser("result", help="最终报告的文件路径与 sha256（原样转交用）")
    rs_.add_argument("run")
    rs_.set_defaults(func=cmd_result)
    ru = sp.add_parser("rules")
    ru.add_argument("--cwd", default=os.getcwd())
    ru.add_argument("--trust", action="store_true", help="确认这份项目配置可信（须用户同意）")
    ru.add_argument("--sha", help="--trust 时必填：预览时显示的 sha256（至少 12 位）")
    ru.set_defaults(func=cmd_rules)
    pr = sp.add_parser("prune")
    pr.add_argument("--days", type=int, default=14)
    pr.add_argument("--yes", action="store_true")
    pr.set_defaults(func=cmd_prune)
    ls = sp.add_parser("list")
    ls.add_argument("-n", type=int, default=20)
    ls.add_argument("--group", help="只列这个并发组的运行")
    ls.set_defaults(func=cmd_list)
    dr = sp.add_parser("doctor")
    dr.set_defaults(func=cmd_doctor)
    mo = sp.add_parser("models", help="默认模型：查看 / --list 查可用模型 / --set 写入 / --clear 清除")
    mo.add_argument("--backend", "-b", choices=["all", *BACKEND_FILES])
    mx = mo.add_mutually_exclusive_group()  # a read-only form (--list) can never also write
    mx.add_argument("--list", action="store_true", help="查询可用模型和思考强度（不消耗 token）")
    mx.add_argument("--set", choices=list(BACKEND_FILES), help="设置这个后端的默认模型")
    mo.add_argument("--model", "-m")
    mo.add_argument("--effort", "-e")
    mo.add_argument("--no-check", action="store_true", help="--set 时跳过可用模型校验")
    mx.add_argument("--clear", choices=["all", *BACKEND_FILES])
    mo.add_argument("--json", action="store_true")
    mo.set_defaults(func=cmd_models)
    a = p.parse_args(argv)
    if not getattr(a, "func", None):
        die(f"未知子命令: {sub}")
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
