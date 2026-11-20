"""恢复编号登记表（线程安全，落盘持久化）。

同一原件以 (st_dev, st_ino) 去重，全局只分配一个恢复编号；
已救回的编号再次提交恢复时直接返回同一份包，因此状态必须跨进程重启保留。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

from .config import settings

# 状态机：pending -> recovering -> recovered / failed
STATUS_PENDING = "pending"
STATUS_RECOVERING = "recovering"
STATUS_RECOVERED = "recovered"
STATUS_FAILED = "failed"


@dataclass
class HolderInfo:
    pid: int
    fd: int
    dev: int
    inode: int
    size: int

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "fd": self.fd,
            "dev": self.dev,
            "ino": self.inode,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HolderInfo":
        return cls(
            pid=int(data["pid"]),
            fd=int(data["fd"]),
            dev=int(data["dev"]),
            inode=int(data["ino"]),
            size=int(data["size"]),
        )


@dataclass
class Entry:
    entry_id: int
    dev: int
    inode: int
    original_path: str
    size: int
    status: str = STATUS_PENDING
    holders: list[HolderInfo] = field(default_factory=list)
    recovered_filename: Optional[str] = None
    sha256: Optional[str] = None
    failure_reason: Optional[str] = None
    last_error: Optional[str] = None
    discovered_at: Optional[str] = None
    recovered_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "dev": self.dev,
            "ino": self.inode,
            "original_path": self.original_path,
            "size": self.size,
            "status": self.status,
            "holders": [h.to_dict() for h in self.holders],
            "recovered_filename": self.recovered_filename,
            "sha256": self.sha256,
            "failure_reason": self.failure_reason,
            "last_error": self.last_error,
            "discovered_at": self.discovered_at,
            "recovered_at": self.recovered_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Entry":
        return cls(
            entry_id=int(data["entry_id"]),
            dev=int(data["dev"]),
            inode=int(data["ino"]),
            original_path=data["original_path"],
            size=int(data["size"]),
            status=data["status"],
            holders=[HolderInfo.from_dict(h) for h in data.get("holders", [])],
            recovered_filename=data.get("recovered_filename"),
            sha256=data.get("sha256"),
            failure_reason=data.get("failure_reason"),
            last_error=data.get("last_error"),
            discovered_at=data.get("discovered_at"),
            recovered_at=data.get("recovered_at"),
        )


class Registry:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._entries: dict[int, Entry] = {}
        self._key_to_id: dict[tuple[int, int], int] = {}
        self._next_id = 1
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            # 登记表损坏时不阻塞服务，以空表起步（旧的恢复文件仍在磁盘上）
            return
        for raw in payload.get("entries", []):
            entry = Entry.from_dict(raw)
            self._entries[entry.entry_id] = entry
            self._key_to_id[(entry.dev, entry.inode)] = entry.entry_id
        self._next_id = max(
            [self._next_id] + [e.entry_id + 1 for e in self._entries.values()]
        )
        self._reconcile_with_disk()

    def _reconcile_with_disk(self) -> None:
        """启动时核对：标记为 recovered 但文件已不在恢复目录的，退回 pending。"""
        for entry in self._entries.values():
            if entry.status == STATUS_RECOVERED and entry.recovered_filename:
                full = os.path.join(settings.recovery_dir, entry.recovered_filename)
                if not os.path.isfile(full):
                    entry.status = STATUS_PENDING
                    entry.recovered_filename = None
                    entry.sha256 = None
                    entry.failure_reason = None

    def _persist_locked(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        payload = {
            "version": 1,
            "entries": [e.to_dict() for e in self._entries.values()],
        }
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)

    # ---------- 查询 ----------

    def get(self, entry_id: int) -> Optional[Entry]:
        with self._lock:
            return self._entries.get(entry_id)

    def list_entries(self) -> list[Entry]:
        with self._lock:
            return list(self._entries.values())

    # ---------- 盘点登记 ----------

    def upsert_discovered(
        self,
        *,
        dev: int,
        inode: int,
        original_path: str,
        size: int,
        holders: list[HolderInfo],
        discovered_at: str,
    ) -> tuple[Entry, bool]:
        """按 (dev, ino) 登记或刷新一个原件，返回 (条目, 是否本次新建)。"""
        with self._lock:
            existing_id = self._key_to_id.get((dev, inode))
            if existing_id is not None:
                entry = self._entries[existing_id]
                # 刷新持有者快照；失败的条目在重新盘点后允许再次尝试
                entry.holders = list(holders)
                if entry.status == STATUS_FAILED:
                    entry.status = STATUS_PENDING
                    entry.failure_reason = None
                self._persist_locked()
                return entry, False

            entry = Entry(
                entry_id=self._next_id,
                dev=dev,
                inode=inode,
                original_path=original_path,
                size=size,
                holders=list(holders),
                discovered_at=discovered_at,
            )
            self._next_id += 1
            self._entries[entry.entry_id] = entry
            self._key_to_id[(dev, entry.inode)] = entry.entry_id
            self._persist_locked()
            return entry, True

    # ---------- 恢复状态流转 ----------

    def mark_recovering(self, entry_id: int) -> Entry:
        with self._lock:
            entry = self._entries[entry_id]
            entry.status = STATUS_RECOVERING
            entry.last_error = None
            self._persist_locked()
            return entry

    def mark_recovered(
        self, entry_id: int, filename: str, sha256: str, recovered_at: str
    ) -> None:
        with self._lock:
            entry = self._entries[entry_id]
            entry.status = STATUS_RECOVERED
            entry.recovered_filename = filename
            entry.sha256 = sha256
            entry.failure_reason = None
            entry.last_error = None
            entry.recovered_at = recovered_at
            self._persist_locked()

    def mark_failed(self, entry_id: int, reason: str) -> None:
        with self._lock:
            entry = self._entries[entry_id]
            entry.status = STATUS_FAILED
            entry.failure_reason = reason
            entry.last_error = reason
            self._persist_locked()

    def reset_to_pending(self, entry_id: int) -> None:
        """recovering 中服务重启等场景，回到 pending 以便重试。"""
        with self._lock:
            entry = self._entries[entry_id]
            if entry.status == STATUS_RECOVERING:
                entry.status = STATUS_PENDING
                self._persist_locked()

    @property
    def lock(self) -> threading.RLock:
        return self._lock


registry = Registry(settings.registry_file)
