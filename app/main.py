"""FastAPI 恢复服务入口。

接口：
- POST /inventory  盘点指定下载进程中“原在归档目录、已删除但仍打开”的诊断包
- POST /recover/{entry_id}  按恢复编号找回（可从任一持有进程接手）
- GET  /entries    查看全部恢复编号与状态
- GET  /recovered/{entry_id}/download  下载已救回的包（写完才开放）
- GET  /info       查看服务端目录配置
"""

from __future__ import annotations

import datetime as _dt
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from . import procfs, recovery
from .config import settings
from .procfs import FdTarget
from .registry import (
    STATUS_PENDING,
    STATUS_RECOVERED,
    STATUS_RECOVERING,
    HolderInfo,
    registry,
)
from .schemas import (
    EntryOut,
    HolderOut,
    InventoryRequest,
    InventoryResponse,
    RecoverResponse,
)

app = FastAPI(
    title="诊断包 fd 恢复服务",
    description=(
        "下载进程仍持有已删除诊断包时，通过 /proc/<pid>/fd 盘点并分块复制找回。\n\n"
        "- 同一原件 (设备号, inode) 只分配一个恢复编号\n"
        "- 描述符/路径被复用到新文件时拒绝顶替（身份校验）\n"
        "- 先写临时文件再原子改名，写完才开放下载\n"
        "- 复制使用独立只读 fd，下载进程退出后复制仍可继续"
    ),
    version="1.0.0",
)


@app.on_event("startup")
def _startup() -> None:
    os.makedirs(settings.recovery_dir, exist_ok=True)
    # 服务重启后把停在 recovering 的条目放回可重试状态
    for entry in registry.list_entries():
        registry.reset_to_pending(entry.entry_id)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _entry_to_out(entry) -> EntryOut:
    return EntryOut(
        entry_id=entry.entry_id,
        original_path=entry.original_path,
        size=entry.size,
        status=entry.status,
        holders=[
            HolderOut(
                pid=h.pid, fd=h.fd, dev=h.dev, inode=h.inode, size=h.size
            )
            for h in entry.holders
        ],
        holder_count=len(entry.holders),
        recovered_filename=entry.recovered_filename,
        sha256=entry.sha256,
        failure_reason=entry.failure_reason,
        discovered_at=entry.discovered_at,
        recovered_at=entry.recovered_at,
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/info")
def info() -> dict:
    return {
        "archive_dir": settings.archive_dir,
        "recovery_dir": settings.recovery_dir,
        "proc_root": settings.proc_root,
        "chunk_size": settings.chunk_size,
    }


@app.post("/inventory", response_model=InventoryResponse, tags=["盘点"])
def inventory(req: InventoryRequest) -> InventoryResponse:
    archive_dir = os.path.abspath(req.archive_dir or settings.archive_dir)
    pid_errors: dict[int, str] = {}
    # key=(dev, ino) -> 该原件的全部持有目标（跨进程、跨 fd 聚合）
    targets: dict[tuple[int, int], list[FdTarget]] = {}
    reused: list[int] = []

    for pid in dict.fromkeys(req.pids):  # 去重且保序
        try:
            fds = procfs.list_fds(pid)
        except procfs.ProcError as exc:
            pid_errors[pid] = str(exc)
            continue

        for fd in fds:
            target = procfs.inspect_fd(pid, fd)
            if target is None:
                continue
            if not target.deleted:
                continue  # 只盘点已删除但仍打开的
            if not procfs.path_within_archive(target.path, archive_dir):
                continue  # 原路径不在指定归档目录
            key = (target.dev, target.inode)
            bucket = targets.setdefault(key, [])
            if not any(t.pid == target.pid and t.fd == target.fd for t in bucket):
                bucket.append(target)

    discovered_outs: list[EntryOut] = []
    for (dev, inode), bucket in targets.items():
        representative = bucket[0]
        holders = [
            HolderInfo(pid=t.pid, fd=t.fd, dev=t.dev, inode=t.inode, size=t.size)
            for t in bucket
        ]
        entry, created = registry.upsert_discovered(
            dev=dev,
            inode=inode,
            original_path=representative.path,
            size=representative.size,
            holders=holders,
            discovered_at=_now(),
        )
        if not created:
            reused.append(entry.entry_id)
        discovered_outs.append(_entry_to_out(entry))

    discovered_outs.sort(key=lambda e: e.entry_id)
    return InventoryResponse(
        archive_dir=archive_dir,
        scanned_pids=req.pids,
        pid_errors=pid_errors,
        discovered=discovered_outs,
        reused_entries=reused,
    )


@app.post("/recover/{entry_id}", response_model=RecoverResponse, tags=["恢复"])
def recover(entry_id: int) -> RecoverResponse:
    entry = registry.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"恢复编号 {entry_id} 不存在")

    with registry.lock:
        # 已救回：再次提交返回同一份包（幂等）
        if entry.status == STATUS_RECOVERED and entry.recovered_filename:
            final_path = os.path.join(
                settings.recovery_dir, entry.recovered_filename
            )
            if os.path.isfile(final_path):
                return RecoverResponse(
                    entry_id=entry.entry_id,
                    status=entry.status,
                    recovered_filename=entry.recovered_filename,
                    sha256=entry.sha256,
                    download_url=f"/recovered/{entry_id}/download",
                )
            # 文件被外部删了，退回 pending 重新恢复
            entry.status = STATUS_PENDING
            entry.recovered_filename = None
            entry.sha256 = None

        if entry.status == STATUS_RECOVERING:
            raise HTTPException(
                status_code=409,
                detail=f"编号 {entry_id} 正在恢复中，请稍后查询 /entries",
            )

        # 重新探测持有者是否仍然存活，剔除已关闭/被复用的 fd
        live_holders, dead_reasons = _live_holders(entry)
        if not live_holders:
            reason = (
                "原件已无法打开：所有登记的持有进程均已退出或关闭/复用了该 fd；"
                + "；".join(dead_reasons)
            )
            registry.mark_failed(entry_id, reason)
            failed = registry.get(entry_id)
            return RecoverResponse(
                entry_id=entry_id,
                status=failed.status,
                failure_reason=failed.failure_reason,
            )
        entry.holders = live_holders
        registry.mark_recovering(entry_id)

    try:
        filename = recovery.recover_entry(entry)
    except recovery.RecoveryError as exc:
        registry.mark_failed(entry_id, str(exc))
        failed = registry.get(entry_id)
        return RecoverResponse(
            entry_id=entry_id,
            status=failed.status,
            failure_reason=failed.failure_reason,
        )

    stored = registry.get(entry_id)
    registry.mark_recovered(
        entry_id,
        filename=filename,
        sha256=stored.sha256 or _sha256_file(
            os.path.join(settings.recovery_dir, filename)
        ),
        recovered_at=_now(),
    )
    done = registry.get(entry_id)
    return RecoverResponse(
        entry_id=entry_id,
        status=done.status,
        recovered_filename=done.recovered_filename,
        sha256=done.sha256,
        download_url=f"/recovered/{entry_id}/download",
    )


