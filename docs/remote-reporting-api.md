# 远程上报 API 规范（V1）

> 状态：服务端和 Windows 上报端的联调基线。
>
> V1 只做 Windows 主动上报，不提供远程控制。一个 HTTPS 接口即可完成事件、
> 心跳、账号等级经验和少量异常证据的上报。
> 手动 `play.cmd` 和以后由账号租约驱动的自动循环共用此接口；托管运行只增加
> 一个可选的 `lease` 上下文，不改变事件模型。
>
> 可直接导入服务端工具的定义见
> [`remote-reporting-openapi.yaml`](remote-reporting-openapi.yaml)。

## 1. 目标与边界

V1 需要满足：

- 按账号查询运行记录、局数、状态和等级经验；
- Windows 断网或服务端短暂不可用时，本地任务继续运行；
- 恢复联网后能够补传，服务端不会重复入库；
- 不上传普通游戏帧，不把网络请求放进识别或输入线程；
- 同一台 Windows 可以先后运行不同账号；
- 服务端可以拒绝无权上报的账号与设备组合。

V1 不包含：

- 远程暂停、继续、停止或发送输入；
- 服务端主动连接 Windows；
- 实时视频或连续截图流；
- 远程下发任意动作、脚本或坐标。

## 2. 身份模型

六个标识不能混用：

| 字段 | 含义 | 生成方 | 生命周期 |
|---|---|---|---|
| `accountId` | `apex_profiles.public_id` | 服务端 | 长期稳定 |
| `deviceId` | Windows 运行设备/安装实例 | 服务端或安装时生成 | 长期稳定 |
| `leaseId` | 一次账号独占租约；手动模式为空 | 服务端 | 一次账号领用 |
| `leaseFence` | 账号每次重新分配时单调增加的 fencing token | 服务端 | 随租约固定 |
| `runId` | 一次单账号游戏会话 | Windows | 单次会话 |
| `seq` | 会话内事件序号 | Windows | 每个 `runId` 从 1 递增 |

`accountId` 必须使用服务端 `apex_profiles.public_id` 分配的不透明 ID，例如
`acct_01J...`。不要用 `apex_profiles.id`、`ea_accounts.id`、玩家昵称、邮箱、Steam
登录名或 EA 显示名；其中有些是内部实现，有些可以修改或重复。

与 `gpt_forge` 现有账号表的权威关联为：

```text
Runner accountId
  -> apex_profiles.public_id
  -> apex_profiles.ea_account_id
  -> ea_accounts.id
  -> ea_steam_links
  -> steam_accounts.id
```

服务端从 Token 得到设备、从 `accountId` 得到 Apex 档案和所属用户。请求体不接收
`user_id`、EA/Steam 用户名或邮箱，也不能让客户端用这些字段覆盖服务端关联。

一个 `runId` 启动后必须固定绑定一个 `accountId`，运行中不允许切换。需要换账号时结束
当前会话，再以新的 `accountId` 启动。

Reporter Token 必须在服务端绑定允许上报的 `deviceId`，且只能拥有 `report:write`
权限。服务端不能只相信客户端传来的设备或账号 ID。账号租约使用另一个 Account
Provider Token；它可以拥有 `lease:*`、`credentials:read` 和 `otp:read`，但不能与
Reporter Token 或网页登录 JWT 共用同一个秘密。

授权分为两种：

- 手动 `play.cmd` 不携带 `lease`，服务端按静态设备/账号授权校验；
- 托管 `account-cycle.cmd` 每个批次都必须携带相同的 `leaseId + leaseFence`，服务端
  按租约和 run 的不可变绑定校验。

托管 run 的第一批有效报告会永久固定以下五元组：

```text
deviceId + accountId + runId + leaseId + leaseFence
```

之后任何字段变化都返回 `409 RUN_BINDING_CONFLICT`。租约服务的 `close` 如果先于
首批报告到达，也可以先用 `runId` 建立同样的不可变绑定；报告端只能补齐该绑定。

只有当前有效租约可以授权创建新的托管 run。租约完成后，已经绑定的历史 run 仍可继续
补传；已完成、失败、释放或过期待确认的租约不能授权新的 run。旧 fence 的已绑定历史
事件可以保存用于审计，但不能修改当前租约、当前 run、当前等级或最高可信等级等任何
当前投影。旧 fence 试图创建新 run 时返回 `409 STALE_LEASE`。

