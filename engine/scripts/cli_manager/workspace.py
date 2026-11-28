"""Independent Git worktrees, snapshots, and journaled application."""
from __future__ import annotations

import base64
import ctypes
import fnmatch
import hashlib
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from .store import DelegateError, digest, lock, write, write_json

SKIP_NAMES = {".git", ".claude", ".codex", ".agents", ".ssh", ".aws", ".kube",
              "node_modules", ".venv", "__pycache__", ".cache", ".cli-delegate-v2",
              ".mcp.json", ".credentials.json", "auth.json", ".netrc", ".npmrc", ".env"}
SKIP_GLOBS = (".env.*", "*.pem", "*.key", "*.p12", "*.pfx", ".cli-manager-apply-*")
COPY_EXCLUDES = [f":(exclude,icase,glob)**/{pattern}{suffix}"
                 for pattern in (*sorted(SKIP_NAMES), *SKIP_GLOBS) for suffix in ("", "/**")]
GIT_ENV = {"PATH": os.defpath, "LANG": "C", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "cli-manager", "GIT_AUTHOR_EMAIL": "cli-manager@localhost",
           "GIT_COMMITTER_NAME": "cli-manager", "GIT_COMMITTER_EMAIL": "cli-manager@localhost"}


def git(where: Path, *args, inp=None, extra=None, required=True):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
         "-c", "core.attributesFile=" + os.devnull, "-C", str(where), *args],
        env={**GIT_ENV, **(extra or {})}, capture_output=True, input=inp, timeout=120,
    )
    if result.returncode and required:
        detail = result.stderr.decode(errors="replace").strip()[:1200]
        raise DelegateError(f"Git {args[0]} 失败：{detail}")
    return result.stdout if required or result.returncode == 0 else None


def text(where: Path, *args, **kwargs):
    return git(where, *args, **kwargs).decode().strip()


def names(raw: bytes):
    return [os.fsdecode(path) for path in raw.split(b"\0") if path]


def protected(path: PurePosixPath) -> bool:
    for part in path.parts:
        name = part.casefold()
        if name in SKIP_NAMES or any(fnmatch.fnmatchcase(name, pattern) for pattern in SKIP_GLOBS):
            return True
    return False


def alias_error(path: Path):
    return DelegateError(f"路径仅通过大小写或 Unicode 别名存在：{path}")


def safe_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (not path.parts or path.is_absolute() or ".." in path.parts or protected(path)
            or path.as_posix() != relative or any(ord(c) < 32 for c in relative)):
        raise DelegateError(f"任务路径被拒绝：{relative!r}")
    current = root
    for index, part in enumerate(path.parts):
        parent, current = current, current / part
        if not os.path.lexists(current):
            return root.joinpath(*path.parts)
        if part not in os.listdir(parent):
            raise alias_error(current)
        if index < len(path.parts) - 1 and (current.is_symlink() or not current.is_dir()):
            raise DelegateError(f"路径父级无法安全访问：{current}")
    return current


def validate_link(root: Path, path: Path, target: str):
    try:
        resolved = (path.parent / target).resolve()
        relative = resolved.relative_to(root.resolve())
    except (OSError, ValueError, RuntimeError) as error:
        raise DelegateError(f"拒绝无法在项目内解析的符号链接：{path}") from error
    if Path(target).is_absolute() or protected(PurePosixPath(relative.as_posix())):
        raise DelegateError(f"拒绝越界或指向受保护路径的符号链接：{path}")


def entry(root: Path, relative: str, *, allow_directory=False):
    path = safe_path(root, relative)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        validate_link(root, path, target)
        return {"type": "link", "target": target}
    if allow_directory and stat.S_ISDIR(info.st_mode):
        return {"type": "directory"}
    if not stat.S_ISREG(info.st_mode):
        raise DelegateError(f"只支持普通文件和项目内符号链接：{relative}")
    if info.st_nlink != 1:
        raise DelegateError(f"拒绝硬链接文件：{relative}")
    if info.st_size > 64 * 1024 * 1024:
        raise DelegateError(f"单文件超过 64 MiB：{relative}")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise DelegateError(f"读取时文件已变化：{relative}")
        data = source.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise DelegateError(f"单文件超过 64 MiB：{relative}")
    return {"type": "file", "executable": bool(info.st_mode & stat.S_IXUSR),
            "data": base64.b64encode(data).decode("ascii")}


UNGUARDED = object()


class WriteConflict(DelegateError):
    """The destination changed before the pending operation made a write."""


