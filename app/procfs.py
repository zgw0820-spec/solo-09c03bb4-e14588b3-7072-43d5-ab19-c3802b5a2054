"""只依赖 /proc 的描述符盘点与原件身份校验。

要点：
- 通过 readlink(/proc/<pid>/fd/<fd>) 判断 fd 指向的路径以及是否已删除（"(deleted)"）。
- 通过 fstat 取得设备号 + inode 作为原件身份；描述符被复用到新文件后，
  即使路径相同，(st_dev, st_ino) 也会变化，不能顶替原件。
- 所有打开的 fd 均在使用后立即关闭，盘点结束不留任何原件句柄。
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass

from .config import settings

_DELETED_SUFFIX = " (deleted)"


class ProcError(Exception):
    """procfs 访问层面的错误（进程不存在、无权限等）。"""


@dataclass(frozen=True)
class FdTarget:
    pid: int
    fd: int
    path: str           # 去掉 " (deleted)" 后缀后的原始路径
    deleted: bool
    dev: int
    inode: int
    size: int


def _fd_dir(pid: int) -> str:
    return os.path.join(settings.proc_root, str(pid), "fd")


def read_cmdline(pid: int) -> str:
    """尽力读取进程命令行，失败时返回空串。"""
    try:
        with open(os.path.join(settings.proc_root, str(pid), "cmdline"), "rb") as fh:
            data = fh.read()
        return data.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def pid_alive(pid: int) -> bool:
    return os.path.isdir(os.path.join(settings.proc_root, str(pid)))


def list_fds(pid: int) -> list[int]:
    """列出进程当前打开的 fd 编号；进程不存在抛 ProcError。"""
    try:
        names = os.listdir(_fd_dir(pid))
    except FileNotFoundError as exc:
        raise ProcError(f"进程 {pid} 不存在或已退出") from exc
    except PermissionError as exc:
        raise ProcError(f"无权读取进程 {pid} 的 fd 目录（{exc.strerror}）") from exc
    except OSError as exc:
        raise ProcError(f"读取进程 {pid} 的 fd 目录失败: {exc.strerror}") from exc

    fds: list[int] = []
    for name in names:
        try:
            fds.append(int(name))
        except ValueError:
            continue  # fd 目录里理论上只有整数命名的条目
    return fds


def inspect_fd(pid: int, fd: int) -> FdTarget | None:
    """读取单个 fd 的指向与身份；fd 已关闭/不可访问时返回 None。"""
    link_path = os.path.join(_fd_dir(pid), str(fd))
    try:
        target = os.readlink(link_path)
    except OSError:
        return None

    deleted = target.endswith(_DELETED_SUFFIX)
    path = target[: -len(_DELETED_SUFFIX)] if deleted else target

    # 以只读方式短暂打开以拿到 fstat。
    # 注意：/proc/<pid>/fd/N 是内核维护的魔法符号链接，O_NOFOLLOW 会返回 ELOOP，
    # 不能使用；其指向由内核保证，原件身份改由 fstat 的 (dev, inode) 校验。
    handle: int | None = None
    try:
        handle = os.open(link_path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(handle)
    except OSError:
        return None
    finally:
        if handle is not None:
            os.close(handle)
            handle = None

    if not stat_is_regular(st):
        return None

    return FdTarget(
        pid=pid,
        fd=fd,
        path=path,
        deleted=deleted,
        dev=st.st_dev,
        inode=st.st_ino,
        size=st.st_size,
    )


def stat_is_regular(st: os.stat_result) -> bool:
    import stat as stat_mod

    return stat_mod.S_ISREG(st.st_mode)


def path_within_archive(path: str, archive_dir: str) -> bool:
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(archive_dir)
    except OSError:
        return False
    return real == base or real.startswith(base + os.sep)


@dataclass
class Holder:
    """一个仍持有原件的 (pid, fd)。"""

    pid: int
    fd: int
    dev: int
    inode: int
    size: int


def open_original(holder: Holder) -> tuple[int, object]:
    """重新打开某个持有者的 fd，拿到只读句柄和 fstat 结果。

    返回 (文件描述符, stat_result)；任何一步失败抛 ProcError。
    调用方负责 close 返回的描述符。
    """
    link_path = os.path.join(
        settings.proc_root, str(holder.pid), "fd", str(holder.fd)
    )
    try:
        # 同上：procfs 魔法符号链接不能加 O_NOFOLLOW
        handle = os.open(link_path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise ProcError(
                f"进程 {holder.pid} 的 fd {holder.fd} 已关闭或进程已退出"
            ) from exc
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise ProcError(
                f"无权打开进程 {holder.pid} 的 fd {holder.fd}（{exc.strerror}）"
            ) from exc
        raise ProcError(
            f"打开进程 {holder.pid} 的 fd {holder.fd} 失败: {exc.strerror}"
        ) from exc
    try:
        st = os.fstat(handle)
    except OSError as exc:
        os.close(handle)
        raise ProcError(f"fstat 原件失败: {exc.strerror}") from exc
    return handle, st


def verify_identity(
    st: os.stat_result, holder: Holder
) -> tuple[bool, str]:
    """核对重新打开的文件是否仍是登记的原件（防描述符复用顶替）。"""
    import stat as stat_mod

    if not stat_mod.S_ISREG(st.st_mode):
        return False, "描述符已不再指向普通文件"
    if (st.st_dev, st.st_ino) != (holder.dev, holder.inode):
        return False, (
            f"描述符已被复用到新文件"
            f"（登记 dev={holder.dev},ino={holder.inode}；"
            f"当前 dev={st.st_dev},ino={st.st_ino}）"
        )
    if st.st_size != holder.size:
        return False, (
            f"原件大小发生变化（登记 {holder.size} 字节，当前 {st.st_size} 字节）"
        )
    return True, ""
