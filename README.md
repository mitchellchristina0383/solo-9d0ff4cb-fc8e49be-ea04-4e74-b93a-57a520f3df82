# ImageFlex — 单实例磁盘镜像扩容服务

新虚拟机需要更大的模板盘。本服务自带一份**含样例文件的未挂载 ext4 镜像**，
运维通过 `/docs`（Swagger UI）下载当前镜像，或填入目标容量（MiB）发起扩容：
服务扩大镜像文件及其中的 ext4 文件系统，**核实文件系统容量达标、样例文件
内容未变**后才发布新版本。扩容期间下载照常；扩容失败或服务重启后，最近一次
发布的镜像仍可下载。

## 实现

```
app/
  main.py       FastAPI 端点：GET /image、POST /expand、GET /status、/docs
  manager.py    状态机：发布/留存版本、下载引用计数、两镜像磁盘上限、崩溃恢复
  expander.py   扩容管线（子进程）：复制 → 扩容 → e2fsck → resize2fs → 校验
scripts/
  make_base_image.sh   构建自带的基础镜像（debugfs 注入样例文件 + SHA-256 清单）
tests/test_service.py  9 个端到端测试（进程内 TestClient + 真实 uvicorn 子进程）
Dockerfile / docker-compose.yml
```

### 基础镜像

`make_base_image.sh` 在构建期生成 64 MiB 的裸 ext4 镜像（普通文件，从不挂载）：
`truncate` 定容、`mkfs.ext4` 格式化、`debugfs write` 注入
`/sample/{hello.txt, lorem.txt, random.bin}`，同时生成 `manifest.json`
记录每个样例文件的 SHA-256。镜像与清单一并打入容器（`/app/base`）。

### 扩容管线（expander.py）

对每次扩容，在数据目录里生成**私有工作副本** `image-vN+1.img.part`，
绝不改动已发布镜像：

1. `shutil.copyfile` 复制当前发布镜像；
2. `os.truncate` 把文件扩大到目标 MiB；
3. `e2fsck -fy` 后 `resize2fs`（不带尺寸参数，自动填满文件）；
4. **容量核实**：解析 `dumpe2fs -h` 的 Block count × Block size，须 ≥ 目标；
5. **样例核实**：`debugfs dump` 逐个取出样例文件，SHA-256 须与清单一致；
6. `e2fsck -fn` 终检文件系统干净，`fsync` 后进入发布。

任一步失败即抛 `ExpansionError`，工作副本被删除，已发布镜像不受影响。

### 发布与下载语义

- **原子发布**：`os.replace(.part → image-vN.img)` + 原子重写 `state.json`
  （先写临时文件再 `os.replace`）。
- **下载即快照**：`GET /image` 在开始瞬间取当前版本，`Content-Length` 来自
  当时打开文件的 `fstat`，内容来自该不可变文件；之后的扩容不影响本次响应。
- **两镜像上限**：数据目录最多同时存在两份镜像——已发布镜像 + （扩容工作副本
  或仍在被下载的旧版）。旧版由下载引用计数保护，最后一个下载结束时删除。
  两个槽位都被占用时 `POST /expand` 返回 **409**（带 `Retry-After`），
  空出后重试即可。
- **目标容量**：等于现版 → 返回现版（`status: current`，不产生新镜像）；
  小于现版 → **400** 拒绝；超过 `MAX_TARGET_MIB`（默认 64 GiB）→ 400。
- **并发模型**：单进程单事件循环；关键段（检查并置位、发布）是无 `await`
  的同步代码，对协程原子；阻塞管线经 `asyncio.to_thread` 在线程里执行，
  扩容期间下载照常。因此部署时只能单 worker（compose 即如此）。
- **重启恢复**：启动时读 `state.json` 定位最近发布版；删除目录里所有其他
  文件（崩溃遗留的 `.part`、随进程死去的旧版下载）；无状态则从自带基础镜像
  重新播种为 v1。

## API

| 端点 | 说明 |
| --- | --- |
| `GET /image` | 下载当前发布镜像（附 `X-Image-Version` / `X-Image-Size-MiB`） |
| `POST /expand` | `{"target_mib": N}`；200 已发布/已是目标，400 目标过小或超限，409 两镜像都在用，500 扩容失败（已发布镜像不受影响） |
| `GET /status` | 当前版本、盘上镜像清单、在途下载数、是否扩容中 |
| `GET /docs` | Swagger UI，可直接下载镜像或发起扩容 |

## 运行

```bash
docker compose up --build        # http://localhost:8000/docs
```

数据目录挂在命名卷 `image-data` 上，重启不丢已发布镜像。

本地开发（需 e2fsprogs 与 Python 3.11+）：

```bash
bash scripts/make_base_image.sh ./base 64
pip install -r requirements-dev.txt
DATA_DIR=./data BASE_DIR=./base uvicorn app.main:app --port 8000
```

## 实测结果

`pytest` 全绿（9 项，约 15 s）：

- 初始下载与发布文件逐字节一致（SHA-256），响应头长度/版本正确；
- 扩容 16→32 MiB：发布 v2，`fs_capacity_mib ≥ 32`，3 个样例文件哈希全部
  与清单一致；**下载回来的镜像**再经 `dumpe2fs`/`debugfs` 复核有效；
- 目标等于现版 → 返回现版、不产生新文件；小于现版 → 400；
- 注入扩容失败 → 500，旧版仍可下载、无 `.part` 残留，修复后重试成功；
- 扩容进行中再发起扩容 → 409；扩容中等值请求 → 正常返回现版；
- **下载与扩容交错**（真实 uvicorn 子进程，64 MiB 镜像）：v1 下载到一半时
  扩容发布 v2，再扩 v3 被拒（409，盘上恰两份镜像）；v1 下载完成后旧版自动
  删除，重试 v3 成功；中途下载的响应长度与内容严格等于下载开始时的 v1；
- 进程重启（含遗留 `.part`/旧版垃圾文件）→ 最近发布版仍可下载，目录恢复
  只剩发布镜像与 `state.json`。

另用 curl 对运行中的服务做了冒烟验证：`/docs` 正常；64→128 MiB 扩容耗时
约 0.4 s；重启后 v2 可下载，下载件经 `dumpe2fs` 确认为 clean ext4、
容量 131072×1024 = 128 MiB，样例文件内容完整。

## 余项 / 限制

- 单实例设计：状态在进程内存中，多副本/多 worker 部署需要外置锁与共享存储。
- 裸 ext4 文件（无分区表）；若模板盘需要分区表，管线要改为 `losetup -P` +
  分区级 `resize2fs`（容器需特权）。
- 无认证鉴权，应放在内网/反向代理之后；`MAX_TARGET_MIB` 限制单次扩容上限。
- 下载不支持 Range 断点续传；大镜像下载中断需重新下载。
- 镜像文件非稀疏复制（`copyfile` 会写实零），超大镜像扩容耗时随体积线性增长；
  可换 `cp --sparse=always` 或 reflink 优化。
- 未做指标/告警（Prometheus）与下载限速；`Retry-After` 目前为固定提示值。
- 本仓库环境无 Docker，`Dockerfile`/`docker-compose.yml` 未实际构建，
  已用等价的裸进程（uvicorn + 相同环境变量）验证全部行为。
