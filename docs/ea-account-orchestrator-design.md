# EA App 自动登录与 20 级切号编排设计

> 状态：定稿，供 Windows 端开发
>
> 日期：2026-07-31
>
> 服务端配套：`gpt_forge` 的
> `2026-07-31-apex-leases-and-operations-v2-design.md`

## 1. 核心结论

保留现有入口：

```text
windows\play.cmd
  -> run.ps1 play
  -> PlaySessionRunner(ContinuePlayPolicy)
  -> 当前账号持续下一把
```

新增托管入口：

```text
windows\account-cycle.cmd
  -> run.ps1 account-cycle
  -> AccountOrchestrator
       -> AccountProvider 领取租约
       -> EaAppDriver 登录并确认账号
       -> PlaySessionRunner(TargetLevelPolicy)
       -> 安全退出 Apex 和 EA
       -> AccountProvider 关闭租约
       -> 领取下一个账号
```

`account-cycle.cmd` 不启动、包装或轮询 `play.cmd` 子进程。两个入口共同调用
`PlaySessionRunner`。否则会出现运行锁、DXcam、退出码、Reporter 和日志生命周期竞态。

系统边界：

- `CapabilityPilot` 只处理 Apex 游戏内画面和输入；
- `PlaySessionRunner` 只运行一个已绑定、已验证账号的游戏会话；
- `AccountOrchestrator` 处理租约、EA/Apex 进程和跨账号状态机；
- `AccountProvider` 只调用服务端租约/凭据/OTP API；
- `RemoteReporter` 只追加事实，不领取或释放账号；
- `EaAppDriver` 不进入 Apex capability 配置。

## 2. 当前基线

现有代码已经具备：

- `play.cmd -> run.ps1 play -> cli.run_play()` 的单账号闭环；
- capability 派发、覆盖层守卫和动作后置确认；
- 每次进入大厅读取等级/经验；
- 完整数值连续两次一致才产生可信 `LOBBY_PROGRESS`；
- 唯一 `runId`、不可变 manifest、连续本地事件序号；
- 异步 Reporter、独立远程序号、断网补传；
- 设备/账号配置和 Steam ActiveUser 核验；
- `play` 单实例锁、输入释放和有限上报 flush。

自动切号不重写这些能力，只新增外层编排和目标等级策略。

## 3. 目标与非目标

### 3.1 目标

- `play.cmd` 的现有行为、退出码和纯本地模式保持不变；
- 新入口从服务端排他领取一个符合资格的账号；
- 使用 EA App UI Automation 登录、处理 OTP、确认身份并启动 Apex；
- 每个账号使用独立 `runId`、Dispatcher、Pilot、Recorder 和 Reporter session；
- 稳定安全大厅中 `level < targetLevel` 时继续现有游戏闭环；
- 稳定 `level >= targetLevel` 时，同一帧阻止下一次“准备”；
- 达标后安全结束 run、退出 Apex、退出 EA、关闭租约；
- 服务端尚未确认完成时不领取下一个账号；
- 崩溃恢复时先恢复旧租约，不产生第二个租约；
- 密码、OTP 和 Token 不落盘、不进事件、不进截图。

### 3.2 非目标

- 不绕过 EA、Apex 或反作弊机制；
- 不处理 CAPTCHA，出现人机挑战时人工暂停；
- 不读取或注入 Apex 进程内存；
- 不允许服务端下发任意脚本、坐标或输入动作；
- 不用固定坐标或 OCR 文字中心操作 EA 登录控件；
- 不把租约 TTL 过期当成账号已安全登出；
- V1 关游戏超时后不直接强杀进程；
- 不用上一次等级或升级动画推断已经达到目标。

## 4. 进程级资源与入口

### 4.1 单实例锁

`play.cmd` 和 `account-cycle.cmd` 使用同一个进程级运行锁。账号循环从开始到结束一直持有，
内部账号切换不能释放。两种入口不能同时发送输入。

`run.ps1` 只选择 CLI 子命令、配置和 Python 环境，不承载状态机。

### 4.2 DXcam 生命周期

当前 DXcam 为规避 COM finalization 问题，依赖进程结束时 `os._exit()`。因此账号循环不能
每个账号创建并遗留一个捕获源。

