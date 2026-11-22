# v1 部署与验证

## 安装和接入

```bash
python3 -m venv .venv
.venv/bin/pip install '.[server]'
.venv/bin/python -m agent_manage.server --config /absolute/path/server.json
```

复制 `examples/v1-server.json` 后填写当前实例的真实绝对路径和端口。
Manager 状态目录必须由服务运行用户所有、权限 0700；不能放在 Agent workspace/public 中。
运行用户需要操作底座文件的权限；Manager 不通过 root 或 Docker socket 绕过目录权限。
每个 Runtime 的 executable/configPath/stateDir/HOME 必须对应同一个环境。使用 NFS/共享磁盘前须单独验证 flock、SQLite WAL、rename/fsync 语义，本实现按本地文件系统设计。
新服务要求原生 workspace、agentDir 和 session.store 使用绝对路径，避免不同 HOME 下的 `~` 指向错误目录；旧 CLI 不增加此限制。安装选项中的 `~` 按注册的 Runtime HOME 展开。

默认回环 HTTP，仅在 TLS 反向代理后提供远程 HTTPS/WSS。代理保留 Upgrade/Connection；媒体关闭整响应缓存，不把租约记录到访问日志。
allowedOrigins 是网站精确 Origin 白名单，不是身份校验的替代品。
Gateway 使用独立的 gatewayOrigin 和原生设备身份，拒绝配对/Origin 时明确报 NATIVE_AUTH_REQUIRED；不会自动关闭生产 OpenClaw 的设备验证。
连接后还会核对协议、Gateway 返回的精确版本和 `config.get` 的配置路径；版本或路径不符时停止调度，避免端口指向另一个 Runtime。后端原生身份需要读取该配置快照的权限，原始配置不发送给浏览器。

需要 Dola 或独立账号服务时，在服务配置增加：

```json
{"ticketIssuer":{
  "issuer":"dola",
  "url":"https://your-trusted-service.example/manager-ticket/redeem",
  "credentialFile":"/private/path/issuer-service-credential"
}}
```

这是接口示例，不是 Dola 已有的生产 URL。credentialFile 为仅服务用户可读的 0600 文件。
Manager 向它 POST `{ticket,audience}`，使用服务凭据 Bearer 认证；不跟随重定向、不自动重试。
签发服务必须原子消费票据，成功直接返回以下 grant：

```json
{"issuer":"dola","subject":"account-id","tenantId":"optional-team-id",
 "audience":"agent-manager:host-001","runtimeIds":["openclaw-primary"],
 "agentIds":["*"],"actions":["*"],"authzVersion":1,"expiresAt":1794480300000}
```

expiresAt 必须是未来 300 秒内的 Unix 毫秒；Chat 使用最小 actions/agentIds，管理员才用 `*`。
平台即时撤销可在可信本机集成调用 `LeaseAuthority.revoke(leaseId)`；本次未定义公网无保护的撤销入口。
缺少撤销推送时依靠短租约到期失效；需要秒级账户封禁时，上层必须补齐可信撤销通知通道。

独立本机部署可以通过受 OS 保护的 Python 集成签发票据：

```python
from agent_manage.core.store import Store
from agent_manage.core.auth import LeaseAuthority
authority = LeaseAuthority(Store('/srv/agent-manager/state'), 'agent-manager:host-001')
ticket = authority.issue_ticket(
    issuer='local-os', subject='operator', runtime_ids=['openclaw-primary'],
    actions=['system.info', 'capabilities.get', 'agents.list', 'threads.list',
             'segments.list', 'messages.list', 'messages.get', 'resources.get'])
# 通过受信任的应用内通道交给本次客户端；不要写进日志或 URL。
```

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
