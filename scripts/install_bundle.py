"""Stage and publish the app and its agent skill on their destination filesystems."""
import argparse
import ctypes
import errno
import os
import shutil
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

APP = "CLI Manager.app"
SKILL = "cli-manager-desktop"
FULL_SKILL = "cli-manager"
RENAME_EXCL = 0x00000004


def exclusive_rename(source, destination):
    rename = ctypes.CDLL(None, use_errno=True).renamex_np
    rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(os.fsencode(source), os.fsencode(destination), RENAME_EXCL):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def ensure_absent(path):
    if path.exists() or path.is_symlink():
        raise FileExistsError(errno.EEXIST, "安装目标已存在", path)


def install(bundle, skill_source, apps, skills, full_source=None, link_dirs=()):
    apps, skills = apps.resolve(), skills.resolve()
    link_dirs = list(dict.fromkeys(path.resolve() for path in link_dirs))
    app = apps / APP
    companion = skills / SKILL
    skill_copies = {companion: skill_source}
    if full_source is not None:
        skill_copies[skills / FULL_SKILL] = full_source
    links = {directory / target.name: target for directory in link_dirs for target in skill_copies}
    copies = (app, *skill_copies)
    destinations = (*copies, *links)
    roots = {apps, skills, *link_dirs}
    if (skills in link_dirs or any(root == destination or destination in root.parents
                                   for root in roots for destination in destinations)):
        raise ValueError("应用目录、Skill 存放目录和软链接目录应独立于安装目标")

    for destination in destinations:
        ensure_absent(destination)
    with ExitStack() as staging:
        staged = {}
        for root in roots:
            root.mkdir(parents=True, exist_ok=True)
            staged[root] = Path(staging.enter_context(TemporaryDirectory(prefix=".cli-manager-", dir=root)))
        staged_app = staged[apps] / APP
        subprocess.run(["/usr/bin/ditto", bundle, staged_app], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", staged_app], check=True)
        for target, source in skill_copies.items():
            shutil.copytree(source, staged[skills] / target.name, symlinks=True)
        with (staged[skills] / SKILL / "app-path").open("x", encoding="utf-8") as pointer:
            pointer.write(str(app) + "\n")
        for link, target in links.items():
            (staged[link.parent] / link.name).symlink_to(target, target_is_directory=True)

        placed = []
        try:
            for destination in destinations:
                source = staged[destination.parent] / destination.name
                placed.append((source, destination, source.lstat()))
                exclusive_rename(source, destination)
        except BaseException:
            failures = []
            for source, destination, identity in reversed(placed):
                try:
                    current = destination.lstat()
                except FileNotFoundError:
                    continue
                except OSError:
                    failures.append(str(destination))
                    continue
                if os.path.samestat(identity, current):
                    try:
                        exclusive_rename(destination, source)
                    except OSError:
                        failures.append(str(destination))
            if failures:
                raise RuntimeError("安装中断，回滚失败。请检查 " + "、".join(failures)) from None
            raise
    return destinations


def interrupted(_signal, _frame):
    raise KeyboardInterrupt


def main():
    import signal

    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("skill_source", type=Path)
    parser.add_argument("apps", type=Path)
    parser.add_argument("skills", type=Path)
    parser.add_argument("full_source", type=Path, nargs="?")
    parser.add_argument("--link-skills-dir", dest="link_dirs", type=Path, action="append", default=[])
    options = parser.parse_args()
    try:
        installed = install(**vars(options))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    for path in installed:
        print(f"Installed: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