`RUN_FINISHED` 只是运行事实，不能释放租约或直接把账号改为 `ready_for_sale`。达到目标
后的 `COMPLETION_PENDING`、证据校验、租约完成和业务阶段流转由租约服务处理。

管理页面选择 Apex 账号和 Windows 设备后，生成账号/设备作用域的私有 Runner 配置。
当前脚本把该配置固化进本次 manifest（不含 Token），历史补传始终读取历史会话自己的
`accountId`，不能用后来切换的新账号覆盖。

昵称 OCR 只能作为人工核对信息。若服务端能够提供 `steam_id64`，配置同时下发
`expectedPlatform=steam` 和 `expectedPlatformAccountId`；Windows 启动时读取 Steam
`ActiveUser` 并比较，明确不一致时在任何游戏输入前退出。没有稳定平台 ID 时只能标记为
`CONFIGURED` 或 `UNAVAILABLE`，不能声称已经验证了实际登录账号。
`gpt_forge` 当前 `steam_accounts` 只有登录名，服务端实现时建议增加可空且唯一的
`steam_id64`；在它被可信采集前，页面应明确显示“账号未自动核验”。

本次核验结果随 `RUN_STARTED.identityVerification` 上报。后台应明确展示核验状态；
`CONFIGURED` 表示只确认了页面生成配置时选择的账号，不等于已经证明游戏中登录的账号。

## 3. 接口

```http
POST /v1/runner/reports
Authorization: Bearer <reporter-token>
Content-Type: application/json
```

只允许 HTTPS。服务端应在完整、持久化提交本批事件后返回 `200`。

### 3.1 请求体

```json
{
  "schemaVersion": 1,
  "accountId": "acct_01K1ABCDEF",
  "deviceId": "dev_01K1UVWXYZ",
  "runId": "run_20260731_001101_7f3a",
  "lease": {
    "leaseId": "lease_01K1LEASE",
    "leaseFence": 17
  },
  "sentAt": "2026-07-31T01:12:30.221+08:00",
  "client": {
    "appVersion": "0.5.0",
    "profile": "play-2560x1440.zh-CN",
    "configRevision": "sha256:..."
  },
  "events": [
    {
      "seq": 42,
      "occurredAt": "2026-07-31T01:12:28.103+08:00",
      "elapsedMs": 115421,
      "type": "MATCH_PHASE_CHANGED",
      "payload": {
        "phase": "IN_MATCH",
        "previousPhase": "DROPSHIP",
        "roundNumber": 1
      }
    }
  ]
}
```

字段约束：

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `schemaVersion` | integer | 是 | V1 固定为 `1` |
| `accountId` | string | 是 | 1～128 字符，服务端已存在 |
| `deviceId` | string | 是 | 1～128 字符，必须与令牌匹配 |
| `runId` | string | 是 | 1～128 字符，在设备内唯一 |
| `lease` | object | 托管模式是 | 手动 `play.cmd` 省略；托管 run 每批必填 |
| `lease.leaseId` | string | `lease` 存在时是 | 1～128 字符，服务端租约 ID |
| `lease.leaseFence` | integer | `lease` 存在时是 | 正整数，必须等于该 run 的固定 fence |
| `sentAt` | RFC 3339 string | 是 | 客户端发送批次的时间 |
| `client` | object | 是 | 客户端版本和配置身份 |
| `events` | array | 是 | 1～100 条，按 `seq` 升序 |
| `events[].seq` | integer | 是 | 正整数，在本次会话内严格递增 |
| `events[].occurredAt` | RFC 3339 string | 是 | 事件真实发生时间 |
| `events[].elapsedMs` | integer | 是 | 从本次会话启动起算，非负 |
| `events[].type` | string | 是 | 必须是第 5 节定义的类型 |
| `events[].payload` | object | 是 | 类型对应的结构化数据 |

同一个请求内的事件必须属于同一个账号、设备、会话和租约上下文。需要补传其他会话时
另发一个请求。`lease` 要么整体省略，要么两个子字段同时存在；不能发送 `null` 或只传
其中一个字段。

### 3.2 成功响应

```json
{
  "schemaVersion": 1,
  "accountId": "acct_01K1ABCDEF",
  "deviceId": "dev_01K1UVWXYZ",
  "runId": "run_20260731_001101_7f3a",
  "acceptedThrough": 42,
  "serverTime": "2026-07-31T01:12:30.298+08:00"
}
```

