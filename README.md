# 诊断包 fd 恢复服务

清理构建产物时误删了归档目录中的诊断包，但下载进程仍持有原件的打开文件描述符。
本服务在 Linux 上通过 `/proc/<pid>/fd` 盘点这些“已删除但仍打开”的文件，并分块复制
到恢复目录，供值班员重新下载。

## 工作原理与关键约束

1. **盘点只做短暂读取，不留句柄**：对指定 PID 逐个 `readlink /proc/<pid>/fd/N`，
   筛选出链接带 ` (deleted)` 后缀、且原路径位于指定归档目录内的普通文件；需要
   `fstat` 时以 `O_RDONLY` 短暂打开，取到 (设备号, inode, 大小) 后立即 `close`。
2. **同一原件一个恢复编号**：以 `(st_dev, st_ino)` 作为原件身份去重，跨进程、跨
   fd 的同一 inode 聚合为同一条目，只分配一个编号；重复盘点返回原编号（响应中的
   `reused_entries`）。
3. **描述符/路径复用不能顶替原件**：恢复前重新打开持有者 fd 并核对
   `(dev, inode, size)`。fd 被 `dup2` 复用到同路径的新文件后，inode 改变，校验
   失败，拒绝接手并在 `failure_reason` 中给出新旧 inode。
4. **可从任一持有者接手**：条目记录全部 `(pid, fd)` 持有者，恢复时逐个尝试，
   任一可用即可。
5. **分块复制，读者退出不影响复制**：服务以自己的独立只读 fd 读取，默认 8 MiB
   分块；复制期间下载进程退出，已打开的 fd 仍指向原 inode，复制继续直到完成。
6. **写完才开放下载**：先写恢复目录下的隐藏临时文件，校验字节数与 sha256、
   `fsync` 后 `os.replace` 原子改名；下载接口对未 `recovered` 的条目返回 409。
7. **失败可解释、可重试**：原件无法打开、身份不符、I/O 错误均写入
   `failure_reason`，临时文件自动清理；重新盘点到持有者后条目回到可恢复状态。
8. **幂等**：已救回的编号再次调用恢复，直接返回同一份包（同一文件名、同一
   sha256），登记表落盘，服务重启后仍然成立。

## 目录结构

```
app/
  config.py    环境变量配置（ARCHIVE_DIR / RECOVERY_DIR / PROC_ROOT 等）
  procfs.py    /proc fd 盘点、原件身份校验、fd 重新打开
  registry.py  恢复编号登记表（线程安全 + JSON 落盘 + 状态机）
  recovery.py  分块复制、临时文件原子改名、失败清理
  schemas.py   请求/响应模型（Swagger 文档来源）
  main.py      FastAPI 路由
demo/
  holder.py    本地演示“下载进程”：写样包 → 打开 → 删目录项 → 继续持有
  entrypoint.sh 容器入口（api / demo 两种角色）
tests/
  e2e_test.py  端到端测试（仅标准库，自动起服务与演示进程）
Dockerfile, docker-compose.yml, requirements.txt
```

## 用 Docker Compose 启动（推荐）

```bash
docker compose up --build
# api:  http://localhost:8000/docs   （Swagger UI）
# demo: 启动两个持有者进程，生成两个样包后删除其目录项，pid 写入共享卷
```

两个服务都配置 `pid: host` 并挂载同一数据卷：容器内 `/proc` 即宿主 procfs，
因此 api 可以检查 demo（以及真实下载进程）的 fd。demo 就绪后可在 Swagger 中：

1. `POST /inventory`，请求体 `{"pids": [<demo-pid-1>, <demo-pid-2>]}`
   （pid 见卷内 `recovery/demo-pids.json`，也可在宿主 `pgrep -f holder.py`）；
2. 对返回的 `entry_id` 调用 `POST /recover/{entry_id}`；
3. 状态为 `recovered` 后访问 `GET /recovered/{entry_id}/download` 下载。

## 本地直接运行（无 Docker 环境时）

```bash
python3 -m venv --without-pip .venv
# 若 venv 内没有 pip：curl -sSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
.venv/bin/pip install -r requirements.txt

# 终端 A：演示进程（两个持有者，两个样包，删除目录项后继续持有）
.venv/bin/python -I demo/holder.py \
  --archive /tmp/diag/archive --pidfile /tmp/diag/pids.json \
  --package diag-bundle-a.tar.gz=10485760 --package diag-bundle-b.zip=5242880

# 终端 B：API
ARCHIVE_DIR=/tmp/diag/archive RECOVERY_DIR=/tmp/diag/recovery \
  .venv/bin/python -m uvicorn app.main:app --app-dir . --port 8000

# 终端 C：端到端测试（自动起服务与演示进程，结束自动清理）
PYTHON_BIN=.venv/bin/python .venv/bin/python -I tests/e2e_test.py
```