正式实现二选一：

1. `account-cycle` 进程只创建一个 `DxcamFrameSource`，注入每个 `PlaySessionRunner`；
2. 先修复并实机验证 DXcam 可以在同一进程内可靠 stop/recreate，再允许会话级创建。

V1 推荐方案 1。`account-cycle` 必须加入 `__main__.py` 的 Windows 硬退出命令名单；硬退出
前必须完成：

- Pilot 停止；
- 输入释放；
- Recorder/result 写入；
- Reporter 线程停止；
- 编排 checkpoint 原子写入；
- 租约状态已持久化。

### 4.3 每账号必须重建

以下对象不能跨账号复用：

```text
CapabilityDispatcher
CapabilityPilot
RunRecorder
runId
RunnerSettings 中的账号/租约绑定
当前会话 Reporter session
进度稳定器和上一账号等级缓存
```

捕获源可以进程级复用，但游戏输入和识别状态不能串号。

## 5. 组件结构

建议模块：

```text
windows/apex_automation/
  play_session.py
  progression_policy.py
  account_orchestrator.py
  account_provider.py
  lease_keeper.py
  ea_app.py
  orchestration_state.py
```

职责：

```text
PlaySessionRunner
  一个已验证账号的一次游戏会话
  Pilot + Recorder + Reporter 事件生产 + 会话清理

ProgressionPolicy
  决定进度读数后继续、达标、暂停或延后

AccountOrchestrator
  跨账号工作流、checkpoint、EA/Apex 生命周期、终态报告补传

AccountProvider
  claim/current/status/credentials/renew/OTP/close

LeaseKeeper
  独立续租，不阻塞 Apex 识别线程

EaAppDriver
  EA App UIA、登录、OTP、身份确认、启动和登出
```

`PlaySessionRunner` 明确不负责：

- CLI 参数；
- 外层运行锁；
- 账号领取和 Provider；
- EA 登录/登出；
- Apex 进程退出；
- 整个进程的 DXcam 和 `os._exit()`。

## 6. 单账号会话

抽出接口：

```python
class PlaySessionRunner:
    def run(
        self,
        identity: SessionIdentity,
        progression_policy: ProgressionPolicy,
        capture_source: FrameSource,
    ) -> PlaySessionResult: ...
```

`SessionIdentity` 至少包含：

```text
accountId
deviceId
leaseId                 托管模式必填，手动模式为空
leaseFence              托管模式必填，手动模式为空
targetLevel             服务端租约值
identityVerification
```

`PlaySessionResult` 至少包含：

```text
status
runId
accountId
leaseId
leaseFence
level
xpCurrentApprox
xpRequiredApprox
progressLocalSeq
lobbyProgressReportSeq
runFinishedReportSeq
frames
actionsSent
roundsStarted
roundsReturnedToLobby
errorCode
error
```

每个 run 的 manifest 固化 `accountId/deviceId/leaseId/leaseFence`，但不含任何 Token、
登录名、密码或 OTP。

关闭租约时，`lobbyProgressReportSeq` 映射为请求的 `lobbyProgressSeq`，
`runFinishedReportSeq` 映射为 `runFinishedSeq`；两者都是远程 outbox 的连续 report
序号，不是本地 `events.jsonl` 的 source seq。

托管模式下 `PlaySessionRunner` 返回后不能立刻销毁 Reporter session。它先关闭 Recorder
并冻结事件生产，再把一个 `ReportDrainHandle` 交给 `AccountOrchestrator`。该 handle 只能
补传这个不可变 run 的既有 outbox、查询 `acceptedThrough` 和停止线程，不能再写事件。
手动 `play.cmd` 仍按现有有界 flush 后退出。

## 7. 进度策略与安全闸门

### 7.1 决策输入

策略不能只收到成功读数。托管模式需要对失败读数 fail closed：

```python
class ProgressionPolicy(Protocol):
    def decide(
        self,
        outcome: ProgressionOutcome,
        context: ProgressionContext,
    ) -> ProgressionDecision: ...
```