`acceptedThrough` 表示该会话从 `seq=1` 开始，服务端已经连续、持久化保存到哪个序号。
Windows 端只能在收到这个响应后推进本地确认游标。响应丢失时会重传，服务端必须幂等。

如果服务器已经保存到 `42`，客户端再次发送 `38～42`，仍返回 `200` 和
`acceptedThrough: 42`。

### 3.3 幂等与冲突

服务端唯一键：

```text
(deviceId, runId, seq)
```

- 同一个唯一键、内容相同：视为重传，返回成功；
- 同一个唯一键、内容不同：返回 `409 SEQUENCE_CONFLICT`；
- 批次中间缺号：可以保存已收到的数据，但 `acceptedThrough` 只能返回最高连续序号；
- 不依赖 HTTP 请求 ID 做事件幂等。

### 3.4 错误响应

统一格式：

```json
{
  "error": {
    "code": "ACCOUNT_NOT_ALLOWED",
    "message": "device is not allowed to report for this account",
    "retryable": false
  }
}
```

| HTTP | 场景 | 客户端行为 |
|---|---|---|
| `400` | JSON 或字段格式错误 | 停止盲目重试，保留本地证据 |
| `408` | 服务端在处理请求前超时 | 保留本地事件并退避重试 |
| `401` | Token 缺失或无效 | 停止重试并显示配置错误 |
| `403` | 设备无静态账号权限，或无权使用目标租约 | 停止重试并显示授权错误 |
| `409` | 序号内容冲突、run 绑定冲突或旧 fence 创建新 run | 停止该会话自动重试并人工检查 |
| `413` | 请求过大 | 拆小批次后重试 |
| `422` | 不支持的事件类型或版本 | 停止盲目重试 |
| `429` | 限流 | 按 `Retry-After` 重试 |
| `5xx` | 服务端暂时不可用 | 本地保留并指数退避 |

建议请求正文上限为 2 MiB。无证据图片的普通批次建议控制在 512 KiB 内。

## 4. 重试与离线补传

Windows 端先把全部本地事件写入 `events.jsonl`，Reporter 再投影需要远程发送的事件：

```text
events.jsonl          本地权威事件，local seq 连续
report-outbox.jsonl   远程关键事件，独立 report seq 连续
report-state.json     sourceThrough / acceptedThrough / 最近错误
```

周期近战等高频事件会被过滤，不能直接沿用本地序号，否则服务端永远等不到被过滤的缺号。
远程 outbox 因此使用独立、从 1 开始连续的 `seq`，并保留仅供本地排查的 `sourceSeq`。
网络发送不能成为本地落盘成功的前置条件。

建议退避：

```text
2s → 5s → 10s → 30s → 60s
```

之后维持最长 60 秒间隔；收到 `Retry-After` 时以服务端值为准。每次成功后恢复正常发送。

Windows 端为每个会话原子保存 `acceptedThrough`。下次启动时扫描未确认的历史会话并补传，
所以进程崩溃、断电或重启不会丢失尚未上报的事件。

## 5. 事件类型

### 5.1 `RUN_STARTED`

每次会话恰好一次，通常为 `seq=1`。

```json
{
  "mode": "play",
  "targetMode": "bot-royale",
  "resolution": [2560, 1440],
  "language": "zh-CN",
  "identityVerification": {
    "status": "VERIFIED",
    "observedPlatform": "steam",
    "observedPlatformAccountId": "76561198000000000",
    "message": null
  }
}
```

### 5.2 `HEARTBEAT`

默认每 30 秒一次。它表示 Reporter 和本地任务仍然存活，不要求画面状态发生变化。

```json
{
  "runtimeState": "RUNNING",
  "observedState": "IN_MATCH_ALIVE",
  "foreground": true,
  "roundNumber": 2,
  "frames": 853,
  "actionsSent": 148,
  "pendingReportCount": 3
}
```

高频近战次数等统计放进心跳聚合，不需要逐条远程上传。

### 5.3 `STATE_CHANGED`

```json
{
  "from": "FREEFALL",
  "to": "IN_MATCH_ALIVE",
  "source": "gameStates",
  "ruleId": "in-match-alive",
  "confidence": 0.9984,
  "observationVersion": 18
}
```

### 5.4 `ACTION_RESULT`

`status` 允许：

```text
SENT | CONFIRMED | REJECTED | FAILED
```

