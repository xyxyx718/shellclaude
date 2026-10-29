#!/usr/bin/env python3
"""Install/update shellclaude on a-Shell using only the Python standard library."""
import argparse
import ast
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from urllib.parse import quote
from urllib.request import Request, urlopen

REPOSITORY = "xyxyx718/shellclaude"
DEFAULT_REF = "feature/chatgpt-sign-in"
FILES = (
    "shellclaude.py", "shellclaude_auth.py", "shellclaude_responses.py", "install.py",
    "defaults/AGENTS.md", "defaults/agents-history.json", "requirements-chatgpt.txt", "README.md",
)


def download(url):
    req = Request(url, headers={"User-Agent": "shellclaude-installer", "Accept": "application/vnd.github+json"})
    try:
        with urlopen(req, timeout=60) as response:
            data = response.read(4_000_001)
        if not data or len(data) > 4_000_000:
            raise ValueError("文件为空或过大")
        return data
    except Exception as exc:
        raise RuntimeError(f"下载失败：{url}（{type(exc).__name__}）。原程序文件尚未替换。") from None


def resolve_commit(ref):
    data = json.loads(download(f"https://api.github.com/repos/{REPOSITORY}/commits/{quote(ref, safe='')}"))
    sha = data.get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("GitHub 返回了无效的提交标识。")
    return sha


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


def install(destination, ref=DEFAULT_REF):
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
        commit = resolve_commit(ref)
        with tempfile.TemporaryDirectory(prefix=".shellclaude-update-", dir=destination) as staging:
            staging = Path(staging)
            previous, changed = {}, []
            (staging / "old").mkdir()
            # Complete every download, parse and backup before replacing anything.
            for relative in FILES:
                target = destination / relative
                if target.is_symlink() or not target.resolve().is_relative_to(destination):
                    raise RuntimeError(f"安装目标是符号链接或位于安装目录之外：{relative}")
                body = download(f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/{relative}")
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
            try:
                for relative in FILES:
                    # Include this path before replacing, so a partial write also rolls back.
                    changed.append(relative)
                    publish(staging / "new" / relative, destination / relative)
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
    args = parser.parse_args()
    try:
        commit = install(args.dir, args.ref)
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
