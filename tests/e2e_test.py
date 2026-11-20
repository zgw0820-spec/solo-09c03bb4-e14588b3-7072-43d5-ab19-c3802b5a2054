#!/usr/bin/env python3
"""端到端测试（仅用标准库，配合 uvicorn + demo/holder.py 运行）。

覆盖场景：
1. 两个持有者打开同一批样包后删除目录项 -> 盘点同一原件只给一个恢复编号、
   持有者聚合为 2；重复盘点不产生新编号。
2. 描述符复用（SIGUSR1：同路径新文件 dup2 覆盖原 fd）-> 恢复拒绝顶替，
   原因指明 inode 变化；已恢复的包内容仍是原件而非新文件。
3. 复制过程中持有者退出（SIGTERM）-> 服务凭自己的独立 fd 继续复制成功。
4. 已救回编号再次恢复 -> 返回同一份包（幂等）；下载字节与原件一致。
5. 未恢复完成不开放下载；无持有者时恢复给出明确原因。
6. 盘点后服务进程没有残留指向归档目录的打开句柄。
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOLDER = ROOT / "demo" / "holder.py"

PKG_A = ("diag-bundle-a.tar.gz", 2 * 1024 * 1024)
PKG_B = ("diag-bundle-b.zip", 1024 * 1024)
COPY_DELAY = 0.1  # 秒/分块，制造“复制中”窗口


def expected_sha256(path_name: str, size: int) -> str:
    """复刻 holder.write_sample 的确定性内容。"""
    seed = 1 if path_name == PKG_A[0] else 2
    digest = hashlib.sha256()
    block = bytes((i * 31 + seed) & 0xFF for i in range(64 * 1024))
    remaining = size
    while remaining > 0:
        chunk = block[: min(len(block), remaining)]
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def http(method: str, url: str, body: dict | None = None, raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            if raw:
                return resp.status, payload
            return resp.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if raw:
            return exc.code, payload
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"raw": payload.decode(errors="replace")}
        return exc.code, parsed


def wait_ready(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"服务未在 {timeout}s 内就绪: {url}")


def wait_pid_exit(pid: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not os.path.isdir(f"/proc/{pid}"):
            return
        time.sleep(0.1)
    raise RuntimeError(f"pid {pid} 未按预期退出")


def count_open_under(api_pid: int, prefix: str) -> list[str]:
    out = []
    fd_dir = f"/proc/{api_pid}/fd"
    try:
        for name in os.listdir(fd_dir):
            try:
                target = os.readlink(os.path.join(fd_dir, name))
            except OSError:
                continue
            if target.startswith(prefix):
                out.append(f"{name}->{target}")
    except FileNotFoundError:
        pass
    return out


class Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append(f"{name} {detail}")
            print(f"  FAIL  {name} {detail}")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="e2e-recovery-"))
    archive = work / "archive"
    recovery = work / "recovery"
    archive.mkdir()
    recovery.mkdir()
    venv_python = os.environ.get("PYTHON_BIN", sys.executable)
    print(f"工作目录: {work}")

    env = os.environ.copy()
    env.update(
        {
            "ARCHIVE_DIR": str(archive),
            "RECOVERY_DIR": str(recovery),
            "REGISTRY_FILE": str(recovery / "registry.json"),
            "COPY_DELAY_SECONDS": str(COPY_DELAY),
            "PYTHONUNBUFFERED": "1",
        }
    )

    # 1) 启动演示持有者（2 个持有者进程，两个包）
    holder_proc = subprocess.Popen(
        [
            venv_python, "-I", str(HOLDER),
            "--archive", str(archive),
            "--package", f"{PKG_A[0]}={PKG_A[1]}",
            "--package", f"{PKG_B[0]}={PKG_B[1]}",
            "--holders", "2",
            "--pidfile", str(recovery / "demo-pids.json"),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = holder_proc.stdout.readline()
    ready = json.loads(line)
    assert ready["event"] == "ready", ready
    holder_pids = ready["holder_pids"]
    print(f"持有者 pid: {holder_pids}")
    checks = Checks()

    # 2) 启动 API
    api_proc = subprocess.Popen(
        [
            venv_python, "-m", "uvicorn",
            "app.main:app", "--host", "127.0.0.1", "--port", "8000",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = "http://127.0.0.1:8000"
    try:
        wait_ready(base + "/info")
        api_pid = api_proc.pid

        # 目录项确实已删除
        checks.check(
            "样包目录项已删除",
            not (archive / PKG_A[0]).exists()
            and not (archive / PKG_B[0]).exists(),
        )

        # 3) 盘点：去重 + 持有者聚合
        code, resp = http("POST", base + "/inventory", {"pids": holder_pids})
        checks.check("盘点返回 200", code == 200, str(resp))
        entries = {e["original_path"].rsplit("/", 1)[-1]: e for e in resp["discovered"]}
        checks.check("发现两个原件", len(resp["discovered"]) == 2, str(len(resp["discovered"])))
        id_a = entries[PKG_A[0]]["entry_id"]
        id_b = entries[PKG_B[0]]["entry_id"]
        checks.check("两个原件编号不同", id_a != id_b)
        checks.check(
            "同一原件跨进程只分配一个编号且聚合 2 个持有者",
            all(e["holder_count"] == 2 for e in resp["discovered"]),
            str([(e["entry_id"], e["holder_count"]) for e in resp["discovered"]]),
        )

        # 重复盘点：编号复用
        code, resp2 = http("POST", base + "/inventory", {"pids": holder_pids})
        checks.check("重复盘点编号复用", set(resp2["reused_entries"]) == {id_a, id_b})
        checks.check(
            "重复盘点不产生新编号",
            sorted(e["entry_id"] for e in resp2["discovered"]) == sorted([id_a, id_b]),
        )

        # 4) 描述符复用：对 A 的持有者发 SIGUSR1（同路径新文件 dup2 覆盖 fd）
        # 仅给 A 打复用标记，B 保持持有原件不动
        (archive / f".{PKG_A[0]}.reuse").write_text("1")
        for pid in holder_pids:
            os.kill(pid, signal.SIGUSR1)
        time.sleep(0.5)
        # 同路径现在是新文件（新 inode）
        replacement = archive / PKG_A[0]
        checks.check("复用后同路径出现新文件", replacement.exists())

        # 再盘点：A 不再出现（fd 指向的新文件未删除），编号保留
        code, resp3 = http("POST", base + "/inventory", {"pids": holder_pids})
        paths3 = {e["original_path"].rsplit("/", 1)[-1] for e in resp3["discovered"]}
        checks.check("复用后 A 不再被盘点为已删除原件", PKG_A[0] not in paths3, str(paths3))

        # 尝试恢复 A：身份校验必须失败
        code, resp_a = http("POST", f"{base}/recover/{id_a}")
        checks.check("复用后恢复 A 返回失败状态", code == 200 and resp_a["status"] == "failed", str(resp_a))
        checks.check(
            "失败原因指出 fd 被复用/无法打开",
            resp_a.get("failure_reason") is not None
            and ("复用" in resp_a["failure_reason"] or "无法打开" in resp_a["failure_reason"]),
            str(resp_a.get("failure_reason")),
        )
        # 未恢复完成不开放下载
        code_dl, _ = http("GET", f"{base}/recovered/{id_a}/download")
        checks.check("未恢复完成不开放下载(409)", code_dl == 409, str(code_dl))

        # 5) B：复制中读者退出，复制仍成功
        # 先触发恢复（后台线程），等临时文件出现后杀掉全部持有者
        import threading

        result_box: dict = {}

        def do_recover():
            result_box["result"] = http("POST", f"{base}/recover/{id_b}")

        worker = threading.Thread(target=do_recover)
        worker.start()
        tmp_glob = list(recovery.glob(f".tmp-{id_b}-*"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not tmp_glob:
            tmp_glob = list(recovery.glob(f".tmp-{id_b}-*"))
            time.sleep(0.05)
        checks.check("恢复已开始并生成临时文件", bool(tmp_glob))
        for pid in holder_pids:
            os.kill(pid, signal.SIGTERM)
        for pid in holder_pids:
            wait_pid_exit(pid)
        worker.join(timeout=30)
        code_b, resp_b = result_box["result"]
        checks.check("持有者退出后恢复 B 成功", code_b == 200 and resp_b["status"] == "recovered", str(resp_b))
        checks.check("返回 sha256", bool(resp_b.get("sha256")))

        final_b = recovery / resp_b["recovered_filename"]
        checks.check("临时文件已原子改名", final_b.exists() and not list(recovery.glob(".tmp-*")))
        checks.check(
            "B 内容与原件一致",
            hashlib.sha256(final_b.read_bytes()).hexdigest() == expected_sha256(*PKG_B),
        )

        # 6) 幂等：再次恢复同一编号返回同一份
        code, resp_b2 = http("POST", f"{base}/recover/{id_b}")
        checks.check(
            "重复恢复返回同一份包",
            resp_b2["status"] == "recovered"
            and resp_b2["recovered_filename"] == resp_b["recovered_filename"]
            and resp_b2["sha256"] == resp_b["sha256"],
            str(resp_b2),
        )

        # 7) 下载
        code, dl = http("GET", f"{base}/recovered/{id_b}/download", raw=True)
        checks.check("下载 200 且字节与原件一致",
                    code == 200 and hashlib.sha256(dl).hexdigest() == expected_sha256(*PKG_B),
                    str(code))

        # 8) 持有者全部退出后再盘点：无新发现；恢复 A 给出“无法打开”原因
        code, resp4 = http("POST", base + "/inventory", {"pids": holder_pids})
        checks.check("进程退出后盘点为空且 pid_errors 有记录",
                    resp4["discovered"] == [] and len(resp4["pid_errors"]) == 2,
                    str(resp4.get("pid_errors")))
        code, resp_a2 = http("POST", f"{base}/recover/{id_a}")
        checks.check("无持有者时恢复 A 失败并说明原因",
                    resp_a2["status"] == "failed" and "无法打开" in (resp_a2.get("failure_reason") or ""),
                    str(resp_a2))

        # 9) 不存在编号
        code, _ = http("POST", f"{base}/recover/9999")
        checks.check("不存在编号返回 404", code == 404)

        # 10) 盘点后无残留句柄指向归档目录
        leaked = count_open_under(api_pid, str(archive))
        checks.check("盘点后服务进程无归档目录残留句柄", leaked == [], str(leaked))

        # 11) A 的新文件内容与原件不同，确认没有被顶替
        if replacement.exists():
            checks.check(
                "复用后的新文件内容不同于原件",
                hashlib.sha256(replacement.read_bytes()).hexdigest()
                != expected_sha256(*PKG_A),
            )

    finally:
        api_proc.terminate()
        try:
            api_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            api_proc.kill()
        holder_proc.terminate()
        try:
            holder_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            holder_proc.kill()

    print(f"\n通过 {checks.passed} 项，失败 {len(checks.failed)} 项")
    if checks.failed:
        for item in checks.failed:
            print(" -", item)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