```json
{
  "capability": "post-match-return-lobby",
  "action": "returnLobbySequence",
  "status": "CONFIRMED",
  "attempt": 1,
  "originState": "POST_MATCH_SUMMARY",
  "evidenceState": "LOBBY_READY_TARGET",
  "reason": null
}
```

周期近战的 `SENT` 默认只在本地记录；远程心跳发送累计次数。菜单、跳伞、回大厅等关键动作
可以上报完整结果。

### 5.5 `MATCH_PHASE_CHANGED`

`phase` 允许：

```text
LOBBY | QUEUEING | DROPSHIP | FREEFALL | IN_MATCH | SPECTATING |
POST_MATCH | LOBBY_RETURNED
```

```json
{
  "phase": "LOBBY_RETURNED",
  "previousPhase": "POST_MATCH",
  "roundNumber": 2
}
```

### 5.6 `LOBBY_PROGRESS`

每次大厅状态稳定出现时读取。`reason` 允许：

```text
INITIAL | RETURNED_AFTER_MATCH | STATE_REENTRY
```

```json
{
  "reason": "RETURNED_AFTER_MATCH",
  "level": 9,
  "xpCurrentApprox": 7870,
  "xpRequiredApprox": 8150,
  "rawText": "7.87K / 8.15K",
  "levelRawText": "9",
  "xpRawText": "7.87K / 8.15K",
  "confidence": 0.98,
  "levelConfidence": 0.99,
  "xpConfidence": 0.98,
  "changed": true,
  "deltaApprox": 4440,
  "readStatus": "OK"
}
```

约束：

- `level`、`xpCurrentApprox`、`xpRequiredApprox` 来自固定大厅 ROI；
- `K` 显示值只能转换为近似整数，因此字段名必须保留 `Approx`；
- 必须保留等级和经验各自的原始文字、置信度以及聚合后的 `rawText/confidence`，便于复核；
- OCR 失败时 `readStatus` 为 `FAILED`，数值字段为 `null`，不能沿用旧值冒充新读数；
- `deltaApprox` 只在同等级且前后读数可信时由客户端提供，跨等级可由服务端结合历史计算；
- 服务端应将每一条作为账号进度快照保存，不要只覆盖账号表上的最新值。

OCR 失败示例：

```json
{
  "reason": "STATE_REENTRY",
  "level": null,
  "xpCurrentApprox": null,
  "xpRequiredApprox": null,
  "rawText": "",
  "levelRawText": "",
  "xpRawText": "",
  "confidence": 0.0,
  "levelConfidence": 0.0,
  "xpConfidence": 0.0,
  "changed": false,
  "deltaApprox": null,
  "readStatus": "FAILED",
  "error": "大厅等级经验连续读数不一致"
}
```

### 5.7 `INCIDENT`

`kind` 建议包含：

```text
UNKNOWN_SCREEN | CAPTURE_ERROR | OCR_ERROR | RESOLUTION_MISMATCH |
FOREGROUND_LOST | ACTION_PAUSED | REPORTER_ERROR
```

```json
{
  "kind": "UNKNOWN_SCREEN",
  "severity": "WARNING",
  "message": "screen remained unknown for 20 seconds",
  "observedState": null,
  "localEvidencePath": "screenshots/009-unknown.png"
}
```

`localEvidencePath` 仅用于本地复盘，服务端不能把它当作可访问地址。

### 5.8 `RUN_FINISHED`

每次会话至多一次。

```json
{
  "status": "STOPPED",
  "reason": "用户停止",
  "durationMs": 606592,
  "frames": 853,
  "actionsSent": 148,
  "roundsStarted": 2,
  "roundsReturnedToLobby": 1
}
```

自动循环达到目标时 `status` 使用 `TARGET_REACHED`。服务端只保存这一事实和派生运行态；
不得因为收到该事件就释放租约或修改账号业务阶段。租约完成接口会引用同 run 的
`LOBBY_PROGRESS` 和 `RUN_FINISHED` 序号，并在证据齐全后完成状态流转。

## 6. 可选证据图片

一个接口仍可支持少量图片。在 `INCIDENT` 的 `payload` 中增加：

```json
{
  "evidence": {
    "mediaType": "image/jpeg",
    "encoding": "base64",
    "sha256": "8b1a9953...",
    "content": "<base64>",
    "width": 1280,
    "height": 720
  }
}
```