```text
ProgressionOutcome
  status                  CONFIRMED | FAILED
  reading
  attempts
  error

ProgressionContext
  observedState
  safeLobby
  queueing
  overlayClear
  pendingAction
  foreground
```

决策只允许：

```text
CONTINUE_PLAY
TARGET_REACHED
PAUSE_UNCERTAIN
DEFER_UNTIL_SAFE_LOBBY
```

Pilot 的顺序固定为：

```text
识别状态
-> 更新大厅访问
-> 结算 pending action
-> 读取并稳定确认等级/经验
-> ProgressionPolicy
-> 只有 CONTINUE_PLAY 才调用 Dispatcher.decide()
-> 发送输入
```

`FAILED` 在托管模式下不能落回 `Dispatcher.decide()`。它必须暂停并低频复核，避免在无法
证明低于 20 级时继续下一局。手动 `play.cmd` 的 `ContinuePlayPolicy` 保持当前行为：
记录失败后继续游戏。

### 7.2 达标条件

必须同时满足：

- 等级和经验完整解析；
- 完整数值元组连续两次一致；
- OCR 置信度达到阈值；
- `level >= targetLevel`，目标来自服务端租约；
- 当前为稳定大厅且不是 `LOBBY_QUEUEING`；
- 不在模式面板；
- 没有活动覆盖层；
- 没有未结算 capability；
- Apex 在前台；
- 租约仍为当前 fence，LeaseKeeper 未报告不确定。

安全大厅：

```text
LOBBY_SELECT_REQUIRED
LOBBY_READY_TARGET_FILL_ON
LOBBY_READY_TRAINING
LOBBY_READY_TARGET
LOBBY_READY_OTHER
```

如果账号从排队、对局或观战中恢复，必须等自然回到安全大厅再判断切号。

### 7.3 服务器资格不能替代登录后预检

服务端只能保证“不领取已知可信等级已经达标”的账号。账号可能被外部人工玩过而服务端
尚未收到新等级，因此托管 Runner 登录后、发送第一次匹配输入前必须先读取大厅等级。
发现已经达标时只完成上报和下号，不进入新对局。

## 8. 达到目标后的不可变顺序

达到目标时按以下顺序执行：

```text
1. 锁住 Pilot，禁止新的 capability 派发
2. release_all()
3. 若释放输入失败，进入人工暂停，不完成租约
4. 本地写 LOBBY_PROGRESS 和 ACCOUNT_TARGET_REACHED
5. 强制写当前状态快照
6. Pilot 返回 TARGET_REACHED
7. 写 pilot summary
8. RunRecorder.finish(TARGET_REACHED)
9. Reporter wake，执行有界 flush
10. 冻结事件生产，Reporter 转为旧 run 的 drain-only 模式
11. checkpoint -> APEX_STOPPING
12. 正常关闭 Apex，确认所有目标进程退出
13. checkpoint -> EA_SIGNING_OUT
14. 退出 EA，确认登录页出现
15. checkpoint -> LEASE_COMPLETING
16. Provider.close(TARGET_REACHED) 幂等提交
17. 服务端证据不足时保持 COMPLETION_PENDING
18. drain-only Reporter 继续补传，同时 Provider.status 查询租约状态
19. 等待引用的两个 report seq 被 accepted 且租约变成 COMPLETED
20. 停止 Reporter 并确认线程退出
21. 原子记录完成确认
22. 清除本地旧租约状态
23. 才能领取下一账号
```

Reporter 有本地待传不能阻塞退出 Apex/EA，也可以先提交 close；服务端在证据未到达时返回
`COMPLETION_PENDING`。此时 LeaseKeeper 继续续租，drain-only Reporter 继续按退避策略
补传，二者都不阻塞 Apex/EA 清理。网络长期不可用时进入可恢复暂停，但不能停止补传后只
轮询状态，也不能启动下一账号 Reporter。只有等级和终态证据到达且租约完成后才能 claim。

## 9. AccountProvider 契约

服务端目标等级和重试策略是权威值。客户端不能传入 `targetLevel` 或 `retryable`。

