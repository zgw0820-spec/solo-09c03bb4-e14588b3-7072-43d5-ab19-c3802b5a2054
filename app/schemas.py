"""请求/响应模型（Swagger 文档即由此生成）。"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class InventoryRequest(BaseModel):
    pids: List[int] = Field(
        ...,
        min_length=1,
        description="要检查的下载进程 PID 列表（只检查这些进程的 fd）",
        examples=[[101, 102]],
    )

    @field_validator("pids")
    @classmethod
    def _check_positive(cls, value: List[int]) -> List[int]:
        bad = [p for p in value if p <= 0]
        if bad:
            raise ValueError(f"PID 必须为正整数，非法值: {bad}")
        return value
    archive_dir: Optional[str] = Field(
        None,
        description="归档目录，缺省使用服务端 ARCHIVE_DIR；只盘点原在该目录下的已删除文件",
    )


class HolderOut(BaseModel):
    pid: int
    fd: int
    dev: int
    inode: int
    size: int


class EntryOut(BaseModel):
    entry_id: int = Field(..., description="恢复编号，同一原件全局唯一")
    original_path: str
    size: int
    status: str = Field(
        ..., description="pending / recovering / recovered / failed"
    )
    holders: List[HolderOut]
    holder_count: int
    recovered_filename: Optional[str] = None
    sha256: Optional[str] = None
    failure_reason: Optional[str] = None
    discovered_at: Optional[str] = None
    recovered_at: Optional[str] = None


class InventoryResponse(BaseModel):
    archive_dir: str
    scanned_pids: List[int]
    pid_errors: dict[int, str]
    discovered: List[EntryOut]
    reused_entries: List[int] = Field(
        ..., description="本次盘点中命中已有恢复编号的原件（未重复分配编号）"
    )


class RecoverResponse(BaseModel):
    entry_id: int
    status: str
    recovered_filename: Optional[str] = None
    sha256: Optional[str] = None
    download_url: Optional[str] = None
    failure_reason: Optional[str] = None
