# EA 自动切号闭环进度（2026-08-01）

## 结论

自动取号、租约续期、失败上报和失败清理已经通过 Windows 真机验证；EA 登录也曾在前面的实测中成功进入并识别到预期 EA ID，但**最新代码上的完整闭环还没有验证完成**。

真正的一级问题不是某个 UI 判定，而是**失败没有留下可复核的证据**：`account_orchestrator.run_once()` 捕获 `EaAppAutomationError` 后只使用 `error.reason_code`，异常正文被整条丢弃，驱动也没有保存任何截图。所以一天的真机运行只产出了重复的 `EA_UI_UNKNOWN`，无法区分究竟是哪一道门失败。这一点已经在代码里修掉（见下文“2026-08-01 晚间改动”）。

关于“卡在邮箱页到密码页”的判断需要修正，详见[当前阻塞点](#当前阻塞点)：按代码里的超时值推算，约 75 秒的失败时长与密码页等待（15 秒后即抛错）对不上，更符合**密码提交之后的身份等待循环超时**（60 秒）。在新日志跑出来之前，两种可能都应保留，不应继续只按“没进密码页”来改代码。

截至本文记录时：

- 已推送提交：`ec8795d Require EA password page transition evidence`
- 未推送改动：EA 登录证据链、页面判定重写、一次性 `ea-login-check`（本地已通过单元测试，**尚未在 Windows 真机运行**）
- Windows 测试 Runner 已设置 `automation_hold=1`
- 暂停原因：`DEV_VALIDATION_STOPPED`
- 当前没有活动租约
- 本轮测试占用的租约均已关闭，没有遗留账号占用

## 当前能力状态

| 环节 | 状态 | 本轮证据与说明 |
| --- | --- | --- |
| Windows Runner 连接 Mac 上的 `gpt_forge` | 已验证 | 健康检查和设备鉴权正常，Windows 可以访问服务端接口 |
| 自动取号和获取登录凭据 | 已验证 | 真实领取测试账号成功；登录标识由服务端按 EA 邮箱下发，字段结构正常 |
| 租约续期和事件审计 | 已验证 | `EA_SIGNING_IN` 期间续租正常，服务端能看到领取、续租、失败和清理事件 |
| EA 顶层窗口发现和登录前检查 | 已验证 | 可以发现 EA App 窗口并识别登录界面；窗口移动不影响基于窗口范围和 OCR 的定位原则 |
| EA 自动填写账号和密码 | 部分验证 | 之前的实测曾完成登录并识别到预期 EA ID；最新回归无法稳定通过邮箱页到密码页的转换 |
| EA 身份核对 | 部分验证 | 之前曾匹配稳定 EA ID；最新运行未能重新到达该步骤 |
| 自动启动 Apex | 部分验证 | 较早的一次实测看到 Apex 进程启动；最新代码未重新完成验证 |
| Apex 大厅识别、等级/XP 解析 | 未验证 | 托管账号闭环尚未到达稳定大厅，不能确认当前实机解析结果 |
| `LOBBY_PROGRESS` / `RUN_FINISHED` 远程上报 | 未验证 | 协议和代码已接线，但本轮没有成功游戏流程作为真实上报证据 |
| 成功退出、EA 登出、释放租约 | 未验证 | 成功路径尚未走通；失败路径已验证 |
| 失败时停止 Apex、EA 登出、关闭租约 | 已验证 | 最近失败租约均以 `FAILED / EA_UI_UNKNOWN` 收口，并记录清理完成 |
| 自动切换到下一个账号 | 未验证 | 还没有完成两个账号连续运行的实机验收 |

## 本轮真机验证经过

Windows 运行 `account-cycle.cmd` 后，已经真实走通以下链路：

1. Runner 连接服务端并领取一个可用账号；
2. 服务端创建租约并下发一次性登录凭据；
3. Runner 进入 `EA_SIGNING_IN` 并持续续租；
4. EA App 被拉起，程序发现登录窗口并尝试提交登录标识；
5. 无法取得可靠的密码页转换证据；
6. 超时后以 `EA_UI_UNKNOWN` 上报失败；
7. Runner 停止可能残留的 Apex 进程、退出 EA 登录并关闭租约。

最近连续多次运行都在同一位置失败。之所以短时间重复领取，是因为 `account-cycle` 本身是持续循环：一次失败并完成清理后，它会自动领取下一个账号。本轮没有及时暂停 Runner，导致相同问题被重复测试。这是测试流程控制失误，不是已经取得更多闭环进展。

## 已修复并推送的 EA 自动化问题

本轮已经陆续修复并推送：

- Python 3.13 下 Windows `SendInput` 指针兼容问题；
- 输入账号和密码前清空原字段；
- 密码页提交坐标错误；
- Windows 运行锁未正确释放；
- 过期 EA 登录会话恢复；
- 登录页面切换时重新发现 EA 窗口和重试预检；
- 程序从已经停留的密码页继续登录；
- 通过 OCR 查找 EA App 中的 Apex 和启动按钮；
- EA 主界面和身份识别遇到空 OCR 帧时重试；
- 身份核对完成后再次寻找可用的 Apex 启动控件；
- 修复把登录页底部 `Forgot your password` 误认为密码页的严重误判。

最后一项对应最新提交 `ec8795d`：现在只有同时看到密码字段、且账号/邮箱字段已经消失，才允许输入密码。这避免了把密码输入账号框，但也暴露出当前还缺少稳定的页面转换判定。

## 当前阻塞点

### 失败时长与代码超时对不上

改动前的 `sign_in()` 只有两处长等待：

- `_wait_for_password_page()`：15 秒，超时立即抛出“提交账号后未出现密码页”；
- 登录后的身份等待循环：60 秒，超时抛出“登录后未在时限内出现可验证身份”。

两条路径抛的都是 `EaAppAutomationError`，`reason_code` 都是 `EA_UI_UNKNOWN`，而异常正文被 `run_once()` 丢弃，所以控制台只能看到同一个代码。按超时值推算：走密码页失败大约在 20 到 25 秒内结束，走身份等待失败在 70 到 80 秒之间。观察到的约 75 秒更接近后者，也就是说**邮箱页大概率已经过去了，卡的是密码提交之后没能读出身份**。

因此候选原因重排为：

1. 密码提交后 EA 没有登录成功，或者登录了但身份读取失败（身份只从窗口右上角一小块 OCR，且要求 8 到 20 位字母数字，窗口宽度、昵称长度和字体都会影响成败）；
2. `NEXT` 或密码提交点击没有真正触发（改动前全部是窗口比例盲点击）；
3. 密码页已经出现但仍残留账号字段文字，被 `ec8795d` 的严格判定挡住。

第 3 种在改动前还有一个更危险的分支：`sign_in()` 里判断“是否已经在密码页”用的仍是宽松的 `password in compact`，与 `_wait_for_password_page()` 的严格判定不一致。只要页面上出现过 `Forgot your password`，就会跳过邮箱输入，直接把密码打进账号框。这一处不一致已经修掉。

### 次要风险

写入 `windows/runs/account-cycle-status.json` 时出现过一次 `WinError 5`（`os.replace` 原子替换失败）。尚未稳定复现，通常来自杀毒或文件被占用，暂时记录为次要风险，不能与 EA 登录阻塞混为一谈。

## 2026-08-01 晚间改动

全部改动都在本地通过单元测试，**都还没有在 Windows 真机上跑过**。

### 1. 失败终于说得出话

`run_once()` 现在会在收口前打印 `reason_code`、当前 `workflowPhase` 和异常正文，未预期异常也会打印类型和消息。同一个 `EA_UI_UNKNOWN` 现在能区分是哪一道门。

### 2. 页面判定拆成可测的纯函数

新增 `windows/apex_automation/ea_pages.py`：页面分类、登录错误、身份匹配和脱敏都不再和 ctypes 绑在一起，Mac 上也能测。`tests/windows/test_ea_pages.py` 用真实页面文字覆盖了这次踩过的坑，包括 `Forgot your password` 不能算密码页、密码页仍显示邮箱时必须能识别、转换中同时出现两个字段时保持等待。

判定规则的取舍：账号字段仍然优先于密码字段。宁可多等，也不能把密码打进账号框——那等于把密码当账号提交给 EA。同时超时消息会说明卡在哪一种状态（`STILL_ON_ACCOUNT_PAGE` / `ACCOUNT_LABEL_STILL_VISIBLE` / `NO_PASSWORD_FIELD`），一次运行就能把候选 2 和候选 3 分开。

### 3. 一帧回答一个问题

驱动新增 `_observe()`：一次截图、一次窗口内 OCR，页面判定、控件定位和证据都来自同一帧。改动前 `_state()` 和随后的文本检查各自重新截图，页面转换期间两次读到的根本不是同一屏。

### 4. 点击不再纯靠比例

提交优先按 Enter（不需要任何坐标），其次点 OCR 命中的按钮，最后才回落到原来的窗口比例。输入框同样先用 OCR 锚点。密码框锚点显式排除 `Forgot your password`，避免点到重置密码链接。每次点击用的是锚点还是比例都会写进证据。

### 5. 身份核对放宽到整窗

`verify_identity()` 现在先在整个窗口里找租约给的稳定 EA ID（含 0/o、1/l、5/s、8/b 这类 OCR 混淆折叠），找不到再回落到右上角那块。只有在整窗都找不到预期 ID、且右上角连续三次读出别的 ID 时才判定为身份不一致。折叠只做等价字符归一，不做编辑距离，避免把别的账号认成目标账号。

### 6. 每次登录自己留证

新增 `windows/apex_automation/ea_evidence.py`。每次 `sign_in()` 开一个新目录 `windows/runs/ea-login/<时间戳>/`，写入 `steps.jsonl` 和脱敏截图，只保留最近 20 次。记录内容包括每一步的页面判定、命中的 UI 标记、点击定位方式、账号是否在页面上回显、以及脱敏后的身份片段。

脱敏是强制的：邮箱、5 位以上数字和本次登录标识在写盘前会被涂黑，敏感键名带字符串值直接拒绝写入。证据写入失败只会打印一行提示，永远不会中断登录。

### 7. 失败代码更准

新增 `LOGIN_INVALID`（EA 明确报登录信息有误）和 `APEX_START_FAILED`（EA 里找不到 Apex 入口或 Play 按钮），都在设计文档已有的枚举内。

## 下一步最小验证方案

### 1. 保持循环暂停

在登录问题定位完成前保持 Runner 的 `automation_hold=1`，防止持续领取真实账号。

### 2. 用一次性 EA 登录检查取证（已实现）

入口：`windows\ea-login-check.cmd`，等价命令 `.\run.ps1 ea-login-check`。

它只领一个租约、只验证登录、不启动 Apex，无论成功失败都关闭租约后退出，并留下上一节描述的证据目录。领号前先做只读 preflight，preflight 失败就不会调用 `claim()`。清理无法确认时不会关闭租约，而是打印需要人工在运营页安全释放的租约号。

跑完先看 `windows/runs/ea-login/<时间戳>/steps.jsonl`，按顺序回答：

1. `signin-start` 的 `page` 是不是 `EMAIL`；
2. `account-typed` 的 `identifierEchoed` 是不是 `true`——`false` 说明点击没落进输入框；
3. `account-submitted` 的 `page`：到了 `PASSWORD` 说明邮箱页这一段没问题；仍是 `EMAIL` 就看 `submitTarget` 和失败消息里的 blocker；
4. 有没有 `password-typed`，以及之后是 `signed-in` 还是 `signin-timeout`；
5. `signin-timeout` 的“最后页面”字段是这一轮最关键的信息。

日志和截图不得包含邮箱全文、密码、Token、OTP 或 TOTP Secret；这条约束已经写进 `EaLoginEvidence`，不是靠人工遵守。

### 3. 分阶段放行

验收顺序应固定为：

1. 邮箱页提交后可靠进入密码页；
2. 连续两次一次性登录成功并匹配 EA ID；
3. 单次托管运行成功启动 Apex；
4. 识别大厅并连续取得稳定等级/XP；
5. 服务端收到 `LOBBY_PROGRESS` 和 `RUN_FINISHED`；
6. 按 F8 后退出 Apex、登出 EA 并正确释放租约；
7. 最后再验证两个账号连续自动切换。

任何一步失败，都应保留该步证据并停止，不自动进入下一账号。

## 完整闭环的完成标准

只有以下实机证据全部出现，才可以把自动切号闭环标记为完成：

- 自动领取账号和租约；
- EA 自动登录并核对当前 EA ID；
- 自动启动 Apex；
- 进入大厅并解析出可信的等级/XP；
- 服务端收到运行开始、大厅进度和运行结束事件；
- 完成一局或按既定条件返回大厅；
- 正常停止 Apex、退出 EA、关闭或释放租约；
- 自动领取第二个账号并重复上述流程。

单元测试通过、代码已经接线、曾经启动过 Apex，或者失败路径可以清理，都不能单独替代这组实机证据。

## 参考资料

### 项目文档

- [当前游戏循环状态](current-status.md)：已有 `play.cmd` 游戏内循环证据；它不是本次托管账号闭环的完成证明。
- [EA 账号编排设计](ea-account-orchestrator-design.md)：账号领取、租约、状态机、检查点和切号方案的设计及实现说明。
- [EA 服务端到 Windows 端到端验证手册](ea-server-windows-end-to-end-validation.md)：原始验收步骤。部分“尚未实现”的描述已经过时，应以本文和当前代码为准。
- [远程上报接口](remote-reporting-api.md)：`RUN_STARTED`、`LOBBY_PROGRESS`、`RUN_FINISHED`、租约围栏和幂等约束。
- [远程上报 OpenAPI](remote-reporting-openapi.yaml)：接口字段和事件枚举的机器可读定义。
- [Windows 首局 Runner](windows-first-match-runner.md)：Windows 游戏启动、首局运行及停止边界。
- [OCR 优先校准说明](ocr-first-calibration.md)：UI 识别、置信度和证据保留原则。

### 运行入口和本地证据

- `windows/account-cycle.cmd`：托管账号循环入口，会一直领下一个账号，不适合用来调登录。
- `windows/ea-login-check.cmd`：一次性登录验证入口，只领一个租约、不启动 Apex。
- `windows/run.ps1`：Windows 命令分发入口，包含 `account-cycle`、`account-cycle-check`、`ea-preflight` 和 `ea-login-check`。
- `windows/apex_automation/account_orchestrator.py`：领取账号、EA 登录、游戏会话和租约收口的主编排。
- `windows/apex_automation/ea_app_win32.py`：Win32 加 OCR 的 EA 混合驱动。
- `windows/apex_automation/ea_pages.py`：页面判定、身份匹配和标记提取的纯逻辑。
- `windows/apex_automation/ea_evidence.py`：登录证据目录、脱敏规则和轮转清理。
- `windows/runs/ea-login/<时间戳>/`：每次登录的 `steps.jsonl` 与脱敏截图，只保留最近 20 次。
- `windows/apex_automation/orchestration_state.py`：工作流阶段、检查点和完成记录。
- `windows/runs/account-cycle-status.json`：当前检查点；文件可能包含运行标识，外发前应检查脱敏。
- `windows/runs/account-cycle-completed.jsonl`：本地完成历史；外发前同样需要检查脱敏。

### 本轮关键提交

- `ec8795d`：要求真实的密码页转换证据，修复密码误填到账号框的问题。
- `aee3c65`：身份核对后重试 EA 中的 Apex 启动控件。
- `436a0f0`：稳定 EA 主窗口 OCR 页面转换。
- `501e4fa`：支持从已经打开的密码页恢复登录。
- `3e4acb3`：等待确认密码页后再输入密码。
- `2aa18d0`：通过 OCR 标签在 EA App 中启动 Apex。
- `8e09155`、`91f5a99`：页面切换时重试窗口发现和预检。
- `22bfb9f`：恢复过期 EA 登录会话。
- `a436ec8`：Apex 启动前失败时关闭租约。
- `157fef0`：修复密码提交和 Windows 运行锁。
- `af5fc96`：填写凭据前清空 EA 输入框。

