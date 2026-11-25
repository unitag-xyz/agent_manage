# Chat 多段历史对接（OpenClaw 2026.7.x）

这是 AgentManager 新增的主机侧只读能力，不是 OpenClaw 7 系列原生 Gateway 方法。
Server API 已支持转发任意 AgentManager 命令时，可以直接通过现有命令通道调用。
若前端采用点分方法名，代理按下表映射即可。用户、实例、Agent 的访问权限由现有 API 层先校验；主机侧再次校验物理段属于指定逻辑线程。

| 建议 WSS 方法 | AgentManager 命令 |
| --- | --- |
| `sessions.segments.history` | `sessions-segments-history` |
| `sessions.segments.message.get` | `sessions-segments-message-get` |
| `sessions.segments.media.get` | `sessions-segments-media-get` |

### 平台参数到 CLI 参数的映射

点分 WSS 协议的 `params` 使用驼峰字段；AgentManager 命令通道若按连字符键转 CLI，则 `parameters` 使用下表中间列。
`agent_manage` 只接收 CLI 参数，不解析平台的 `commandCode` 或 `parameters`；平台必须显式完成映射，不能只把 JSON 传给主机。

| WSS 字段 | 命令通道参数键 | CLI 参数 |
| --- | --- | --- |
| `sessionKey` | `session-k`（当前平台兼容）；`session-key`（标准） | `--session-k` 或 `--session-key` |
| `sessionId` | `session-id` | `--session-id` |
| `messageId` | `message-id` | `--message-id` |
| `edge` | `edge` | `--edge` |
| `limit` | `limit` | `--limit` |
| `view` | `view` | `--view compact`（默认）或 `--view full` |
| `cursor` | `cursor` | `--cursor` |
| `ref` | `ref` | `--ref` |

三个命令均要求逻辑线程参数（`session-k` 或 `session-key`）、`session-id`；消息补读还要求 `message-id`。
附件首次读取要求 `ref`，分块续读可仅传 `cursor`。历史的 `cursor` 与 `edge` 互斥。
2026-10-12 前端任务反馈：同值使用 `session-key` 时主机提示缺少该参数，改为 `session-k` 后线上旧段读取成功；短值也有同样差异。
这些结果说明问题与参数名有关，尚未证明是白名单或敏感键处理。Server API 源码不在本仓库。
三个只读命令现显式声明 `--session-k` 别名，不依赖 argparse 自动缩写；标准 `--session-key` 保留。实例、Agent 和会话归属检查不变。
当前平台转发建议使用 `session-k`；响应和点分 WSS 字段仍是 `sessionKey`。
例如平台请求：

```json
{
  "commandCode": "agent.sessions-segments-history",
  "parameters": {
    "session-k": "agent:example:dashboard:thread-key",
    "session-id": "00000000-0000-4000-8000-000000000001",
    "edge": "end",
    "limit": "30"
  }
}
```

应构造如下 argv（每个值保持为一个独立参数，不通过 shell 字符串拼接）：

```json
["sessions-segments-history", "--session-k", "agent:example:dashboard:thread-key", "--session-id", "00000000-0000-4000-8000-000000000001", "--edge", "end", "--limit", "30"]
```

## 分段索引

沿用 OpenClaw `sessions.usage`，参数 `key: <sessionKey>, groupBy: family, limit: 1`。
`currentSessionId` 是当前段，`includedSessionIds` 是已记录的段 ID；去重并把当前段移到最后。
主机读取器依据同一会话条目的 `sessionId` 和 `usageFamilySessionIds` 校验归属，不扫描其他线程来拼接来源。
索引未记录的旧 ID 不允许仅凭 UUID 读取。索引记录存在但文件已清理时返回 `SEGMENT_NOT_FOUND`。

## 历史分页

```bash
agent-manage sessions-segments-history \
  --session-key 'agent:example:dashboard:thread-key' \
  --session-id '00000000-0000-4000-8000-000000000001' \
  --edge end --limit 30
```

参数映射为 `sessionKey`, `sessionId`, `edge`, `limit`, `cursor`, `view`。
`sessionKey` 必须包含 `agent:<agentId>:`；`sessionId` 必填，不替换成当前段。
`edge` 默认为 `end`，也支持 `start`；`limit` 默认 50，范围 1–200。历史页消息内容预算为 1 MiB（最终响应还包含 JSON 包装、游标和计时字段）；到达预算时可能少于指定条数。向后读取和单条预览按条数限制；从末尾或向前读取默认按下文的用户消息边界停止。
传 `cursor` 时不要同时传 `edge`。向前、向后分页都用同一个命令：