```python
class AccountProvider(Protocol):
    def claim(
        self,
        claim_request_id: str,
        task_type: str,
    ) -> AccountLease | None: ...

    def current(self) -> LeaseStatus | None: ...

    def status(
        self,
        lease_id: str,
        lease_fence: int,
    ) -> LeaseStatus: ...

    def recover(
        self,
        lease_id: str,
        lease_fence: int,
    ) -> AccountLease: ...

    def credentials(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
    ) -> SecretCredentials: ...

    def renew(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
        phase: str,
        run_id: str | None,
    ) -> LeaseStatus: ...

    def request_otp(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
        challenge_id: str,
        challenge_started_at: datetime,
    ) -> OtpCode: ...

    def close(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
        outcome: str,
        run_id: str | None,
        evidence: CompletionEvidence | None,
        reason_code: str,
        cleanup: CleanupEvidence,
    ) -> LeaseStatus: ...
```

映射接口：

```text
POST /v1/runner/account-leases
GET  /v1/runner/account-leases/current
GET  /v1/runner/account-leases/{leaseId}
POST /v1/runner/account-leases/{leaseId}/credentials
POST /v1/runner/account-leases/{leaseId}/renew
POST /v1/runner/account-leases/{leaseId}/otp-challenges
POST /v1/runner/account-leases/{leaseId}/close
```

所有写调用使用持久化的 `operationId/Idempotency-Key`。同一操作成功或收到确定性、
不可重试的 4xx 前不能生成新 ID。若 `CLEANUP_UNCONFIRMED` 后完成了新的清理，必须先把
原 operation 标记为 rejected，再持久化新 ID 和新正文；不能拿原 key 修改请求重试。
`current()` 只恢复占用型租约；`status()` 可以读取该设备历史租约的终态，用于确认
`COMPLETION_PENDING -> COMPLETED`。查询失败或 fence 不匹配不能被当作完成。

claim 返回 `204` 时 `claim()` 映射为 `None`，编排器按服务端 `Retry-After` 保持空闲，
不启动 EA/Apex，也不自行缩短轮询间隔。

### 9.1 Claim 崩溃窗口

调用 claim 前必须先原子保存 `claimRequestId`：

```text
checkpoint(claimRequestId, pendingOperation=CLAIM)
-> 调用 claim
-> checkpoint(leaseId, leaseFence, expiresAt)
```

如果服务端已经创建租约而客户端在第二次 checkpoint 前崩溃，重启后使用相同
`claimRequestId` 重试，服务端必须返回同一租约。

启动时还要调用 `current()`。即使本地文件丢失，只要设备已有未终结租约，也不能领取第二
个账号。

### 9.2 凭据恢复

密码和 OTP 禁止落盘，所以恢复旧租约时通过受审计、限流、`no-store` 的 Provider 接口
重新读取。Secret 对象必须 `repr=False`，异常和网络日志不能包含响应正文。

### 9.3 失败语义

客户端只提交稳定枚举 `reasonCode`：

```text
LOGIN_INVALID
OTP_TIMEOUT
CAPTCHA
IDENTITY_MISMATCH
EA_UI_UNKNOWN
APEX_START_FAILED
APEX_EXIT_TIMEOUT
EA_SIGNOUT_FAILED
LEASE_STALE
OPERATOR_STOPPED
```

服务端决定冷却、隔离、封禁或是否允许重新入池。

## 10. LeaseKeeper

租约续期由独立 `LeaseKeeper` 负责，从 claim 成功持续到 close 得到服务端终态。
不能放进 Pilot 帧循环。

要求：

- 按服务端 `renewAfter` 调度，不用客户端固定猜测；
- 每次续租带 `leaseFence`、当前 phase 和 API 字段 `runId`；
- 续租调用使用幂等 operation ID；
- 网络失败先进入 `LEASE_UNCERTAIN`；
- fence 过时立即进入 `PAUSED_MANUAL`；
- `LEASE_UNCERTAIN` 时不开始下一局、不启动新账号、不领取下一租约；
- 已在对局中时不盲目中断输入序列，先释放输入并安全暂停；
- close 完成前持续续租；
- checkpoint 保存最后确认的 `expiresAt` 和续租结果。

