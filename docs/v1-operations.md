# v1 部署与验证

## 安装和接入

```bash
python3 -m venv .venv
.venv/bin/pip install '.[server]'
.venv/bin/python -m agent_manage.server --config /absolute/path/server.json
```

复制 `examples/v1-server.json` 后填写当前实例的真实路径和原生 Gateway 地址。
Manager 状态目录必须由服务运行用户所有、权限 0700；不能放在 Agent workspace/public 中。
运行用户需要操作底座文件的权限；Manager 不通过 root 或 Docker socket 绕过目录权限。
每个 Runtime 的 executable/configPath/stateDir/HOME 必须对应同一个环境。使用 NFS/共享磁盘前须单独验证 flock、SQLite WAL、rename/fsync 语义，本实现按本地文件系统设计。
新服务要求原生 workspace、agentDir 和 session.store 使用绝对路径，避免不同 HOME 下的 `~` 指向错误目录；旧 CLI 不增加此限制。安装选项中的 `~` 按注册的 Runtime HOME 展开。

Manager 仅监听 `stateDirectory/manager.sock`。将状态目录和 socket 置于 Server API 与 Manager 的可信 OS 用户边界内。该目录权限为 0700，部署时应和不受信的工具、Agent 及脚本使用的 OS 身份隔离。同一 UID 的进程共享这一信任边界。
Dola 平台入口负责 HTTPS/WSS 和用户鉴权。主机侧 Server API 建立到 Manager 的内部连接，独立构造授权信息，过滤客户端的 Origin、内部授权头与鉴权帧。浏览器只持有平台登录凭据。媒体代理保留 Range 和流式响应。
Gateway 使用独立的 gatewayOrigin 和原生设备身份，拒绝配对/Origin 时明确报 NATIVE_AUTH_REQUIRED；不会自动关闭生产 OpenClaw 的设备验证。
连接后还会核对协议、Gateway 返回的精确版本和 `config.get` 的配置路径；版本或路径不符时停止调度，避免端口指向另一个 Runtime。后端原生身份需要读取该配置快照的权限，原始配置不发送给浏览器。

主机侧 Server API 复用现有平台派发的用户与实例身份，向 `POST /v1/auth/tickets` 发送以下格式的授权：

```json
{"issuer":"dola","subject":"account-id","tenantId":"optional-team-id",
 "audience":"agent-manager:host-001","runtimeIds":["openclaw-primary"],
 "agentIds":["agent_..."],"actions":["threads.list","messages.list"],
 "authzVersion":1,"ttlSeconds":60,"leaseSeconds":300}
```

Chat 使用明确的最小范围，管理员才用 `*`。平台撤销用户权限时，Server API 应停止该用户的转发、关闭相关 WSS 并停止复用其租约。票据和租约使用细节见 [v1 对接合同](v1-api.md#私有传输与授权)。
从 1.0.001 的旧验证配置迁移时，删除 `ticketIssuer`、`listenHost`、`listenPort` 和 `allowedOrigins`，再接入本地 socket。旧 CLI 仍按原平台方式派发。

旧 CLI 不需要这套网络授权，也不需要安装 server extra。

## 预算和可观测性

| 项目 | 默认 |
| --- | --- |
| 同步历史工作线程 | 4，满载返回 BUSY，不无限排队 |
| 管理写命令工作线程 | 2；Runtime 内另有写锁 |
| 媒体工作线程 | 2，与控制/历史独立 |
| 并行后台 Operation | 16 |
| HTTP RPC / 媒体传输 / WSS 连接 | 8 / 4 / 64，包含等待慢客户端发送的时间 |
| 每条 WSS 并行请求 / 订阅 | 8 / 32 |
| WSS 事件缓冲 | 64 条且最多 1 MiB；溢出 resync |
| 单请求 / 单响应 | 1 MiB / 8 MiB |
| 历史页面 | 默认 50、上限 200 条，1 MiB 内容预算 |
| HTTP 媒体流块 | 1 MiB；原 CLI 仍为 5 MiB |
| 补读消息 | 8 MiB 原始消息预算 |
| v1 单 transcript 扫描 | 最多 256 MiB、20,000 个记录，单记录最多 16 MiB；超限明确报错，旧 CLI 不增加此限制 |

`timings.managerElapsedMs` 是 Manager 收到 RPC 后的处理时间，不包含网站→平台→主机网络排队。
历史 result.timings 保留分阶段耗时、读取字节数、缓存命中等；hostCpuMs 现在按执行线程计时，避免常驻服务中混入其他请求的 CPU 时间。
日志只记录方法、耗时和错误类型；浏览器不接收底座 token、完整命令日志或 SQLite 内容。
SQLite 的操作与对象映射目前不自动清理，以免破坏幂等与旧 ID；生产部署需监控状态目录体积，后续加入有明确保留期的归档策略。

## 故障恢复

1. 新服务重启：accepted/running Operation 变成 unknown；未完成 Run 的 observation 变 unknown。通过原 ID 查询与原生事件/历史确认，不能自动再执行。
2. 刷新被 kill：下次管理写入或容器启动先读取 manifest，检查 before/after 指纹并恢复受管理文件；成功记 rolled_back。
3. 发现用户独立修改或旧格式 prepared manifest：RECOVERY_REQUIRED，保留现场/备份，管理员确认后处理。绝不无条件覆盖现有文件。
4. install.sh 失败：模板已经提交，保留它和副作用信息，使用 agent-install/agents.install 明确重试。文件恢复不代表外部服务被恢复。
5. WSS 掉线或 slow consumer：重新兑换票据/订阅，读取权威历史。没有事件重放保证时不可凭 sequence 拼接一个“保证完整”的历史。

应备份：原生 config、agents、workspace/记忆、credentials，Manager 数据库（含 WAL 的一致快照）和 devices，refresh backups，以及模板发行物。
Provider 凭据和租约数据库作为私密数据处理；公开分享导出不能直接复制这些目录。

## 测试与发布门槛

所有旧测试保持，新增文件验证环境隔离、进程锁、并发更新、写前日志、媒体读取预算、HTTP/WSS、鉴权变更和幂等。
标准库测试不强依赖 aiohttp；安装 server extra 后必须同时跑新服务测试。

```bash
python -m unittest discover -s tests
AGENT_MANAGE_TEST_OPENCLAW_BIN=/absolute/path/openclaw-2026.7.1-2/openclaw.mjs \
  python -m unittest discover -s tests
```

macOS 若临时目录经 `/var`→`/private/var` 别名导致旧 Fly 路径断言不同，测试入口先设置
`tempfile.tempdir = os.path.realpath(tempfile.gettempdir())`；不要删改旧测试来掩盖它。

`test_v1_native_gateway.py` 会启动临时 July Gateway 和本地模型服务，验证真实握手、发送、流、历史、取消；不消耗付费模型，也不读写用户的 OpenClaw 环境。
这证明指定版本的协议闭环，不等于生产 Docker、Dola 授权服务和网站已联调。生产切换前仍需真实平台授权与当前 Chat 集成验收。
OpenClaw 更新必须重跑对应包的集成测试，不凭版本字符串自动宣称完整兼容。