V1 建议限制：

- 只允许 `UNKNOWN_SCREEN`、严重错误和人工明确开启的证据；
- 单张压缩后不超过 512 KiB；
- 最长边不超过 1280；
- 默认不上传普通大厅、对局和结算截图；
- 等级经验默认只上传结构化数值，不上传全屏；
- 截图可能包含玩家昵称，服务端必须设置访问控制和保留期限。

以后图片量明显增加时，再拆成对象存储上传接口；这不影响 V1 事件协议。

## 7. 服务端最小数据模型

直接复用 `gpt_forge` 已有的 `ea_accounts`、`steam_accounts` 和 `ea_steam_links`，新增：

```text
steam_accounts（现有表扩展）
  steam_id64               NULLABLE UNIQUE，用于 Windows 当前账号核验

apex_profiles
  id
  public_id                 UNIQUE，acct_...
  user_id
  ea_account_id             UNIQUE -> ea_accounts.id
  stage
  stage_revision
  current_level
  highest_trusted_level
  current_xp_approx
  next_level_xp_approx
  target_level              DEFAULT 20
  level_confidence
  level_observed_at
  level_trust               unknown/trusted/review
  lease_fence               DEFAULT 0
  last_reported_at
  created_at
  updated_at

apex_runners
  id
  public_id                 UNIQUE，dev_...
  user_id
  display_name
  status
  last_seen_at
  last_version
  revoked_at
  created_at
  updated_at

apex_runner_tokens
  id
  runner_id
  token_prefix
  token_hash
  scopes
  created_at
  last_used_at
  revoked_at

apex_runner_account_permissions
  runner_id
  apex_profile_id
  created_at
  revoked_at

apex_runs
  id
  runner_id
  client_run_id
  apex_profile_id
  lease_id                  NULLABLE -> apex_account_leases.id
  lease_fence               NULLABLE
  mode
  state
  current_phase
  started_at
  last_heartbeat_at
  finished_at
  result
  last_error_code
  last_error_message
  created_at
  updated_at

apex_run_events
  run_id
  sequence
  occurred_at
  type
  payload_json

apex_level_snapshots
  apex_profile_id
  run_id
  event_sequence
  observed_at
  level
  xp_current_approx
  xp_required_approx
  raw_text
  level_raw_text
  xp_raw_text
  confidence
  level_confidence
  xp_confidence
  read_status
  trust_status              unknown/trusted/review/audit_only/invalidated
  invalidated_at            NULLABLE
  invalidated_by            NULLABLE
  invalidation_reason       NULLABLE
  received_at
```

关键约束：

```text
UNIQUE (apex_runs.runner_id, apex_runs.client_run_id)
UNIQUE (apex_run_events.run_id, apex_run_events.sequence)
UNIQUE (apex_level_snapshots.run_id, apex_level_snapshots.event_sequence)
UNIQUE (apex_runner_account_permissions.runner_id, apex_profile_id)
INDEX  (apex_profiles.public_id)
INDEX  (apex_run_events.type, occurred_at)
```

Reporter Token 使用高熵随机值，数据库只保存不可逆哈希和便于定位的前缀，不复用网页
登录 JWT，也不使用可解密的账号凭据加密器。Provider Token 可复用 token 表，但必须是
另一个秘密和独立 scope。处理一批事件时，在一个数据库事务内写入事件、
更新运行态，并将 `LOBBY_PROGRESS` 写入 `apex_level_snapshots`；只有授权仍有效、run
绑定正确且 fence 未过时的可信快照可以更新 `apex_profiles` 当前等级。历史旧 fence 的
快照仍可保存，但只用于审计。`user_id` 必须从 EA 账号所有权派生，不信任 Runner 请求体。
旧 fence 快照的 `trust_status` 固定为 `audit_only`，不能进入当前等级、高水位或完成证据
计算。
每次可信更新同时执行
`highest_trusted_level = max(highest_trusted_level, current_level)`；只有管理员先作废
错误快照并留下审计记录后才能重新计算并降低该高水位。快照作废、当前值/高水位重算、
profile revision 增加和审计写入必须在同一事务完成。