租约 TTL 过期不能自动恢复。服务端进入 `EXPIRED_UNCONFIRMED` 后，Runner 必须停止新动作并
等待恢复或人工处置。

## 11. EA App 自动化

### 11.1 UI Automation 优先

顺序：

1. 通过进程、窗口类和安装位置确认 EA App；
2. 读取 Microsoft UI Automation 控件树；
3. 按 `AutomationId/ControlType/Name/父子关系` 定位；
4. 普通文本优先 `ValuePattern`；
5. 按钮使用 `InvokePattern`；
6. 密码框不支持 ValuePattern 时，先证明焦点在目标控件，再用 Unicode `SendInput`；
7. 每个动作后重新获取控件并验证新页面。

禁止：

- 固定屏幕坐标；
- OCR 找字后点击文字中心；
- 剪贴板传递密码或 OTP；
- 固定等待后直接假定成功；
- 在登录页保存未脱敏全屏截图。

### 11.2 UIA 勘察门槛

编码前在目标 Windows 记录：

- EA 进程、主窗口和语言；
- 账号、密码、OTP 和按钮暴露的 UIA 模式；
- 控件标识在重启、窗口变化和 EA 更新后是否稳定；
- 更新、离线、错误密码、OTP 过期、账号菜单和退出登录状态；
- Apex 启动入口和启动中状态。

关键控件完全不暴露 UIA 时，第二方案只能是：

```text
UIA 确认目标窗口
-> 已验证 Tab 导航
-> Unicode SendInput
-> UIA/视觉后置确认
```

仍无法建立确定性后置条件时进入人工暂停。

### 11.3 OTP

OTP 必须绑定本次 challenge：

```text
challengeId
challengeStartedAt
receivedAt
expiresAt
```

拒绝早于本次 challenge 的旧验证码。有限超时和退避由 Orchestrator 处理，不进入游戏识别
线程。

### 11.4 身份确认

- Apex 已运行但无法证明账号归属时人工暂停；
- EA 已登录目标账号时可跳过登录，但仍需身份确认；
- EA 已登录其他账号时先退出并确认登录页；
- EA 登录标识、服务端 `accountId` 和 EA 稳定账号 ID 是不同字段；
- 身份不一致时不能启动 Apex或发送游戏输入。

当前 Steam 模式可读取 ActiveUser。EA 模式落地时必须由 `EaAppDriver` 提供稳定身份事实，
不能只凭页面显示昵称。

## 12. Apex 启动与退出

启动成功条件：

- 目标 Apex 进程出现；
- 目标窗口存在；
- 捕获源取得符合配置分辨率的非空帧；
- 前台守卫识别目标进程；
- 当前租约 fence 仍有效。

达到目标后的退出：

1. Pilot 和 Reporter session 已安全结束；
2. 向窗口发送正常关闭请求；
3. 确认 `r5apex.exe/r5apex_dx12.exe` 全部结束；
4. 超时进入人工暂停，V1 不强杀；
5. Apex 完全结束后才退出 EA；
6. EA 登录页出现后才声明 `eaSignedOut=true`；
7. Provider close 未确认完成前保留租约 checkpoint。

## 13. 本地 checkpoint

文件：

```text
windows/runs/account-cycle-status.json
```

允许字段：

```text
schemaVersion
deviceId
claimRequestId
leaseId
leaseFence
leaseExpiresAt
accountId
targetLevel
workflowPhase
runState
resumePhase
pendingOperation
operationId
activePlayRunId
previousPlayRunIds
targetReading
reportEvidence
lastErrorCode
updatedAt
```

`workflowPhase` 与暂停态分开：

```text
workflowPhase
  CLAIMING
  EA_STARTING
  EA_SIGNING_IN
  EA_IDENTITY_VERIFYING
  APEX_STARTING
  APEX_PLAYING
  APEX_STOPPING
  EA_SIGNING_OUT
  LEASE_COMPLETING

runState
  ACTIVE
  PAUSED_RETRYABLE
  PAUSED_MANUAL
  STOPPED
```

`resumePhase` 指明暂停解除后继续哪个副作用，不能用 `PAUSED_*` 覆盖工作阶段。

禁止字段：