```bash
agent-manage sessions-segments-history \
  --session-key 'agent:example:dashboard:thread-key' \
  --session-id '00000000-0000-4000-8000-000000000001' \
  --cursor '<olderCursor 或 newerCursor>' --limit 30
```

CLI 外层沿用 `{result, error, typeCode, message, serverTimeStamp, restartRequired}`。
API 代理可将 `result` 放入既有 WSS `payload`。以下为 `result`：

```json
{
  "sessionKey": "agent:example:dashboard:thread-key",
  "sessionId": "00000000-0000-4000-8000-000000000001",
  "view": "compact",
  "segment": {
    "startedAt": 1791766800000,
    "endedAt": 1791766801000,
    "isCurrent": false,
    "readable": true,
    "preview": ["最后一条用户消息", "最后一条助手消息"]
  },
  "messages": [{
    "id": "stable-entry-id",
    "role": "assistant",
    "timestamp": 1791766801000,
    "content": [{"type": "text", "text": "完整正文"}],
    "__openclaw": {"id": "stable-entry-id"}
  }],
  "olderCursor": "opaque-token",
  "newerCursor": null
}
```

每页按旧到新排列。段时间取本次快照中已存储记录的最早/最晚时间（毫秒），无时间则 `null`；当前段的 `endedAt` 是最后已存储记录时间，不代表对话已结束。
预览最多 180 个 Unicode 字符，正文不受这个限制，也不采用 OpenClaw `sessions.usage.logs` 或 `chat.history` 的文本截断规则。
保留已保存的思考和普通正文；默认精简工具记录，规则见下节。按 July transcript 树规则选择当前有效分支，不拼入已放弃分支。
无 ID 的旧记录以物理字节位置生成稳定 ID。不会重新生成未保存的思考或恢复已删除内容。

历史分页游标仅在同一实例、线程和物理段内有效，不按时间过期，升级前签发的分页游标也按此规则读取。
每次读页时返回的长消息和附件引用仍只在一小时内有效；分块续读继承原截止时间，不会顺延。
首次读取固定文件身份、已完成记录的字节边界和内容哈希；后续追加不进入这个快照。
文件正常重命名为 reset 归档仍可继续读；原文件被改写、替换、清理或引用失效返回 `CURSOR_EXPIRED`。
Gateway 认证配置变更也会使旧引用失效。页中保留内部消息，前端可自行隐藏；隐藏后即使没有显示行，也要继续处理返回的游标。
两侧游标均只在本段内推进。跨段顺序由前端的分段索引决定。

### 默认精简工具操作

新建页面默认 `view: compact`，原命令无需加参数。主机按固定规则处理，不调用模型做摘要：

- 用户消息、助手正文和已保存思考保留；图片、音频及媒体引用保留。
- `toolCall` / `tool_use` / `functionCall` 保留类型、ID、工具名，`arguments` 保留一个截短的预览字段（`command` / `cmd` / `path` / `file_path` / `filePath` / `url` / `query` / `pattern`），最多 180 字并压成一行；原本使用 `input` 的调用也返回同一预览。完整代码、额外参数不传。新字段 `summary` 同样提供这段预览，兼容前端从旧参数字段生成摘要的逻辑。没有这些字段时只显示工具名，不推测操作含义。
- `toolResult` / `tool` / `function` 保留工具名、调用关联、错误标记和附件；正文代码、日志、完整输出与任意 `details` 不传。`toolSummary` 返回 `{name, status}`，明确 `isError: true/false` 才返回 `error/success`，未记录时为 `unknown`。
- 被精简的消息带 `__openclaw.compacted: true, reason: "tool_details_omitted"`。省略的工具详情不带全文补读引用，前端无需补取；返回的预览参数不能用于重新执行命令。
- 所有历史/消息展示响应（包括 `view: full`）移除 `thinkingSignature`，保留可读 `thinking`。这是返回时过滤，本地 transcript 不修改，OpenClaw 继续对话所需的加密推理数据仍在原始记录里。

例如操作内容块为：

```json
{"type":"toolCall","id":"call-1","name":"exec","arguments":{"command":"grep -n 'auth' /app/extensions/openai/speech-providers.ts"},"summary":"grep -n 'auth' /app/extensions/openai/speech-providers.ts"}
```

