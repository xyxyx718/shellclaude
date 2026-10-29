#!/usr/bin/env python3
"""Install/update shellclaude on a-Shell using only the Python standard library."""
import argparse
import ast
import contextlib
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPOSITORY = "xyxyx718/shellclaude"
DEFAULT_REF = "feature/chatgpt-sign-in"
MANIFEST = "release-files.sha256"
LEGACY_MODULE_HASHES = {'shellclaude_auth.py': '26a8ad00e0b3c7a76ec5932917bad43db22fc8987c63c6503612991deba1ad7e', 'shellclaude_responses.py': 'c8967a7ff45f7924f39a0fa8e143819e4ec5b917d76f33d7db5a5e649e796286'}


def parse_manifest(body):
    entries = {}
    for line in body.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)", line)
        if not match:
            raise ValueError("发布清单格式错误")
        digest, path = match.groups()
        parts = path.split("/")
        if any(part in (".", "..") for part in parts) or path in entries or not (
            path in {"shellclaude.py", "install.py", "install.sh", "requirements-chatgpt.txt", "README.md"}
            or (len(parts) == 2 and parts[0] in {"shellclaude_lib", "defaults"})
        ):
            raise ValueError("发布清单含不允许的路径")
        entries[path] = digest
    if not {"shellclaude.py", "install.py", "install.sh", "shellclaude_lib/__init__.py", "shellclaude_lib/auth.py", "shellclaude_lib/responses.py", "defaults/AGENTS.md", "defaults/agents-history.json"} <= entries.keys():
        raise ValueError("发布清单缺少必要文件")
    return entries


def download(url):
    req = Request(url, headers={"User-Agent": "shellclaude-installer", "Accept": "application/vnd.github+json"})
    try:
        with urlopen(req, timeout=60) as response:
            data = response.read(4_000_001)
        if not data or len(data) > 4_000_000:
            raise ValueError("文件为空或过大")
        return data
    except HTTPError as exc:
        raise RuntimeError(f"下载失败：HTTP {exc.code}：{url}。原程序文件尚未替换。") from None
    except Exception as exc:
        raise RuntimeError(f"下载失败：{url}（{type(exc).__name__}）。原程序文件尚未替换。") from None




def validate(relative, data):
    text = data.decode("utf-8")
    if relative.endswith(".py"):
        ast.parse(text, filename=relative)
    elif relative == "defaults/agents-history.json":
        history = json.loads(text)
        if not isinstance(history, list) or any(not isinstance(x, str) or not re.fullmatch(r"[0-9a-f]{64}", x) for x in history):
            raise ValueError("平台规则历史格式错误")
    elif not text.strip():
        raise ValueError(f"空文件：{relative}")


def publish(source, target):
    """Same-filesystem staging and replacement; also used for rollback."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".install-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            with contextlib.suppress(OSError):
                os.fchmod(output.fileno(), source.stat().st_mode & 0o777)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(destination, ref=DEFAULT_REF, source=None):
    base = f"https://raw.githubusercontent.com/{REPOSITORY}/{quote(ref, safe='/')}/"
    source = Path(source).resolve() if source else None
    manifest_body = (source / MANIFEST).read_bytes() if source else download(base + MANIFEST)
    entries = parse_manifest(manifest_body)
    commit = hashlib.sha256(manifest_body).hexdigest()
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    data_dir = destination / "shellclaude-data"
    if data_dir.is_symlink() or not data_dir.resolve().is_relative_to(destination):
        raise RuntimeError("数据目录链接到安装目录之外，请先整理安装路径。")
    data_dir.mkdir(mode=0o700, exist_ok=True)
    # SQLite locks are crash-released and do not require fork or fcntl.
    with contextlib.closing(sqlite3.connect(data_dir / ".installer.sqlite3", timeout=0)) as lock:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            raise RuntimeError("另一个安装/更新任务正在运行，请稍后重试。") from None
        with tempfile.TemporaryDirectory(prefix=".shellclaude-update-", dir=destination) as staging:
            staging = Path(staging)
            previous, changed = {}, []
            (staging / "old").mkdir()
            # Complete every download, parse and backup before replacing anything.
            for relative in entries:
                target = destination / relative
                if target.is_symlink() or not target.resolve().is_relative_to(destination):
                    raise RuntimeError(f"安装目标是符号链接或位于安装目录之外：{relative}")
                body = (source / relative).read_bytes() if source else download(base + relative)
                if hashlib.sha256(body).hexdigest() != entries[relative]:
                    raise ValueError(f"文件校验失败：{relative}，请重新运行更新。")
                validate(relative, body)
                staged = staging / "new" / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(body)
                if target.exists():
                    backup = staging / "old" / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                    previous[relative] = backup
                else:
                    previous[relative] = None
            obsolete = []
            for relative, digest in LEGACY_MODULE_HASHES.items():
                target = destination / relative
                if target.is_file() and not target.is_symlink() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                    backup = staging / "old" / relative
                    shutil.copy2(target, backup)
                    previous[relative] = backup
                    obsolete.append(relative)
            try:
                for relative in entries:
                    # Include this path before replacing, so a partial write also rolls back.
                    changed.append(relative)
                    publish(staging / "new" / relative, destination / relative)
                for relative in obsolete:
                    changed.append(relative)
                    (destination / relative).unlink()
                lock.execute("CREATE TABLE IF NOT EXISTS installed (commit_sha TEXT)")
                lock.execute("DELETE FROM installed")
                lock.execute("INSERT INTO installed VALUES (?)", (commit,))
                lock.commit()
            except BaseException:
                failures = []
                for relative in reversed(changed):
                    try:
                        if previous[relative] is not None:
                            publish(previous[relative], destination / relative)
                        else:
                            (destination / relative).unlink(missing_ok=True)
                    except OSError:
                        failures.append(relative)
                if failures:
                    # Preserve rollback material outside the temporary directory.
                    recovery = Path(tempfile.mkdtemp(prefix="shellclaude-recovery-", dir=data_dir))
                    shutil.copytree(staging / "old", recovery / "old", dirs_exist_ok=True)
                    raise RuntimeError(f"回滚未完成，原文件备份保存在 {recovery}，请先恢复再启动。") from None
                raise
    return commit


def main():
    parser = argparse.ArgumentParser(description="安装或更新 shellclaude（保留配置、会话和自定义规则）")
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent), help="安装目录，默认安装脚本所在目录")
    parser.add_argument("--ref", default=DEFAULT_REF, help="GitHub 分支、标签或提交")
    parser.add_argument("--source", help="由 shell 脚本下载的暂存目录")
    parser.add_argument("--list", dest="list_manifest", help="验证清单并输出下载路径")
    args = parser.parse_args()
    if args.list_manifest:
        for path in parse_manifest(Path(args.list_manifest).read_bytes()):
            print(path)
        return 0
    try:
        commit = install(args.dir, args.ref, args.source)
    except (OSError, ValueError, SyntaxError, RuntimeError, sqlite3.Error) as exc:
        print(f"安装/更新失败：{exc}")
        return 1
    print(f"安装/更新完成：{Path(args.dir).expanduser().resolve()}（{commit[:12]}）")
    print("配置、会话、自定义 AGENTS.md 和登录凭据已保留。")
    print("启动：python3 shellclaude.py")
    print("首次使用 ChatGPT 登录时安装依赖：python3 -m pip install -r requirements-chatgpt.txt")
    print("可选彩色显示：python3 -m pip install rich")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