```text
loginIdentifier
password
otp
reportToken
providerToken
完整 HTTP 请求/响应
完整异常正文
```

规则：

- 所有外部副作用先写 checkpoint，再调用；
- 所有状态写入使用临时文件 + fsync + 原子替换；
- operation 成功确认后再推进 phase；
- Provider 完成确认前不能清除租约；
- 清除旧租约状态前先保存终态审计摘要。

## 14. 崩溃和 orphan run 恢复

启动顺序：

```text
读取 checkpoint
-> Provider.current()
-> 对齐本地/服务端租约
-> 检查 activePlayRunId 的 manifest/result/events
-> 检查 EA/Apex 当前进程和登录身份
-> 选择恢复 phase
```

规则：

- 服务端有租约、本地无租约：恢复服务端租约，不 claim；
- 本地有租约、服务端无对应租约：人工暂停，不自行登录；
- 旧 run 已有 `TARGET_REACHED` 或对应 result：绝不恢复匹配，继续 Apex/EA 清理和 close；
- 旧 run 没有 `RUN_FINISHED`：将旧 run 以 `CRASHED` 收口，不向旧 JSONL 随意续写；
- 租约有效且 EA 身份一致：新建 `runId` 恢复同一账号；
- EA 身份无法确认：人工暂停；
- Apex/EA 未确认退出前，不 release、不 claim；
- `COMPLETION_PENDING`：继续补传/查询，不能重新打一局；
- Provider `close`、claim 和 renew 都用原 operation ID 幂等重试。

## 15. 托管上报

托管 run 每批报告携带：

```json
{
  "lease": {
    "leaseId": "lease_...",
    "leaseFence": 17
  }
}
```

服务端首次接收 run 时固定：

```text
deviceId
accountId
runId
leaseId
leaseFence
```

租约结束后仍允许这个已绑定 run 补传历史事件，但旧租约不能授权创建新的 run。普通
`play.cmd` 不带 lease 上下文，继续使用静态账号授权。

Reporter Token 使用设备级独立秘密，Provider Token 使用更高权限的另一个秘密。切账号
不能更换成只对当前账号有效的短期 Reporter Token，否则上一账号的历史补传会收到 403。

## 16. 配置

非敏感配置：

```text
windows/config/ea-app.zh-CN.json
```

包含：

```text
EA 可执行文件/窗口候选
UIA 控件选择器
页面和动作超时
最大重试次数
Apex 启动入口
允许语言
脱敏区域
```

私有设备配置：

```text
reportUrl
reportToken
providerBaseUrl
providerToken
deviceId
```

手动 `play.cmd` 可以继续带固定 `accountId`。托管配置不固定账号，`accountId/targetLevel`
来自租约，并只固化到单次 run manifest。

## 17. 实施阶段

### 阶段 A：会话边界

1. 抽取 `PlaySessionRunner`；
2. 保持 `play.cmd` 行为和测试不变；
3. 实现 `ProgressionPolicy`、`ContinuePlayPolicy`、`TargetLevelPolicy`；
4. 托管读取失败在 dispatcher 前暂停；
5. 定义每账号新建对象和进程级 capture。

### 阶段 B：本地编排

1. 实现 `AccountOrchestrator`、checkpoint 和恢复；
2. 实现 `FakeAccountProvider`；
3. 实现 `LeaseKeeper`；
4. 覆盖 claim 崩溃窗口、幂等和 orphan run；
5. 保证秘密不落盘。

### 阶段 C：EA App 实机

1. 勘察 UIA 控件树；
2. 登录、OTP、身份确认、启动、退出；
3. 覆盖更新、离线、错误密码、过期 OTP 和未知页面；
4. 只保存脱敏证据。

### 阶段 D：真实 Provider

1. 接入 claim/current/status/credentials/renew/OTP/close；
2. 接入 leaseId/fence 上报；
3. 验证 `COMPLETION_PENDING`；
4. 验证历史补传和旧 fence；
5. 验证租约过期隔离。

### 阶段 E：20 级长跑

1. 取得真实 19、20 和其他两位数等级大厅样本；
2. 验证 19 -> 20 同帧停止准备；
3. 连续切换至少三个账号；
4. 覆盖断网、进程重启和租约过期；
5. 检查所有日志、manifest、checkpoint 和截图没有秘密。

