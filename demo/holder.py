#!/usr/bin/env python3
"""本地演示用“下载进程”：写完样包并打开后删除目录项，继续持有原件 fd。

行为：
1. 在归档目录生成若干内容固定的样包（写完 fsync，之后不再修改）。
2. fork 出 --holders 个持有者进程，各自以只读方式打开样包。
3. 所有持有者就绪后，父进程 unlink 样包的目录项（文件已删除但 fd 仍打开）。
4. 父进程把 pid、包路径、各持有者的 fd 编号以 JSON 行输出到 stdout。
5. SIGUSR1：持有者把原 fd 通过 dup2 复用到“同路径的新文件”（新 inode、新内容），
   用于演示描述符/路径被复用后不能顶替原件。
6. SIGTERM/SIGINT：持有者退出（也用于演示复制过程中读者退出）。

注意：本脚本只生成演示数据，不参与恢复服务逻辑。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time


def parse_packages(specs: list[str]) -> list[tuple[str, int]]:
    packages: list[tuple[str, int]] = []
    for spec in specs:
        if "=" not in spec:
            raise SystemExit(f"--package 格式应为 名称=字节数: {spec}")
        name, size_s = spec.rsplit("=", 1)
        size = int(size_s)
        if not name or size <= 0:
            raise SystemExit(f"非法包定义: {spec}")
        if os.sep in name or name in (".", ".."):
            raise SystemExit(f"包名非法: {name}")
        packages.append((name, size))
    return packages


def write_sample(path: str, size: int, seed: int) -> None:
    """生成确定性内容的样包，写完 fsync。"""
    with open(path, "wb") as fh:
        block = bytes((i * 31 + seed) & 0xFF for i in range(64 * 1024))
        remaining = size
        while remaining > 0:
            chunk = block[: min(len(block), remaining)]
            fh.write(chunk)
            remaining -= len(chunk)
        fh.flush()
        os.fsync(fh.fileno())


def child_main(
    archive_dir: str,
    packages: list[tuple[str, int]],
    ready_fd: int,
) -> None:
    held: dict[str, int] = {}
    try:
        for name, _size in packages:
            path = os.path.join(archive_dir, name)
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
            held[name] = fd
    except OSError as exc:
        os.write(ready_fd, json.dumps({"error": os.strerror(exc.errno)}).encode())
        os._exit(2)

    # 报告本持有者的 pid 与 fd 编号
    report = {"pid": os.getpid(), "fds": {n: fd for n, fd in held.items()}}
    os.write(ready_fd, (json.dumps(report) + "\n").encode())

    def on_usr1(signum, frame):  # noqa: ANN001
        for name, old_fd in held.items():
            # 只复用显式打了标记的包：标记文件 <archive>/.<name>.reuse
            marker = os.path.join(archive_dir, f".{name}.reuse")
            if not os.path.exists(marker):
                continue
            path = os.path.join(archive_dir, name)
            # 在同一路径创建一个全新文件（新 inode、不同内容）
            with open(path, "wb") as fh:
                fh.write(b"REPLACEMENT-NOT-ORIGINAL:" + os.urandom(64))
                fh.flush()
                os.fsync(fh.fileno())
            new_fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.dup2(new_fd, old_fd)  # 原 fd 编号现在指向新文件
            finally:
                os.close(new_fd)

    def on_term(signum, frame):  # noqa: ANN001
        os._exit(0)

    signal.signal(signal.SIGUSR1, on_usr1)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # 后台 shell 退出不连带杀死持有者

    while True:
        signal.pause()


def main() -> int:
    ap = argparse.ArgumentParser(description="fd 恢复服务的演示下载进程")
    ap.add_argument("--archive", required=True, help="归档目录（样包写在这里再删除目录项）")
    ap.add_argument(
        "--package",
        action="append",
        required=True,
        help="样包定义 名称=字节数，可重复传入",
    )
    ap.add_argument("--holders", type=int, default=2, help="持有者进程数（默认 2）")
    ap.add_argument("--duration", type=int, default=86400, help="最长存活秒数")
    ap.add_argument("--pidfile", help="把持有者 pid 列表写入该文件（JSON）")
    args = ap.parse_args()

    packages = parse_packages(args.package)
    archive_dir = os.path.abspath(args.archive)
    os.makedirs(archive_dir, exist_ok=True)

    # 1. 父进程生成内容固定的样包
    for idx, (name, size) in enumerate(packages):
        write_sample(os.path.join(archive_dir, name), size, seed=idx + 1)

    # 2. fork 持有者，子进程通过管道回报就绪信息
    read_fd, write_fd = os.pipe()
    child_pids: list[int] = []
    for _ in range(args.holders):
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            child_main(archive_dir, packages, write_fd)
        child_pids.append(pid)
    os.close(write_fd)

    # 读取全部持有者的就绪报告
    reports: list[dict] = []
    chunks = b""
    while len(reports) < args.holders:
        data = os.read(read_fd, 65536)
        if not data:
            break
        chunks += data
        while b"\n" in chunks:
            line, chunks = chunks.split(b"\n", 1)
            payload = json.loads(line.decode())
            if "error" in payload:
                print(json.dumps({"event": "holder_error", **payload}), flush=True)
                return 2
            reports.append(payload)
    os.close(read_fd)

    # 3. 删除目录项（所有持有者仍持有原件）
    for name, _size in packages:
        os.unlink(os.path.join(archive_dir, name))

    if args.pidfile:
        with open(args.pidfile, "w", encoding="utf-8") as fh:
            json.dump({"pids": child_pids}, fh)

    ready = {
        "event": "ready",
        "holder_pids": child_pids,
        "archive_dir": archive_dir,
        "packages": [
            {"name": name, "path": os.path.join(archive_dir, name), "size": size}
            for name, size in packages
        ],
        "holders": reports,
    }
    print(json.dumps(ready, ensure_ascii=False), flush=True)

    # 4. 父进程转发信号；忽略 SIGHUP，避免父所在终端/后台 shell 退出时连带
    #    终止持有者（模拟长期运行的下载进程）
    def forward(sig: int):
        def handler(signum, frame):  # noqa: ANN001
            for pid in child_pids:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass

        return handler

    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGUSR1, forward(signal.SIGUSR1))
    signal.signal(signal.SIGTERM, forward(signal.SIGTERM))
    signal.signal(signal.SIGINT, forward(signal.SIGTERM))

    deadline = time.monotonic() + args.duration
    try:
        while time.monotonic() < deadline:
            # 非阻塞回收已退出的子进程，避免僵尸；不影响仍存活的持有者
            try:
                while True:
                    finished, _ = os.waitpid(-1, os.WNOHANG)
                    if finished == 0:
                        break
            except ChildProcessError:
                pass
            # 只等全部子进程退出才结束；个别持有者被 kill 不影响其余持有者
            alive = any(os.path.isdir(f"/proc/{pid}") for pid in child_pids)
            if not alive:
                break
            time.sleep(0.2)
    finally:
        for pid in child_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for pid in child_pids:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