结果消息为：

```json
{"id":"result-1","role":"toolResult","toolCallId":"call-1","toolName":"exec","isError":false,"content":[],"toolSummary":{"name":"exec","status":"success"},"__openclaw":{"id":"result-1","compacted":true,"reason":"tool_details_omitted"}}
```

前端用 `summary` 展示操作预览，用 `toolSummary` 展示结果，不再从完整参数和代码输出生成预览。
已有前端从 `arguments.command/path` 等字段取预览时也可直接显示，无需先适配 `summary`。
原先的完整模式仍可显式使用 `--view full`；这不是默认浏览流程，也不要求前端调用它。普通超大正文仍遵循原有输出预算和补读规则。
游标绑定首次页面的 `view`，后续可省略参数并继承；切换模式需重新打开该段，不能用原游标改模式。
旧版未带 `view` 的页游标继续沿用原完整模式；重新打开该段即用精简模式。

## 大消息补读

页面按输出字节预算控制大小，可能少于 `limit` 条。超大消息不静默截正文，而是返回：

```json
{
  "id": "large-entry-id",
  "role": "assistant",
  "timestamp": 1791766801000,
  "content": [],
  "__openclaw": {"id": "large-entry-id", "truncated": true, "reason": "full_message_reference"},
  "fullMessageRef": {
    "method": "sessions.segments.message.get",
    "params": {"sessionKey": "agent:example:dashboard:thread-key", "sessionId": "00000000-0000-4000-8000-000000000001", "messageId": "large-entry-id", "ref": "opaque-reference"}
  }
}
```

```bash
agent-manage sessions-segments-message-get \
  --session-key '<sessionKey>' --session-id '<sessionId>' \
  --message-id '<messageId>' --ref '<fullMessageRef.params.ref>'
```

`ref` 保持与历史页面相同的快照。也可以不传 `ref`，按指定物理段重新建立快照。
页面签发的 `ref` 也保留该页的 `view`；精简页面补齐超长正文/思考时，仍省略工具参数和输出，不会恢复这些工具原文。分块游标继续继承这个模式。
正常大小返回 `{sessionKey, sessionId, messageId, message}`。
特别大的消息返回如下分块结果：

```json
{
  "sessionKey": "agent:example:dashboard:thread-key",
  "sessionId": "00000000-0000-4000-8000-000000000001",
  "messageId": "large-entry-id",
  "contentType": "application/json",
  "encoding": "base64",
  "data": "base64-encoded-bytes",
  "offset": 0,
  "totalBytes": 900000,
  "sha256": "hash-of-the-complete-json-bytes",
  "nextCursor": "opaque-chunk-token"
}
```

继续调用同一补读命令，传 `--cursor <nextCursor>`；此时可以省略 `--ref`。
CLI 首次补读仍按快照选取有效分支。对于大消息，如果签名使用了私密的游标密钥或字面量 Gateway Token
（至少 16 个字符，不使用环境变量占位符），新分块游标还绑定原始记录的位置和 SHA-256。
续读只解析该条记录，仍核验逻辑会话归属、物理文件身份及整个快照前缀的 SHA-256。
不必单独配置游标密钥；使用 Gateway Token 时，能读取该 Token 的管理方也在游标签名的信任范围内。没有合适的签名材料或游标无记录定位信息时仍按原逻辑扫描。
新消息分块游标同时绑定完整输出的 SHA-256。即使只替换了附件、会话文件未变，只要输出发生变化，续读就返回 `CURSOR_EXPIRED`，需从首块重读。相同展示格式的旧游标仍可用，但只能从首次换回新游标起校验输出的一致性。
逐块 Base64 解码为字节，按 `offset` 拼接，直到 `nextCursor: null`。
检查长度和 SHA-256，再整体 UTF-8 解码并 JSON.parse。不要逐块直接解码文本，UTF-8 字符可能跨块。
本次展示格式变化前签发的消息分块游标返回 `CURSOR_EXPIRED`，需要从首块重新读取，避免新旧过滤格式混合拼接；历史页游标和附件字节分块不受此格式标记影响。

## 耗时定位

三个读取命令每次自动记录计时，不需要额外参数。成功响应位于 `result.timings`；失败时 `result` 为 `null`，诊断位于 AgentManager 输出的外层 `timings`（即平台包装中的 `output.timings`）。API 应透传该字段。