## 18. 验收标准

1. `play.cmd` 不配置 Provider 时行为不变；
2. 新入口不启动 `play.cmd` 子进程；
3. 两入口不能同时取得运行锁；
4. 账号循环只持有一个可控的 DXcam 进程级生命周期；
5. 每账号使用新的 run、Dispatcher、Pilot 和 Recorder；
6. claim 响应丢失后重试得到同一租约；
7. 重启时先恢复旧租约，不领取第二个；
8. LeaseKeeper 不确定时不开始下一局；
9. 旧 fence 使自动化停止；
10. 托管等级读取失败不会落入 Dispatcher；
11. 稳定 `level < target` 继续匹配；
12. 稳定 `level >= target` 同一帧不发送准备；
13. 达标后输入、run 和 Reporter 按固定顺序结束；
14. Reporter 待传不阻塞安全退出，但阻止领取下一账号直到服务端完成；
15. Apex 未退出时不退出 EA；
16. EA 未登出时不完成租约；
17. `COMPLETION_PENDING` 重启后不会重新打一局；
18. orphan run 不会被继续追加或串账号；
19. 历史 run 补传不依赖当前账号；
20. `COMPLETION_PENDING` 时旧 Reporter 持续 drain，不能只轮询租约；
21. 密码、OTP 和 Token 不出现在任何持久化产物中。

## 19. 当前实现状态

截至 2026-07-31，阶段 A 和不依赖真实接口的阶段 B 骨架已经落地：

- `PlaySessionRunner` 已从 `run_play()` 抽出，手动入口使用
  `ContinuePlayPolicy`，原有 `play.cmd` 行为保持不变；
- `TargetLevelPolicy` 已接在等级稳定确认与 `Dispatcher.decide()` 之间，20 级同帧不会再发送
  “准备”，托管读取失败会暂停并低频复核；
- 托管 manifest 和每个远程批次已携带 `leaseId/leaseFence`；
- 已实现 `AccountProvider` 协议、真实 `HttpAccountProvider`、`FakeAccountProvider`、
  原子 checkpoint、`LeaseKeeper` 和 `AccountOrchestrator`；
- 已覆盖 claim 响应丢失、Provider 写操作幂等、续租不确定、终态清理顺序和
  `COMPLETION_PENDING` 重启不重打；
- `account-cycle.cmd`、CLI 子命令、共享运行锁和进程级 DXcam 生命周期已经接线；
- `account-cycle` 已加入 Windows `os._exit()` 安全退出名单。

目前 `account-cycle.cmd` 会读取服务端导出的 `windows/account-cycle.private.json`，使用其中
的 `leaseUrl/providerToken` 自动构造 HTTP Provider 与 `PlaySessionRunner`。固定账号
`play.cmd` 使用另一份 `windows/runner.private.json`，避免静态 `accountId` 与动态租约配置
互相冲突。自动切号入口仍然 fail closed，不会在缺少 EA UIA 驱动时领取账号。
剩余阻塞项：

1. 在目标 Windows 勘察并记录 EA App UIA 控件树，之后实现真实 `EaAppDriver`；
2. 实现真实 EA UIA 适配器并注入 CLI；在此之前不添加坐标、OCR 点击或猜测的 UIA
   选择器；
3. 结合真实进程/身份事实完成 `APEX_PLAYING` orphan run 的受控 `CRASHED` 收口；当前会
   人工暂停且绝不重打一局；
4. 实机验证 Apex 正常关闭、EA 登出、DXcam 单进程复用和连续三账号切换。

控件树勘察入口为 `windows/probe-ea-uia.cmd`。它只读 UI Automation 元数据，不点击、
不输入、不读取 ValuePattern，并对邮箱和数字验证码做脱敏；结果写入
`windows/runs/ea-uia-*.txt`。

托管私有配置示例见 `windows/account-cycle.private.example.json`。优先使用服务端导出的
`leaseUrl/providerToken`，同时兼容旧示例的 `providerBaseUrl`；Token 不会写入 manifest、
checkpoint、事件或截图。