首次创建托管 `apex_runs` 时必须在同一事务内验证并固化 `lease_id/lease_fence`。现有 run
的任何绑定字段不同都返回冲突，不能通过 upsert 覆盖。`RUN_FINISHED` 只更新运行记录；
`COMPLETION_PENDING` 及 `leveling -> ready_for_sale` 由租约服务在校验证据后处理。
如果 renew/close 早于首批报告，租约服务可以先按同一唯一键创建不可变的占位
`apex_runs`；报告接口只能补全该行，不能另建或改绑。

## 8. 服务端验收用例

服务端完成以下用例即可开始与 Windows 联调：

1. 正常提交 `seq=1..10`，返回 `acceptedThrough=10`。
2. 重复提交 `seq=6..10`，不重复入库，仍返回 `10`。
3. 先提交 `seq=12`，返回的连续确认仍为 `10`；补交 `11` 后返回 `12`。
4. 相同 `(deviceId, runId, seq)` 换一个 payload，返回 `409`。
5. Token 对应设备无权上报目标账号，返回 `403`。
6. `LOBBY_PROGRESS` 能按 `accountId` 查询时间线。
7. 服务端短暂返回 `503` 后，客户端重试不产生重复事件。
8. 超过大小限制的内联图片返回 `413`，普通事件仍可拆批重传。
9. 手动 `play.cmd` 省略 `lease` 时，按静态设备/账号权限正常上报。
10. 托管 run 首批报告固定账号、设备、租约和 fence，后续任一字段变化返回 `409`。
11. 已完成租约的已绑定 run 可以补传历史事件，但该租约不能授权新的 run。
12. 收到 `RUN_FINISHED/TARGET_REACHED` 不会自动释放租约或修改业务阶段。
13. 旧 fence 的历史事件可留存审计，但不会更新当前运行、等级或租约状态。

## 9. Windows 端配置约定

远程 Reporter 默认关闭。推荐由 Apex 页面生成以下下载内容，保存为
`windows/runner.private.json`：

```json
{
  "schemaVersion": 1,
  "enabled": true,
  "reportUrl": "https://example.com/v1/runner/reports",
  "reportToken": "<reporter-token>",
  "deviceId": "dev_01K1UVWXYZ",
  "accountId": "acct_01K1ABCDEF",
  "accountLabel": "内部备注，可选",
  "expectedPlatform": "steam",
  "expectedPlatformAccountId": "76561198000000000"
}
```

`expectedPlatform` 两项可暂时省略；省略时仍能按“页面选择的账号”归档，但无法自动证明
Steam 当前登录的就是该账号。环境变量也可以覆盖私有文件：

```text
APEX_REPORT_URL=https://example.com/v1/runner/reports
APEX_REPORT_TOKEN=<reporter-token>
APEX_DEVICE_ID=dev_01K1UVWXYZ
APEX_ACCOUNT_ID=acct_01K1ABCDEF
```

Token 不写入仓库、事件日志、截图或错误上报。启用 Reporter 时四项必须齐全，否则
`play.cmd` 在发送任何游戏输入前明确报配置错误；Reporter 未启用时仍保持当前纯本地行为。

上述是手动模式配置。托管 `account-cycle.cmd` 不在私有文件中固定 `accountId`，账号、
目标等级、`leaseId` 和 `leaseFence` 来自租约接口，并固化到各 run manifest。托管模式
仍使用设备级 Reporter Token 上报历史 run；另行配置的 Provider Token 只用于租约、
凭据和 OTP 接口。

一个 `POST /v1/runner/reports` 足够承载 Runner 遥测，但“页面选择账号并生成配置”属于
登录后的管理能力，不是该上报接口自动完成的。管理后台至少还要提供设备创建/撤销、
账号授权/解绑和 Token 轮换；生成或轮换时只展示一次明文 Token。第一版可以直接下载配置
文件，不必再给 Runner 增加在线激活接口。

## 10. `gpt_forge` 上线前检查

远程接口暴露前至少完成：

- `apex_profiles` 增加唯一、不透明的 `public_id`，不要把内部整数主键发给 Runner；
- Reporter Token 独立生成和哈希保存，不复用浏览器 JWT；
- Reporter Token 与 Account Provider Token 使用不同秘密和最小权限；
- JWT 密钥必须由部署环境提供，删除固定默认值，并且启动日志不能打印实际密钥；
- 账号密码不得保留明文副本；
- SQLite 开启 WAL，单个上报批次在一个短事务内提交，避免多台 Runner 互相长期阻塞；
- 对 `(runner_id, client_run_id, sequence)` 做数据库唯一约束，而不是只在应用层查重。