### 演示描述符复用

`demo/holder.py` 的持有者收到 `SIGUSR1` 时，只对存在标记文件
`<archive>/.<包名>.reuse` 的包执行“同路径新建文件 + `dup2` 覆盖原 fd”，
用于验证服务拒绝顶替；`SIGTERM` 让持有者退出，用于验证复制中读者退出。

## API 一览（Swagger 中有完整 schema）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/inventory` | 盘点指定 PID 中位于归档目录的已删除打开文件，返回恢复编号 |
| POST | `/recover/{entry_id}` | 按编号恢复；已恢复则幂等返回同一份；失败返回原因 |
| GET | `/entries` | 全部编号与状态（pending/recovering/recovered/failed） |
| GET | `/recovered/{entry_id}/download` | 下载已救回的包，未写完返回 409 |
| GET | `/info` | 服务端目录与分块配置 |

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ARCHIVE_DIR` | `/data/archive` | 归档目录（只盘点原在该目录下的文件） |
| `RECOVERY_DIR` | `/data/recovery` | 恢复目录与登记表位置 |
| `REGISTRY_FILE` | `$RECOVERY_DIR/registry.json` | 登记表路径 |
| `PROC_ROOT` | `/proc` | procfs 根（容器共享宿主 pid ns 时即 `/proc`） |
| `COPY_CHUNK_SIZE` | `8388608` | 分块复制大小（字节） |
| `COPY_DELAY_SECONDS` | `0` | 每分块额外延迟，仅测试用 |

## 设计说明

- 归档目录约定只存放已写完、内容固定的包，因此恢复时以盘点时刻的
  `st_size` 为期望大小；复制后字节数不符即判失败，不会交出截断的包。
- 恢复端点为同步执行：复制期间条目状态为 `recovering`，并发再次请求返回 409；
  服务重启后停在 `recovering` 的条目自动回到 `pending`，临时文件下次恢复前清理。
- 权限：检查其他用户的进程需要同 uid 或 `CAP_SYS_PTRACE`（容器场景用
  `pid: host` + 适当权限/同用户运行；本仓库镜像默认 root，本地演示不受影响）。
- 登记表 JSON 采用临时文件 + 原子 rename 写入，损坏时以空表起步而不阻塞服务
  （已恢复的文件仍在恢复目录中，可人工关联）。

## 实测结果

在本机（Python 3.11，无 Docker，venv 安装 fastapi 0.115.6 / uvicorn 0.34.0）
执行 `tests/e2e_test.py`，**24 项检查全部通过**，覆盖：

- 两持有者 × 两个已删除样包：同一原件只给一个编号、持有者聚合为 2；重复盘点
  编号复用、不产生新编号；
- 描述符复用（同路径新文件 `dup2` 覆盖 fd）：盘点不再认其为原件，恢复时身份
  校验失败且原因含新旧 inode，未恢复完成下载返回 409，新文件内容不会顶替原件；
- 复制过程中 `SIGTERM` 杀掉全部持有者：服务凭独立 fd 复制成功，临时文件原子
  改名，sha256 与原件一致；
- 已恢复编号再次恢复返回同一份包（文件名与 sha256 不变）；下载字节与原件一致；
- 持有者全部退出后盘点为空且 `pid_errors` 给出原因；无可用持有者时恢复返回
  failed 并说明“原件已无法打开”；未知编号返回 404；
- 盘点结束后检查服务进程 `/proc/<pid>/fd`，无任何指向归档目录的残留句柄。

另做了补充验证：

- 3 个持有者杀掉其中 2 个后再恢复：从唯一存活持有者接手成功（任一持有者可接手）；
- 归档目录中**未删除**的打开文件不会被盘点；
- 服务重启后登记表从磁盘加载，条目状态与恢复文件保留，幂等仍成立；
- mock `os.read` 抛 `EIO`：恢复返回中文失败原因，恢复目录无临时文件残留；
- `/docs`（Swagger）与 `/openapi.json` 可正常访问；`docker-compose.yml` 与
  `entrypoint.sh` 语法校验通过。

## 遗留事项 / 边界

- 当前环境无 Docker 守护进程，compose 文件只做了静态校验，未实际
  `docker compose up`；在有 Docker 的机器上可直接按上节步骤验证。
- 恢复为同步接口，超大文件复制期间 HTTP 请求会持续占用一个工作线程；如需
  异步化可改为后台任务 + 轮询 `/entries`（状态机已支持）。
- 未做鉴权，默认只在内网值班环境暴露；如需跨网络使用应前置网关认证。
- 归档目录内文件若在删除后仍被写入（内容不固定），大小校验会拦截并判失败，
  这符合“归档目录只存放内容固定的包”的约定，但不会尝试自动重试。
