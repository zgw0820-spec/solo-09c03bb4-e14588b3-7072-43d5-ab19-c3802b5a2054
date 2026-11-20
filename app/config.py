"""运行配置，全部可通过环境变量覆盖。"""

from __future__ import annotations

import os


class Settings:
    def __init__(self) -> None:
        # 归档目录：只存放已写完、内容固定的诊断包
        self.archive_dir: str = os.path.abspath(
            os.environ.get("ARCHIVE_DIR", "/data/archive")
        )
        # 恢复目录：恢复完成的包放在这里，写完才开放下载
        self.recovery_dir: str = os.path.abspath(
            os.environ.get("RECOVERY_DIR", "/data/recovery")
        )
        self.registry_file: str = os.path.abspath(
            os.environ.get(
                "REGISTRY_FILE", os.path.join(self.recovery_dir, "registry.json")
            )
        )
        # 容器内共享宿主 pid ns 时 /proc 即宿主 procfs
        self.proc_root: str = os.environ.get("PROC_ROOT", "/proc")
        self.chunk_size: int = int(os.environ.get("COPY_CHUNK_SIZE", 8 * 1024 * 1024))
        # 测试用：每个分块复制后的额外延迟（秒），默认关闭
        self.copy_delay: float = float(os.environ.get("COPY_DELAY_SECONDS", "0"))


settings = Settings()
