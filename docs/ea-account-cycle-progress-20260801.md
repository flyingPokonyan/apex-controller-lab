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
| EA 自动填写账号和密码 | 已验证 | 20:53 实测：邮箱页到密码页、密码提交全部通过，两次提交都由回车触发，输入框由 OCR 锚点定位，未用到比例兜底。证据见 `windows/runs/ea-login/20260801-205258/` |
| EA 身份核对 | 已验证 | 21:12 实测：登录后 16 秒读到徽标 `f4vb...gvlb`（置信度 1.000）并与租约下发的稳定 EA ID 匹配。证据见 `windows/runs/ea-login/20260801-211215/` |
| EA 新设备验证（验证器） | 已验证 | 23:03 实测：识别「Verify your identity」选择页 → 选 App Authenticator → 落到验证器码页 → 取 TOTP 填入 → 登录成功，全程 54 秒。邮箱验证码尚不支持，服务端只能生成 TOTP。证据见 `windows/runs/ea-login/20260801-230321/` |
| EA 登出并释放租约 | 已验证 | 21:23 和 21:24 连续两次自动登出，`trigger=chevron`，均以 `signed-out / page=EMAIL` 收尾，租约自动关闭。证据见 `windows/runs/ea-login/20260801-212313/` 和 `.../20260801-212412/` |
| 自动启动 Apex | 已验证 | 23:08 托管运行：EA 内定位 Apex 入口并启动，进标题页后自动点“继续”、关闭欢迎公告 |
| Apex 大厅识别、等级/XP 解析 | 已验证 | 大厅解析 `level=9, xp=7.87K/8.15K`，等级置信度 0.99，`readStatus=OK` |
| `LOBBY_PROGRESS` / `RUN_FINISHED` 远程上报 | 已验证 | 服务端 `accepted_through=110`，`LOBBY_PROGRESS` 载荷完整；`RUN_FINISHED` 以 `STOPPED` 收尾 |
| 完整对局循环 | 已验证 | `roundsStarted=1, roundsReturnedToLobby=1`：选模式→匹配→跳伞→对局→阵亡观战→回大厅→自动开下一局 |
| 成功退出、EA 登出、释放租约 | 已验证 | F8 后停 Apex、EA 登出、租约 `RELEASED / OPERATOR_STOPPED`，服务端 `completed_at` 已写入 |
| 租约续期（长跑） | 已验证 | 17 分钟运行期间按 `renewAfter` 每 2 分钟续租，无中断 |
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

## 20:53 实测结论（已用证据取代推测）

第一次带证据的运行结束了猜测阶段：

**原始故障已确认。** `02-account-typed.png` 显示 EA 邮箱页底部就印着 `Forgot your password, or need to create a new one?`。旧代码判断“是否已经在密码页”用的正是 `password in compact`，所以它**从来没有输入过邮箱**，直接把密码打进邮箱框，再点 `(0.50, 0.58)`——那个位置是 `Sign in as invisible` 复选框——什么都没提交，然后干等 60 秒。这就是那 75 秒的真身。文档此前记录的“邮箱页到密码页转换失败”是错误结论。

**登录链路本身是通的。** `steps.jsonl` 显示 `signin-start → account-typed(anchor) → account-submitted(enter, PASSWORD) → password-typed(anchor)`，10 秒走完。两次提交都是回车生效，两次输入框都是 OCR 锚点命中，比例兜底一次都没用上。

顺带证伪了另一个方向：登录页的比例坐标其实全是对的（NEXT 在 0.695、SIGN IN 在 0.577），之前反复调坐标是白费。

**新的阻塞点在登录成功之后。** EA 登录成功的瞬间会销毁 520×867 的登录窗口、换成主窗口，驱动全程持有的旧句柄失效，`GetWindowRect` 抛出“无法读取 EA App 窗口边界”。连带暴露 `ea-login-check` 的收口写在 `with DxcamFrameSource(...)` 外面，清理时截图服务已关闭，异常冲出 `finally`，**租约没有关闭**，下一次运行被“已有活动租约”挡住。两处都已修：

- 句柄失效时自动重新发现 EA 窗口（`_live()`），`_observe()` 在窗口边界读取失败时丢弃句柄重试；
- 收口移进捕获上下文内部，清理异常一律吞掉并打印，绝不阻断关闭租约；
- 输入后加入 0.8 秒稳定期——`identifierEchoed=false` 是假阴性，截图源返回了打字前的旧帧；
- 登出改为点击刚读到的身份徽标文字，而不是固定的 `(0.89, 0.075)`。

## 历史推理记录（已被上面的实测取代）

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

验收顺序固定为：

1. ✅ 邮箱页提交后可靠进入密码页——21:12、21:23、21:24 三次全部通过，两次提交都由回车触发；
2. ✅ 连续两次一次性登录成功并匹配 EA ID——21:23 和 21:24，各约 45 秒，含自动登出和自动关闭租约；
3. ✅ 单次托管运行成功启动 Apex——`20260801-230808-48fa12db`；
4. ✅ 识别大厅并取得稳定等级/XP——`level=9`，置信度 0.99；
5. ✅ 服务端收到 `LOBBY_PROGRESS` 和 `RUN_FINISHED`——110 个事件全部接收；
6. ✅ 按 F8 后退出 Apex、登出 EA 并正确释放租约——`RELEASED / OPERATOR_STOPPED`；
7. 最后再验证两个账号连续自动切换——**唯一剩下的一层**。

第 1、2 步已收口。注意两次登录领到的是同一个账号（`RELEASED` 不加冷却，放回池子后立即可再领），所以“换号”仍未验证，那是第 7 步的事。

新设备验证只在“这台机器没验过的账号”第一次登录时出现，验证页勾着 `Remember this device`，所以是每个账号一次性的成本。当前只支持验证器；只有邮箱验证码的账号仍然过不去，需要在 `gpt_forge` 的 `otp()` 里补上读邮件取码（邮箱凭据和 Outlook/IMAP 处理器都是现成的，注册流程已经在用）。

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

- `windows/account-cycle.cmd`：托管账号循环入口，会一直领下一个账号，不适合用来分阶段验收。
- `windows/account-cycle-once.cmd`：只跑一个账号一轮就退出，用于验收第 3 到第 6 步。
- `windows/ea-login-check.cmd`：一次性登录验证入口，只领一个租约、不启动 Apex。
- `windows/ea-preflight.cmd`：只读预检，不领号、不输入凭据。
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