主要字段：

| 字段 | 测量范围 |
| --- | --- |
| `version` | 诊断结构版本，目前为 1 |
| `startedAtUnixMs` / `measuredAtUnixMs` | 主机上本次计时开始/最后采样的 Unix 毫秒，可用于和后台日志关联；跨机器比较需时钟同步 |
| `hostElapsedMs` | CLI 入口到最后采样的墙钟耗时，包含初始化、读取和首次响应序列化 |
| `hostCpuMs` | 同一测量窗口内进程消耗的 CPU 时间；与墙钟时间不同 |
| `hostReadMs` | 进入读取处理器到读取完成/失败的耗时，兼容此前字段 |
| `cliModuleImportMs` | 本进程加载 CLI 模块及依赖的耗时；在 CLI 入口之前，独立于 `hostElapsedMs`，不是完整 Python 启动时间 |
| `phasesMs` | 各阶段累计墙钟耗时，详见下表 |
| `phaseCalls` | 各阶段调用次数，键与 `phasesMs` 相同 |
| `counts` | 记录数和字节数，仅记录数量，不包含路径、命令正文或凭据 |

`phasesMs` 可能包含：

| 阶段 | 含义 |
| --- | --- |
| `cliSetupMs` | CLI 参数解析和管理器初始化 |
| `environmentAndVersionMs` | 执行环境、程序定位和本地版本信息检查 |
| `configReadMs` | 读取实例配置 |
| `readerInitMs` | 读取器、路径和引用签名上下文初始化 |
| `indexReadMs` | 读取会话索引与段归属校验，通常开始/结束各一次 |
| `referenceValidationMs` | 游标/引用签名、作用域和期限校验 |
| `fileLocateAndOpenMs` | 定位原始文件/reset 归档并打开 |
| `transcriptScanMs` | 扫描 JSONL 快照、解析记录、哈希和选取有效消息 |
| `branchSelectionMs` | 有效分支选择，是 `transcriptScanMs` 的子阶段 |
| `messageReadMs` | 按字节位置读取所选消息 |
| `messageBuildMs` | 构造消息、处理媒体引用、过滤签名和精简工具；包含相关子阶段 |
| `mediaReadAndHashMs` | 附件读取、哈希或缓存检查 |
| `toolCompactMs` | 精简工具记录 |
| `jsonEncodeMs` | 读取器内部的 JSON 编码，包括引用和消息大小计算 |
| `chunkEncodeMs` | 分块编码及续读引用生成 |
| `snapshotVerificationMs` | 返回前再次校验文件快照和会话归属，包含末次 `indexReadMs` |
| `responseSerializationMs` | 带诊断占位符的业务响应序列化及 UTF-8 大小计算；不含最终的小型诊断对象和 stdout 写出 |

阶段存在嵌套，**不能把所有阶段相加作为总耗时**。总耗时看 `hostElapsedMs`；未执行的阶段不出现，失败也保留已执行阶段。

`counts` 按实际操作返回：`transcriptBytes`、`recordsScanned`、`messagesSelected`、`messagesReturned`、`messageLocationHits`、`snapshotVerifyBytes`、`mediaBytesRead`、`mediaCacheHits`、`chunkBytes`、`responseBytesBeforeTimingFinalization`。最后一项是诊断位置暂为 `null` 时的 UTF-8 字节数；8 MiB 输出限制仍以填入最终诊断后的实际字节数为准。`messageLocationHits` 表示通过已签名的记录位置跳过全段 JSON 解析，不代表跳过快照校验。`messagesReturned` 是内联返回条数，分块消息的大小看 `chunkBytes`。

以下仅为结构示例，数值不是线上实测：

```json
{
  "timings": {
    "version": 1,
    "startedAtUnixMs": 1791810000000,
    "measuredAtUnixMs": 1791810000014,
    "hostElapsedMs": 14.2,
    "hostCpuMs": 11.8,
    "hostReadMs": 10.1,
    "cliModuleImportMs": 47.3,
    "phasesMs": {"cliSetupMs": 3.1, "indexReadMs": 0.4, "transcriptScanMs": 2.8, "responseSerializationMs": 0.5},
    "phaseCalls": {"cliSetupMs": 1, "indexReadMs": 2, "transcriptScanMs": 1, "responseSerializationMs": 1},
    "counts": {"transcriptBytes": 12000, "recordsScanned": 31, "messagesReturned": 30}
  }
}
```