def _live_holders(entry) -> tuple[list[HolderInfo], list[str]]:
    """重新打开每个登记的持有者做身份校验。

    返回 (仍持有原件的持有者, 失效持有者的原因描述)。
    """
    live: list[HolderInfo] = []
    reasons: list[str] = []
    for holder in entry.holders:
        label = f"pid={holder.pid},fd={holder.fd}"
        try:
            handle, st = procfs.open_original(holder)
        except procfs.ProcError as exc:
            reasons.append(f"{label}: {exc}")
            continue
        try:
            ok, why = procfs.verify_identity(st, holder)
            if ok:
                live.append(holder)
            else:
                reasons.append(f"{label}: {why}")
        finally:
            os.close(handle)
    return live, reasons


def _sha256_file(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@app.get("/entries", response_model=list[EntryOut], tags=["盘点"])
def list_entries() -> list[EntryOut]:
    return [_entry_to_out(e) for e in registry.list_entries()]


@app.get("/recovered/{entry_id}/download", tags=["下载"])
def download(entry_id: int) -> FileResponse:
    entry = registry.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"恢复编号 {entry_id} 不存在")
    if entry.status != STATUS_RECOVERED or not entry.recovered_filename:
        raise HTTPException(
            status_code=409,
            detail=(
                f"编号 {entry_id} 尚未恢复完成（当前状态: {entry.status}），"
                "包写完后才开放下载"
            ),
        )
    path = os.path.join(settings.recovery_dir, entry.recovered_filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=410, detail="恢复文件已不在恢复目录")
    return FileResponse(
        path,
        filename=os.path.basename(entry.original_path),
        media_type="application/octet-stream",
    )
