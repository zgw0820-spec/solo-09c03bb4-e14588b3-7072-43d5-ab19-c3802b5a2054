"""从持有者 fd 分块复制原件到恢复目录。

- 复制前重新打开 fd 并核对 (dev, inode, size)，描述符被复用到新文件时拒绝接手。
- 先写临时文件，完整复制 + 校验大小 + fsync 后再原子 rename，写完才开放下载。
- 复制持有的是我们自己的独立 fd（O_RDONLY），下载进程中途退出不影响读取。
- 复制失败清理临时文件并给出原因，条目可重新提交恢复。
"""

from __future__ import annotations

import errno
import hashlib
import os
import time

from . import procfs
from .config import settings
from .registry import Entry, HolderInfo


class RecoveryError(Exception):
    """恢复失败，message 为可直接展示给值班员的中文原因。"""


def _safe_filename(path: str, entry_id: int) -> str:
    base = os.path.basename(path) or "package"
    # 只保留保守字符集，防止路径穿越
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in base)
    return f"{entry_id:04d}_{cleaned}"


def _copy_from_holder(holder: HolderInfo, tmp_path: str, expected_size: int) -> str:
    """从单个持有者复制，返回 sha256；持有者失效时抛 RecoveryError。"""
    try:
        handle, st = procfs.open_original(holder)
    except procfs.ProcError as exc:
        raise RecoveryError(str(exc)) from exc

    try:
        ok, reason = procfs.verify_identity(st, holder)
        if not ok:
            raise RecoveryError(
                f"进程 {holder.pid} fd {holder.fd} 身份校验失败：{reason}"
            )

        digest = hashlib.sha256()
        copied = 0
        with open(tmp_path, "wb") as out:
            while True:
                try:
                    chunk = os.read(handle, settings.chunk_size)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        raise RecoveryError(
                            f"读取原件时发生 I/O 错误（{exc.strerror}）"
                        ) from exc
                    raise RecoveryError(
                        f"读取原件失败：{exc.strerror or exc}"
                    ) from exc
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
                if settings.copy_delay > 0:
                    time.sleep(settings.copy_delay)
            out.flush()
            os.fsync(out.fileno())

        if copied != expected_size:
            raise RecoveryError(
                f"复制字节数与原件不符：期望 {expected_size}，实际 {copied}"
            )
        return digest.hexdigest()
    finally:
        try:
            os.close(handle)
        except OSError:
            pass


def recover_entry(entry: Entry) -> str:
    """按条目持有者顺序逐个尝试，成功后返回恢复目录内的最终文件名。"""
    os.makedirs(settings.recovery_dir, exist_ok=True)
    final_name = _safe_filename(entry.original_path, entry.entry_id)
    final_path = os.path.join(settings.recovery_dir, final_name)
    tmp_path = os.path.join(
        settings.recovery_dir, f".tmp-{entry.entry_id}-{os.getpid()}"
    )

    failures: list[str] = []
    for holder in entry.holders:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        try:
            _copy_from_holder(holder, tmp_path, entry.size)
        except RecoveryError as exc:
            failures.append(f"pid={holder.pid},fd={holder.fd}: {exc}")
            continue

        os.replace(tmp_path, final_path)
        return final_name

    # 全部持有者都无法使用
    if os.path.exists(tmp_path):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if not failures:
        raise RecoveryError("盘点时未记录任何仍持有原件的进程，无法恢复")
    raise RecoveryError("所有持有进程均无法提供原件；" + "；".join(failures))