主机测不到 Server API/任务编排排队、转发和浏览器网络耗时；这些需要 API 层记录。Python 在 CLI 模块计时前的启动、最终计时元数据序列化、stdout 写出/传输也不包含在 `hostElapsedMs` 中，无法在同一份响应内准确返回该响应写完后的耗时。
若浏览器请求约 1–3 秒，而主机 `hostElapsedMs` 与模块加载耗时只有几十毫秒，下一步检查 API、任务编排转发和网络链路。

## 图片、音频等附件

图片的内嵌 Base64 改为内容块上的 `mediaRef`，并在消息 `mediaRefs` 数组列出引用。
本地 `path` / `url` / `MEDIA:` 引用也会给出 `mediaRefs`，正文与原有路径及 `media` 字段仍保留。
引用结构为 `{method: "sessions.segments.media.get", params: {sessionKey, sessionId, ref}}`。

可读取附件的引用外层另带 `mediaId: "sha256:<附件原始字节哈希>"`。
内联 Base64 按解码后的字节计算；本地文件仅在既有 workspace/media 权限检查通过后计算，同文件在本次读取内复用哈希。
同一消息的 `mediaRefs` 按 `mediaId` 去重；内容块上的嵌套 `mediaRef` 仍保留，并携带相同 ID。
前端可按实例、逻辑线程、物理段和 `mediaId` 合并下载请求；下载后校验返回的 `sha256` 与 ID 匹配。
只有字节相同的附件会合并，字节不同的图片、缩略图和原图保留。
缺失、越界、符号链接或无法计算身份的附件引用没有 `mediaId`，不会据此合并或导致整条历史消息失败；真正读取时仍返回原有明确错误。
文件在历史读取后发生变化时，带身份的引用返回 `CURSOR_EXPIRED`，前端重新打开该段。
旧主机没有这个字段，前端应保留兼容；更新主机 AgentManager 后才有下载前身份和去重。

```bash
agent-manage sessions-segments-media-get \
  --session-key '<sessionKey>' --session-id '<sessionId>' --ref '<mediaRef.params.ref>'
```

返回格式与消息分块相同，`contentType` 为附件 MIME，`data` 为附件字节的 Base64。

附件每块最多 5 MiB 原始字节（5,242,880 字节），不超过该大小的附件一次返回；更大的附件通过 `nextCursor` 续读。Base64 后单块约 6.67 MiB，Server API 和代理需允许该响应大小及 JSON 包装开销。消息正文补读仍为每块 96 KiB。已有附件游标按字节偏移续读，无需改变前端拼接协议。

AgentManager CLI 的 stdout 上限为 8 MiB，按完整 UTF-8 JSON（含 Base64、游标和包装字段）加末尾一个 LF 字节计算；恰好达到上限可以返回，多一个字节则返回 `RESPONSE_OUTPUT_TOO_LARGE`。stderr 的原始和实际输出字节数也包含 LF。调用方的 stdout 和 HTTP 响应限制需容纳完整响应。
传 `--cursor <nextCursor>` 继续取后续字节；所有块完成后校验 `sha256`，前端可构造 Blob/Object URL。
这样附件仍经过现有认证命令通道，不需要把主机文件目录公开为 HTTP 静态目录。
引用必须来自本段已存储消息，文件仅允许位于该 Agent workspace 或当前实例 `media` 目录；拒绝符号链接和目录越界。
原消息中的远程 HTTPS URL 保留原样，不由主机下载或绕过远端认证；链接过期或文件已清理无法恢复。
如代理另提供 HTTP 下载入口，可使用同一引用做鉴权读取，不改变此协议。

## 错误与只读保证

| `error.code` | 含义 |
| --- | --- |
| `FORBIDDEN` | 段不属于该线程、Agent 不属于实例、附件目录越界或符号链接 |
| `SEGMENT_NOT_FOUND` | 已知段的 transcript/归档不存在 |
| `CURSOR_EXPIRED` | 游标或引用失效、跨实例/线程/段、快照被替换或改写 |
| `MESSAGE_NOT_FOUND` | 指定消息不在该段有效分支/快照 |
| `MEDIA_NOT_FOUND` | 已引用附件不存在 |
| `SEGMENT_READ_FAILED` | 无法读取、索引或 transcript 格式损坏 |
| `VALIDATION_ERROR` / `INVALID_ARGUMENT` | 参数或命令格式错误 |