def path_stamp(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return stat_stamp(info)


def stat_stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def darwin_attributes(path: Path):
    library = ctypes.CDLL(None, use_errno=True)
    list_names, get_value = library.listxattr, library.getxattr
    list_names.argtypes = (ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
    get_value.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                          ctypes.c_uint32, ctypes.c_int)
    list_names.restype = get_value.restype = ctypes.c_ssize_t
    source = os.fsencode(path)
    nofollow = 1  # XATTR_NOFOLLOW

    def read(operation):
        count = operation(None, 0)
        if not 0 <= count <= 64 * 1024 * 1024:
            raise WriteConflict(f"无法核对扩展属性：{path}")
        if not count:
            return b""
        output = ctypes.create_string_buffer(count)
        if operation(output, count) != count:
            raise WriteConflict(f"核对期间扩展属性发生变化：{path}")
        return output.raw

    names = read(lambda buf, size: list_names(source, buf, size, nofollow)).split(b"\0")
    return [(name, read(lambda buf, size: get_value(source, name, buf, size, 0, nofollow)))
            for name in sorted(names) if name]


def file_metadata(path: Path, *, mode=None, copyable=False):
    """Fingerprint permissions and attributes that can change without changing bytes."""
    info = path.lstat()
    if sys.platform == "darwin":
        attributes = darwin_attributes(path)
        result = subprocess.run(["/bin/ls", "-dlenq", str(path)], env={"LC_ALL": "C"}, capture_output=True, timeout=10)
        if result.returncode:
            raise WriteConflict(f"无法核对文件 ACL：{path}")
        acl = [line.hex() for line in result.stdout.splitlines()[1:]]
    elif hasattr(os, "listxattr"):
        attributes = [(os.fsencode(name), os.getxattr(path, name, follow_symlinks=False))
                      for name in sorted(os.listxattr(path, follow_symlinks=False))]
        acl = []  # POSIX ACLs are included in Linux's system.* extended attributes.
    else:
        raise WriteConflict(f"当前平台无法核对文件属性：{path}")
    # No timestamps in the fingerprint: renaming updates ctime, and children
    # update directory mtime. Use the full stamp to detect a concurrent read.
    if path_stamp(path) != stat_stamp(info):
        raise WriteConflict(f"核对属性期间原文件发生变化：{path}")
    full_mode = info.st_mode if mode is None else stat.S_IFMT(info.st_mode) | mode
    metadata = [(name.hex(), hashlib.sha256(content).hexdigest()) for name, content in attributes
                if not (copyable and name == b"com.apple.provenance")]
    return digest([full_mode, info.st_uid, info.st_gid, getattr(info, "st_flags", 0), metadata, acl])


def copy_replacement_metadata(original: Path, staged: Path):
    mode = None if staged.is_symlink() else stat.S_IMODE(staged.lstat().st_mode)
    expected = file_metadata(original, mode=mode, copyable=True)
    if sys.platform == "darwin":
        copy = ctypes.CDLL(None, use_errno=True).copyfile
        copy.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32)
        flags = 7 | (1 << 18) | (1 << 19)  # COPYFILE_METADATA, COPYFILE_NOFOLLOW_SRC/DST
        if copy(os.fsencode(original), os.fsencode(staged), None, flags):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(original))
    else:
        shutil.copystat(original, staged, follow_symlinks=False)
    if mode is not None:
        staged.chmod(mode)
    if file_metadata(staged, copyable=True) != expected:
        raise WriteConflict(f"无法在替换时保留原文件权限或属性：{original}")


def observe_entry(root: Path, relative: str):
    path = safe_path(root, relative)
    stamp = path_stamp(path)
    value = entry(root, relative, allow_directory=True)
    if stamp != path_stamp(safe_path(root, relative)):
        raise WriteConflict(f"核对期间原路径发生变化：{relative}")
    return value, stamp


def file_id(path: Path):
    stamp = path_stamp(path)
    return list(stamp[:2]) if stamp else None


def rename_new(source: Path, destination: Path):
    """Publish a prepared file atomically while requiring a vacant destination."""
    native_rename(source, destination, exchange=False)


def rename_exchange(source: Path, destination: Path):
    """Replace a file atomically while retaining the previous inode."""
    native_rename(source, destination, exchange=True)


def native_rename(source: Path, destination: Path, *, exchange):
    library = ctypes.CDLL(None, use_errno=True)
    old, new = os.fsencode(source), os.fsencode(destination)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        result = rename(old, new, 0x00000002 if exchange else 0x00000004)  # RENAME_SWAP or RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        result = rename(-100, old, -100, new, 2 if exchange else 1)  # AT_FDCWD, RENAME_EXCHANGE or RENAME_NOREPLACE
    else:
        raise DelegateError("当前环境不支持原子创建项目文件")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def install(root: Path, relative: str, value, *, restored_mode=None, expected_stamp=UNGUARDED, record=None, slot=None):
    def check_destination():
        if expected_stamp is UNGUARDED:
            return
        try:
            current = path_stamp(safe_path(root, relative))
        except DelegateError as error:
            raise WriteConflict(str(error)) from error
        if current != expected_stamp:
            raise WriteConflict(f"写入前原路径发生变化：{relative}")

    def publish(staged):
        check_destination()
        if expected_stamp is None:
            try:
                rename_new(staged, path)
            except FileExistsError as error:
                raise WriteConflict(f"创建前原路径已被占用：{relative}") from error
        elif slot is not None:
            previous = list(expected_stamp[:2])
            record(exchanged_id=previous, exchanged_shape=[*expected_stamp[2:5], expected_stamp[6]],
                   retained_metadata=file_metadata(path))
            check_destination()
            rename_exchange(staged, path)
            if file_id(staged) != previous:
                raise DelegateError(f"原路径被其他进程替换，两个版本都已保留：{relative}")
        else:
            os.replace(staged, path)

    check_destination()
    path = safe_path(root, relative)
    if value is None:
        is_dir = path.is_dir() and not path.is_symlink()
        exists = is_dir or os.path.lexists(path)
        if slot is not None and exists:
            if is_dir and os.listdir(path):
                raise WriteConflict(f"待移除目录包含其他文件：{relative}")
            record(removed_id=list(expected_stamp[:2]), removed_shape=[*expected_stamp[2:5], expected_stamp[6]],
                   retained_metadata=file_metadata(path))
            check_destination()
            rename_new(path, slot)
            return
        check_destination()
        if is_dir:
            path.rmdir()
        elif exists:
            path.unlink()
        if record and exists:
            record(removed=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    private = tempfile.TemporaryDirectory(dir=path.parent) if slot is None else None
    staged = Path(private.name) / "item" if private else slot
    try:
        if value["type"] == "directory":
            staged.mkdir()
            if restored_mode is not None:
                staged.chmod(restored_mode)
        elif value["type"] == "link":
            staged.symlink_to(value["target"])
            validate_link(root, path, value["target"])
        else:
            old_mode = stat.S_IMODE(path.stat().st_mode) if path.is_file() and not path.is_symlink() else 0o644
            mode = restored_mode if restored_mode is not None else (
                old_mode | 0o100 if value["executable"] else old_mode & ~0o111
            )
            with staged.open("xb") as handle:
                handle.write(base64.b64decode(value["data"]))
                os.fchmod(handle.fileno(), mode)
                handle.flush()
                os.fsync(handle.fileno())
        if (slot is not None and expected_stamp is not None
                and stat.S_IFMT(expected_stamp[2]) == stat.S_IFMT(staged.lstat().st_mode)):
            copy_replacement_metadata(path, staged)
        if record:
            record(post_id=file_id(staged), post_metadata=file_metadata(staged))
        publish(staged)
    finally:
        if private:
            private.cleanup()


def validate_tree(repo: Path, commit: str):
    for item in git(repo, "ls-tree", "-rz", commit).split(b"\0"):
        if not item:
            continue
        head, filename = item.split(b"\t", 1)
        relative = os.fsdecode(filename)
        path = PurePosixPath(relative)
        if head.startswith(b"160000 "):
            raise DelegateError(f"任务基线包含未支持的 Git 子模块：{relative}")
        if (protected(path) or any(ord(c) < 32 for c in relative)
                or any(part.casefold() == ".git" for part in path.parts)):
            raise DelegateError(f"基线包含受保护路径：{relative}")


def validate_links(work: Path, repo: Path, commit: str):
    for item in git(repo, "ls-tree", "-rz", commit).split(b"\0"):
        if item.startswith(b"120000 "):
            entry(work, os.fsdecode(item.split(b"\t", 1)[1]))


def init_repo(path: Path, *, literal=False):
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        git(path.parent, "init", "--bare", "--quiet", "--template=", str(path))
    if literal:
        # The private Git index is a transport for plain-project bytes.
        write(path / "info" / "attributes", b"* -text !eol !working-tree-encoding -ident -filter\n")


def import_commit(target: Path, source: Path, commit: str):
    git(target, "fetch", "--quiet", "--no-tags", "--no-auto-gc", "--depth=1", str(source), commit)


def check_path_aliases(work: Path, tracked):
    listings = {}
    for relative in tracked:
        directory = work
        for part in PurePosixPath(relative).parts:
            if directory.is_symlink() or not directory.is_dir():
                break
            if directory not in listings:
                listings[directory] = set(os.listdir(directory))
            path = directory / part
            if part not in listings[directory]:
                if os.path.lexists(path):
                    raise alias_error(path)
                break
            directory = path


def snapshot(repo: Path, work: Path, parent: str | None, *, include=(), force_all=False):
    with tempfile.TemporaryDirectory(dir=repo.parent) as temp:
        env = {"GIT_DIR": str(repo), "GIT_WORK_TREE": str(work),
               "GIT_INDEX_FILE": str(Path(temp) / "index")}
        if parent:
            git(work, "read-tree", parent, extra=env)
            check_path_aliases(work, names(git(work, "ls-files", "-z", extra=env)))
        if force_all:
            # A non-Git project may contain a .gitignore. Copy its files as-is.
            git(work, "add", "-A", "--force", "--", ".", *COPY_EXCLUDES, extra=env)
        else:
            git(work, "add", "-A", "--", ".", extra=env)
            if include:
                git(work, "add", "--force", "--", *(":(literal)" + name for name in include), extra=env)
        tree = text(work, "write-tree", extra=env)
    if parent and tree == text(repo, "rev-parse", parent + "^{tree}"):
        return parent
    args = ["commit-tree", tree, "-m", "cli-manager checkpoint"]
    if parent:
        args.extend(("-p", parent))
    commit = text(repo, *args)
    validate_tree(repo, commit)
    return commit


def copy_non_git(source: Path, dest: Path):
    dest.mkdir(mode=0o700)
    total = 0
    for root, dirs, files in os.walk(source):
        base = Path(root)
        dirs[:] = [name for name in dirs if not protected(PurePosixPath(name))]
        files.extend(name for name in dirs if (base / name).is_symlink())
        for name in files:
            relative = (base / name).relative_to(source).as_posix()
            if protected(PurePosixPath(relative)):
                continue
            value = entry(source, relative)
            total += (base / name).lstat().st_size
            if total > 300 * 1024 * 1024:
                raise DelegateError("非 Git 项目快照超过 300 MiB")
            install(dest, relative, value)


def freeze(project: Path, directory: Path, include: list[str]):
    root = git(project, "rev-parse", "--show-toplevel", required=False)
    is_git = root is not None
    original = Path(os.fsdecode(root.rstrip(b"\n"))).resolve() if is_git else project
    repo = directory / "baseline.git"
    init_repo(repo, literal=not is_git)
    with tempfile.TemporaryDirectory(dir=directory) as temp:
        scratch = Path(temp) / "source"
        if is_git:
            head = text(original, "rev-parse", "--verify", "HEAD")
            import_commit(repo, original, head)
            validate_tree(repo, head)
            git(repo, "worktree", "add", "--quiet", "--detach", str(scratch), head)
            try:
                validate_links(scratch, repo, head)
                present = []
                for relative in include:
                    value = entry(original, relative)
                    install(scratch, relative, value)
                    if value is not None:
                        present.append(relative)
                base = snapshot(repo, scratch, head, include=present)
            finally:
                git(repo, "worktree", "remove", "--force", str(scratch))
        else:
            if include:
                raise DelegateError("--include 仅用于 Git 基线")
            head = None
            copy_non_git(original, scratch)
            base = snapshot(repo, scratch, None, force_all=True)
    git(repo, "update-ref", "refs/heads/frozen", base)
    root_stat = original.stat()
    return {"root": str(original), "device": root_stat.st_dev, "inode": root_stat.st_ino,
            "project_subdir": project.relative_to(original).as_posix(),
            "git": is_git, "commit": base, "head": head, "included": include}


def setup(directory: Path, frozen_repo: Path, task: dict, attempt: dict, patches: list[bytes], handoff=None):
    repo, work = directory / "repo.git", directory / "work"
    init_repo(repo, literal=not task["baseline"]["git"])
    frozen = task["baseline"]["commit"]
    import_commit(repo, frozen_repo, frozen)
    branch = f"cli-manager/{attempt['id']}"
    git(repo, "worktree", "add", "--quiet", "-b", branch, str(work), frozen)
    for patch in patches:
        if not patch:
            continue
        if git(work, "apply", "--check", "--binary", "-", inp=patch, required=False) is not None:
            git(work, "apply", "--binary", "-", inp=patch)
        elif git(work, "apply", "--reverse", "--check", "--binary", "-", inp=patch, required=False) is None:
            raise DelegateError("上游差异与任务基线冲突，请重新规划依赖")
    force_all = not task["baseline"]["git"]
    base = snapshot(repo, work, frozen, force_all=force_all)
    validate_links(work, repo, base)
    if handoff:
        source_repo, source_base, commit, expected_tree = handoff
        if text(repo, "rev-parse", base + "^{tree}") != source_base:
            raise DelegateError("交接基线与上一次尝试不一致")
        import_commit(repo, source_repo, commit)
        delta = git(repo, "diff", "--no-renames", "--binary", base, commit)
        if delta:
            git(work, "apply", "--check", "--binary", "-", inp=delta)
            git(work, "apply", "--binary", "-", inp=delta)
        if text(repo, "rev-parse", snapshot(repo, work, base, force_all=force_all) + "^{tree}") != expected_tree:
            raise DelegateError("交接后的工作树与检查点不一致")
    validate_links(work, repo, snapshot(repo, work, base, force_all=force_all))
    git(repo, "update-ref", "refs/heads/" + branch, base)
    git(work, "reset", "--mixed", "--quiet", base)
    if not task["baseline"]["git"]:
        (work / ".git").unlink()
    cwd = work / task["baseline"]["project_subdir"]
    if not cwd.is_dir() or not cwd.resolve().is_relative_to(work):
        raise DelegateError("任务基线缺少工作子目录")
    return {"base": base, "base_tree": text(repo, "rev-parse", base + "^{tree}"),
            "branch": branch, "cwd": str(cwd), "work": str(work), "repo": str(repo)}


def tree_entry(repo: Path, commit: str, relative: str, *, checkout=None):
    rows = git(repo, "ls-tree", "-z", commit, "--", ":(literal)" + relative).split(b"\0")
    if not rows[0]:
        return None
    header, _ = rows[0].split(b"\t", 1)
    mode, kind, object_id = header.split()
    if kind == b"tree":
        return {"type": "directory"}
    if kind != b"blob":
        raise DelegateError(f"不支持在应用中替换 Git 子模块：{relative}")
    if mode == b"120000":
        data = git(repo, "cat-file", "blob", object_id.decode())
        return {"type": "link", "target": os.fsdecode(data)}
    if checkout:
        directory, options = checkout
        data = git(repo, *options, "cat-file", "--filters", "--path=" + relative, object_id.decode(),
                   extra={"GIT_WORK_TREE": str(directory)})
    else:
        data = git(repo, "cat-file", "blob", object_id.decode())
    return {"type": "file", "data": base64.b64encode(data).decode("ascii"),
            "executable": mode == b"100755"}


def application_trees(repo: Path, task: dict, attempt: dict, paths: set):
    commits = (attempt["base"], attempt["checkpoint"])
    if not task["baseline"]["git"]:
        return [{path: tree_entry(repo, commit, path.as_posix()) for path in paths} for commit in commits]

    # Use only committed attributes and whitelisted EOL config. The private
    # repo has no external filter drivers, includes, or hooks.
    options = []
    root = Path(task["baseline"]["root"])
    for key in ("core.autocrlf", "core.eol"):
        value = git(root, "config", "--get", key, required=False)
        if value is not None:
            options.extend(("-c", key + "=" + os.fsdecode(value).strip()))
    trees = []
    with tempfile.TemporaryDirectory(dir=repo.parent) as temp:
        for index, commit in enumerate(commits):
            directory = Path(temp) / str(index)
            directory.mkdir()
            for item in git(repo, "ls-tree", "-rz", commit).split(b"\0"):
                if not item:
                    continue
                header, name = item.split(b"\t", 1)
                relative = os.fsdecode(name)
                if PurePosixPath(relative).name == ".gitattributes" and header.startswith((b"100644 ", b"100755 ")):
                    blob = header.split()[-1].decode()
                    write(safe_path(directory, relative), git(repo, "cat-file", "blob", blob))
            trees.append({path: tree_entry(repo, commit, path.as_posix(), checkout=(directory, options))
                          for path in paths})
    return trees


def copy_integration_paths(source: Path, target: Path, paths: list[str]):
    affected = {PurePosixPath(path) for path in paths}
    copied = []

    def copy(relative):
        name = relative.as_posix()
        value = entry(source, name, allow_directory=True)
        if value and value["type"] == "directory":
            install(target, name, value)
            for child in sorted((source / relative).iterdir()):
                if not protected(PurePosixPath(child.name)):
                    copy(relative / child.name)
        else:
            install(target, name, value)
            if value:
                copied.append(name)

    for relative in sorted(affected):
        if affected.intersection(relative.parents):
            continue
        location = safe_path(target, relative.as_posix())
        if location.is_dir() and not location.is_symlink():
            shutil.rmtree(location)
        else:
            install(target, relative.as_posix(), None)
        copy(relative)
    return copied


def changes(attempt: dict, checkpoint: str):
    repo = Path(attempt["repo"])
    return names(git(repo, "diff", "--no-renames", "--name-only", "-z", attempt["base"], checkpoint))


def summarize(entry):
    if not entry:
        return None
    return {"type": entry["type"], "sha256": digest(entry)}


def origin(task: dict):
    baseline = task["baseline"]
    root = Path(baseline["root"])
    info = root.lstat()
    actual = (info.st_dev, info.st_ino)
    if not stat.S_ISDIR(info.st_mode) or root.resolve() != root or actual != (baseline["device"], baseline["inode"]):
        raise DelegateError("原项目目录已被移动或替换")
    anchor = Path(root.anchor)
    check_path_aliases(anchor, [root.relative_to(anchor).as_posix()])
    return root


def plan_application(task: dict, attempt: dict, paths: list[str]):
    root, repo = origin(task), Path(attempt["repo"])
    staged = set()
    if task["baseline"]["git"]:
        staged = set(names(git(root, "diff", "--cached", "--no-renames", "--name-only", "-z")))
    affected = {PurePosixPath(path) for path in paths}
    candidates = affected | {parent for path in affected for parent in path.parents if parent.parts}
    before_tree, after_tree = application_trees(repo, task, attempt, candidates)
    directory = {"type": "directory"}
    collapsed = {path for path in affected if before_tree[path] == directory and after_tree[path] is not None}
    expanded = {path for path in affected if before_tree[path] is not None and after_tree[path] == directory}
    expected_children = {}
    for path, before in before_tree.items():
        if before:
            expected_children.setdefault(path.parent, set()).add(path.name)

    rows, conflicts = [], []
    for candidate in sorted(candidates):
        before, after = before_tree[candidate], after_tree[candidate]
        if before == after:
            if before == directory:
                try:
                    if entry(root, candidate.as_posix(), allow_directory=True) != directory:
                        raise DelegateError("受影响文件的父目录已移动或替换")
                except DelegateError as error:
                    conflicts.append({"path": candidate.as_posix(), "reason": str(error)})
            continue
        if before == directory and after is None and not collapsed.intersection(candidate.parents):
            continue
        path = candidate.as_posix()
        try:
            # The child of an approved file-to-directory conversion cannot
            # exist yet. Its parent is checked against the old file separately.
            if expanded.intersection(candidate.parents):
                actual, original_mode = None, None
            else:
                actual = entry(root, path, allow_directory=True)
                location = safe_path(root, path)
                if before is None and after == actual == directory:
                    continue
                if before == actual == directory and set(os.listdir(location)) != expected_children.get(candidate, set()):
                    raise DelegateError("待替换的目录包含额外文件或目录")
                original_mode = (stat.S_IMODE(location.lstat().st_mode)
                                 if actual and actual["type"] != "link" else None)
            if after and after["type"] == "link":
                validate_link(root, root / candidate, after["target"])
        except DelegateError as error:
            conflicts.append({"path": path, "reason": str(error)})
            continue
        captured_stage = attempt.get("integration_stage")
        if not captured_stage or path not in captured_stage:
            changed_index = path in staged
        else:
            index_now = git(root, "ls-files", "--stage", "-z", "--", ":(literal)" + path).hex()
            changed_index = index_now != captured_stage.get(path)
        if changed_index or actual != before:
            conflicts.append({"path": path, "reason": "原路径有暂存修改、工作目录改动或内容漂移"})
        rows.append({"path": path, "before": actual, "after": after, "before_mode": original_mode})
    return rows, conflicts


def project_lock(store, task):
    key = hashlib.sha256(task["baseline"]["root"].encode()).hexdigest()
    return lock(store.root / "locks" / ("project-" + key))


def mutation_receipt(journal, journal_path, operation, phase):
    def record(**details):
        operation.setdefault(phase, {}).update(details)
        write_json(journal_path, journal)
    return record


def clear_staging(root: Path, receipt: dict):
    if not receipt.get("staging"):
        return
    relative = PurePosixPath(receipt["staging"])
    parent = safe_path(root, relative.parent.as_posix()) if relative.parent.parts else root
    suffix = relative.name.removeprefix(".cli-manager-apply-")
    if suffix == relative.name or len(suffix) != 24 or any(char not in "0123456789abcdef" for char in suffix):
        raise WriteConflict("旧日志中的暂存名称无效")
    check_path_aliases(parent, [relative.name])
    directory = parent / relative.name
    actual_id = file_id(directory)
    if actual_id is None:
        return
    if actual_id != receipt.get("staging_id") or directory.is_symlink() or not directory.is_dir():
        raise WriteConflict(f"无法确认暂存目录归属：{directory}")
    check_path_aliases(directory, ["item"])
    staged = directory / "item"
    if set(os.listdir(directory)) - {"item"}:
        raise WriteConflict(f"暂存目录有外部内容：{directory}")
    if os.path.lexists(staged):
        if receipt.get("post_id") and file_id(staged) != receipt["post_id"]:
            raise WriteConflict(f"无法确认暂存文件归属：{staged}")
        if staged.is_dir() and not staged.is_symlink():
            staged.rmdir()
        else:
            staged.unlink()
    directory.rmdir()


def application_scratch_parent(root: Path, rows, journal_path: Path):
    device = root.stat().st_dev
    for row in rows:
        location = root / row["path"]
        while location != root and not os.path.lexists(location):
            location = location.parent
        if location.lstat().st_dev != device:
            raise DelegateError(f"待改路径在其他挂载卷：{row['path']}")
    admin = git(root, "rev-parse", "--absolute-git-dir", required=False)
    candidates = [parent for parent in (journal_path.parent.resolve(), root.parent) if not parent.is_relative_to(root)]
    if admin is None:
        candidates.append(root)
    else:
        git_dir = Path(os.fsdecode(admin.rstrip(b"\n"))).resolve()
        if not git_dir.is_relative_to(root) or git_dir.is_relative_to(root / ".git"):
            candidates.append(git_dir)
    for parent in candidates:
        if parent.stat().st_dev == device and os.access(parent, os.W_OK | os.X_OK):
            return parent
    raise DelegateError("无法在项目同一卷创建应用暂存目录")


def read_scratch(journal, *, required=False, recover_allocation=False):
    if journal.get("schema_version") != 3 or not journal.get("scratch"):
        return None
    scratch = Path(journal["scratch"])
    check_path_aliases(scratch.parent, [scratch.name])
    actual_id = file_id(scratch)
    if actual_id is None and not required:
        return None
    if journal["scratch_id"] is None and recover_allocation and not required:
        info = scratch.lstat()
        # An interrupted allocation has no payload. Remove only the empty
        # directory reserved by the durable intent and owned by this user.
        if (stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
                and info.st_uid == os.getuid() and scratch.name.startswith(".cli-manager-apply-")):
            scratch.rmdir()
            return None
    if actual_id != journal["scratch_id"] or scratch.is_symlink() or not scratch.is_dir():
        raise WriteConflict(f"无法确认应用暂存目录归属：{scratch}")
    return scratch


def remove_slot(path: Path):
    if path.is_dir() and not path.is_symlink():
        path.rmdir()
    else:
        path.unlink(missing_ok=True)


def slot_content_matches(path, stamp, value):
    mode = stamp[2]
    if value["type"] == "link":
        return stat.S_ISLNK(mode) and os.readlink(path) == value["target"]
    if value["type"] == "directory":
        return stat.S_ISDIR(mode) and not os.listdir(path)
    if not stat.S_ISREG(mode):
        return False
    original = base64.b64decode(value["data"])
    if len(original) != stamp[3]:
        return False
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        if stat_stamp(os.fstat(source.fileno())) != stamp:
            return False
        return (hashlib.file_digest(source, "sha256").digest() == hashlib.sha256(original).digest()
                and stat_stamp(os.fstat(source.fileno())) == stamp)


def cleanup_application(journal, *, inspect_only=False):
    scratch = read_scratch(journal)
    if not scratch:
        return
    slots = {}
    for index, record in enumerate(journal["paths"][:journal["started"]]):
        for phase in ("forward", "undo"):
            receipt = record.get(phase) or {}
            before, after = record["before"], record["after"]
            slots[f"{phase}-{index}"] = (receipt, before, after) if phase == "forward" else (receipt, after, before)

    def check(slot):
        plan = slots.get(slot.name)
        stamp = path_stamp(slot)
        if plan is None or stamp is None:
            raise WriteConflict(f"应用暂存包含无法安全清理的内容：{slot}")
        receipt, before, after = plan
        identity = list(stamp[:2])
        retained_id = receipt.get("removed_id") or receipt.get("exchanged_id")
        shape = receipt.get("removed_shape") or receipt.get("exchanged_shape")
        if retained_id and identity == retained_id:
            expected_metadata = receipt.get("retained_metadata")
            if ([*stamp[2:5], stamp[6]] != shape or not before or not slot_content_matches(slot, stamp, before)
                    or (expected_metadata and file_metadata(slot) != expected_metadata)):
                raise WriteConflict(f"原文件内容或属性已变化，保留于：{slot}")
        elif posted := receipt.get("post_id"):
            expected_metadata = receipt.get("post_metadata")
            if (identity != posted or not after or not slot_content_matches(slot, stamp, after)
                    or (expected_metadata and file_metadata(slot) != expected_metadata)):
                raise WriteConflict(f"暂存文件身份或内容已变化，保留于：{slot}")
        elif retained_id or (stat.S_ISDIR(stamp[2]) and os.listdir(slot)):
            raise WriteConflict(f"应用暂存包含无法安全清理的内容：{slot}")
        return stamp

    contents = {slot: check(slot) for slot in scratch.iterdir()}
    if not inspect_only:
        for slot, previous in contents.items():
            if check(slot) != previous or path_stamp(slot) != previous:
                raise WriteConflict(f"清理前暂存文件发生变化：{slot}")
            remove_slot(slot)
        scratch.rmdir()


def recover_application(root: Path, journal: dict, journal_path: Path):
    conflicts = []
    recovered = set(journal.get("recovered", []))
    untouched = set(journal.get("untouched", []))
    version = journal.get("schema_version")
    scratch = read_scratch(journal, required=journal["started"] > 0, recover_allocation=True)
    for index in reversed(range(journal["started"])):
        if index in recovered:
            continue
        record = journal["paths"][index]
        path, before, after = (record[k] for k in ("path", "before", "after"))
        try:
            forward = record.get("forward") or {}
            if version == 2:
                clear_staging(root, record.get("undo") or {})
                clear_staging(root, forward)
            current, stamp = observe_entry(root, path)
            owned = index not in untouched
            replaced_id = forward.get("exchanged_id") if scratch else None
            replacement = scratch / f"forward-{index}" if replaced_id else None
            restored = (replaced_id and stamp and list(stamp[:2]) == replaced_id
                        and file_id(replacement) == forward.get("post_id"))
            if version in {2, 3} and after is not None:
                owned = owned and stamp is not None and list(stamp[:2]) == forward.get("post_id")
            elif version == 2:
                owned = owned and forward.get("removed")
            elif scratch:
                removal = scratch / f"forward-{index}"
                owned = owned and bool(forward.get("removed_id")) and file_id(removal) == forward["removed_id"]
                if owned and current is not None:
                    conflicts.append(path)
                    continue
            if current == after and owned:
                if (after is not None and (metadata := forward.get("post_metadata"))
                        and file_metadata(safe_path(root, path)) != metadata):
                    raise WriteConflict(f"已发布文件的权限或属性发生变化：{path}")
                if scratch and after is None:
                    destination = safe_path(root, path)
                    if removal.is_symlink():
                        validate_link(root, destination, os.readlink(removal))
                    rename_new(removal, destination)
                elif replacement:
                    destination = safe_path(root, path)
                    if replacement.is_symlink():
                        validate_link(root, destination, os.readlink(replacement))
                    if file_id(replacement) != replaced_id or path_stamp(destination) != stamp:
                        raise WriteConflict(f"恢复前的文件身份已变化：{path}")
                    rename_exchange(replacement, destination)
                    if file_id(replacement) != forward.get("post_id"):
                        raise WriteConflict(f"恢复时原路径被其他进程替换，两个版本都已保留：{path}")
                else:
                    undo = mutation_receipt(journal, journal_path, record, "undo") if scratch else None
                    slot = scratch / f"undo-{index}" if scratch else None
                    if slot is not None and before is not None:
                        undo_id = (record.get("undo") or {}).get("post_id")
                        if undo_id and (actual := file_id(slot)) not in (None, undo_id):
                            raise WriteConflict(f"无法确认恢复暂存文件归属：{slot}")
                        remove_slot(slot)
                    install(root, path, before, restored_mode=record.get("before_mode"), expected_stamp=stamp,
                            record=undo, slot=slot)
            elif current != before and not restored:
                conflicts.append(path)
                continue
        except (OSError, DelegateError, subprocess.SubprocessError):
            conflicts.append(path)
            continue
        recovered.add(index)
        journal["recovered"] = sorted(recovered)
        write_json(journal_path, journal)
    for relative in journal.get("created_dirs", []):
        try:
            path = safe_path(root, relative)
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
        except (OSError, DelegateError):
            # An external writer may now be using the directory.
            continue
    if not conflicts and scratch:
        try:
            cleanup_application(journal, inspect_only=True)
        except (OSError, DelegateError):
            conflicts.append(str(scratch))
    journal.update(status="conflict" if conflicts else "rolled_back", conflicts=conflicts)
    write_json(journal_path, journal)
    if not conflicts:
        cleanup_application(journal)
    return conflicts


def apply_application(root: Path, rows: list[dict], journal_path: Path, approved: str):
    removals, installs = [], []
    for record in rows:
        before, after = record["before"], record["after"]
        directory = {"type": "directory"}
        if after is None or (before is not None and (before == directory or after == directory)):
            removals.append({**record, "after": None})
            before = None
        if after is not None:
            installs.append({**record, "before": before, "before_mode": record.get("before_mode") if before else None})
    depth = lambda row: len(PurePosixPath(row["path"]).parts)
    operations = sorted(removals, key=depth, reverse=True) + sorted(installs, key=depth)
    parent = application_scratch_parent(root, rows, journal_path) if operations else None
    scratch = parent / (".cli-manager-apply-" + secrets.token_hex(12)) if parent else None
    journal = {"schema_version": 3, "status": "applying", "approved": approved, "paths": operations, "started": 0,
               "scratch": str(scratch) if scratch else None, "scratch_id": None}
    write_json(journal_path, journal)
    try:
        if scratch:
            scratch.mkdir(mode=0o700)
            journal["scratch_id"] = file_id(scratch)
            write_json(journal_path, journal)
        for i, record in enumerate(operations, start=1):
            path = record["path"]
            current, stamp = observe_entry(root, path)
            if current != record["before"]:
                raise DelegateError(f"应用过程中原目录发生变化：{path}")
            journal["started"] = i
            write_json(journal_path, journal)
            try:
                receipt = mutation_receipt(journal, journal_path, record, "forward")
                install(root, path, record["after"], expected_stamp=stamp, record=receipt,
                        slot=scratch / f"forward-{i - 1}")
            except WriteConflict:
                journal["untouched"] = [i - 1]
                write_json(journal_path, journal)
                raise
        journal["status"] = "applied"
        write_json(journal_path, journal)
    except BaseException:
        recover_application(root, journal, journal_path)
        raise
    if scratch and not any(scratch.iterdir()):
        scratch.rmdir()
        return None
    return scratch