真正空段成功返回 `messages: []` 和两个 `null` 游标。失败不伪装空段，不退回当前段。
读取不调用模型，不修改 config、transcript、当前会话 ID、模型、标题、`updatedAt` 或排序。
Python 直接读取 July 的 JSON 会话索引和 JSONL transcript/reset 归档，不启动 Node、不加载 OpenClaw SDK，不使用截断的用量日志拼装消息。
线程 key 使用索引中的原始完整 key 做精确匹配，段必须在该条目的当前/历史 ID 集合中；不做跨 key 的大小写猜测。
保留轻量的归属、路径、文件身份和快照校验；不调用索引维护或更新函数。只扫描选中段，以字节位置读取当页消息，分页期间追加及 reset 不改变已签发快照。
每次请求仍需扫描选中 transcript 的快照边界；超大记录很多时，文件解析仍有成本。当前只针对 OpenClaw 2026.7.x 的 JSON 存储格式。

## 前端衔接

1. 进入线程：当前段继续走原有 `chat.history`，取 family 索引。
2. 悬停：目标段 `edge: end, limit: 1`，缓存时间与预览。
3. 进入旧段：`edge: end, limit: 30`。
4. 上滚：用 `olderCursor` 前插；为空则读取上一段末尾，保持原可见行位置。
5. 下滚：用 `newerCursor` 追加；为空则读取下一段开头。当前段也支持 `edge: start`。
6. 回到当前段最新位置时，用原生 `chat.history` 重新同步尾部，按稳定 ID 去重并恢复实时流；旧快照不会自动纳入后续追加的消息。
7. 切线程后丢弃迟到响应；引用失效时重新打开原目标段。

浏览旧段仅切换显示内容。发送消息仍走逻辑线程的当前段。

### 可选省略思考内容

历史命令传入 `--omit-thinking` 后，不返回 thinking/reasoning/redacted_thinking 内容块及消息顶层 thinking 字段；默认仍保留。用户正文、助手回复、工具记录与图片按原有规则返回，原始 transcript 不修改。可与 `--view compact` 或 `--view full` 组合。

```sh
agent-manage sessions-segments-history --session-k 'agent:example:dashboard:thread-key' --session-id '<物理段 ID>' --omit-thinking
```

CLI 转发参数名为 `omit-thinking`，支持无值开关、`--omit-thinking true` 和 `--omit-thinking=true`（均省略思考）；显式 `false` 则保留思考，布尔值不区分大小写。未传参数时继承游标设置；显式传值时覆盖游标中的过滤设置，允许从当前分页位置切换，新返回的游标和消息引用记录本次设置。平台可使用 `parameters: {"omit-thinking": "true"}`，需转发为对应 CLI 参数；主机读取器字段为 `omitThinking: true`。分页游标、`fullMessageRef` 及消息分块游标继承该选项，续读只传游标即可；切换过滤设置只影响本次及后续读取，已返回的页面不重新处理。单独 message-get 不带页面 ref 时仍按默认保留思考。

启用 `--omit-thinking` 或默认 compact 模式时，过滤后为空的普通 assistant 消息不占 `limit`，接口继续扫描同一段，直到返回足够消息、达到响应预算或段边界。工具状态、工具调用、图片、用户消息和控制记录仍保留。游标按实际扫描位置推进；若余下记录全部被过滤，可以返回空页及该方向的 null 游标。

### 默认向前读取到用户消息

从末尾读取（`edge: end`，默认）或使用 `olderCursor` 时，除 `limit: 1` 的单条预览外，先取够 `limit` 条过滤后保留的消息。如果这批消息已包含任意 `role: user`，立即停止，不要求第一页边缘恰好是用户消息；只有这批里没有用户消息，才继续向前读取到用户消息（包含它）。因此必要时可能超过 200 条；1 MiB 消息预算仍生效。预算耗尽或段首到达时也会停止，不跨段自动读取。

响应新增 `reachedUser`：本页包含用户消息时为 `true`，否则为 `false`。如果为 false 且有 `olderCursor`，可继续读取；没有游标则已到段首。这里只识别 transcript 的 user 角色，不保证该记录一定来自人工输入，也不保证当前轮已完成。单条超长用户消息可能以 `fullMessageRef` 返回，正文仍需补读。

`limit: 1` 保持单条预览；`edge: start` / `newerCursor` 保持按条数分页。`--omit-thinking`、视图及媒体处理规则不变。
