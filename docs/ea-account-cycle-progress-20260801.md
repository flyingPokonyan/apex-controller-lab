# EA 自动切号闭环进度（2026-08-01）

## 一句话状态

**七步验收全部通过，托管切号闭环打通。**

- `20260801-230808-48fa12db`：一个账号 17 分钟连续跑完 EA 登录 → 新设备验证 → 核对 EA ID →
  启动 Apex → 大厅读出等级 → 上报服务端 → 打完一局回大厅 → 自动开下一局 → F8 干净收口。
  服务端接收 110 个事件。
- `20260801-234225-f4119100`：账号 A 进大厅读到 `level=10 >= target_level=10`，直接判达标，
  上报 `RUN_FINISHED(TARGET_REACHED)`，租约 `COMPLETED / TARGET_REACHED /
  TARGET_LEVEL_CONFIRMED`，profile 的 `stage` 自动从 `leveling` 变成 `ready_for_sale` 退出账号池。
- `20260801-234504-f3a44c2f`：循环随即领取账号 B（`acct_e1d7a346…`，fence=2），重新登录、
  启动 Apex、读出 `level=8`、进入匹配。**A 收口到 B 开打间隔 80 秒，全自动。**

剩下的都不是闭环问题，见[已知没做的](#已知没做的)：长跑稳定性、只有邮箱验证码的账号、
以及若干识别不出的画面。

## 复现这一步的方法

第 7 步（自然达标 + 自动换号）的验证方式，留档备用：

### 前置

1. 服务端放开**两个**测试账号 A、B，其余保持暂停；Runner 未暂停
2. 设备当前没有活动租约（运营页「任务与租约」确认）
3. EA App 停在登录页
4. Windows 上先 `windows\update.cmd`

### 步骤

把 A 的目标等级改成它当前等级，让它一进大厅就算达标，不用真打到 20 级：

```sql
-- 库在 /Users/jie/Desktop/Gpt_register/gpt_forge/backend/data/gptforge.db
-- 挑一个服务端已记录 current_level 且 level_trust=trusted 的账号
UPDATE apex_profiles SET target_level = <该账号的 current_level>
WHERE public_id = '<accountId>';
```

2026-08-01 那次用的是 `acct_1febebef1cca4119bcd646c66d796472`（`current_level=10`），
它现在已经是 `ready_for_sale`，不能再用来复现。

`TargetLevelPolicy` 在安全大厅里读到 `level >= target_level` 就直接判 `TARGET_REACHED`，
不需要打完一局（`progression_policy.py:111`）。`eligibility()` 不检查等级是否已达标，
所以改完这个号照样能被领取。

然后双击 **`windows\account-cycle.cmd`**（这一步要的就是循环，不是 `--once`）。

### 判据

按顺序看到这些才算过：

1. A 登录、启动 Apex、进大厅
2. 服务端收到 `LOBBY_PROGRESS`，其中 `level >= target_level`
3. 收到 `RUN_FINISHED`，`status=TARGET_REACHED`
4. A 的租约 `outcome=TARGET_REACHED`，profile 的 `stage` 从 `leveling` 变成 `ready_for_sale`
5. **循环自动领取 B**，B 重新走一遍登录到大厅
6. 全程没有 `COMPLETION_PENDING`、`EXPIRED_UNCONFIRMED` 或孤儿租约

第 5 步出现就是第 7 步通过。B 那一轮看到进大厅即可按 F8 收工。

### 代价

A 达标后 `stage` 变 `ready_for_sale`，而 `STAGE_TRANSITIONS` 里这个转换**单向不可逆**
（`ready_for_sale` 只能去 `sold`），运营页改不回 `leveling`。要复用这个测试账号只能直接改库。

## 各入口分别干什么

| 文件 | 作用 | 什么时候用 |
| --- | --- | --- |
| `windows\ea-preflight.cmd` | 只读识别 EA 窗口和当前页面 | 怀疑识别有问题，不领号 |
| `windows\ea-login-check.cmd` | 领 1 个号只验登录+登出，不启动 Apex | 只调登录时 |
| `windows\account-cycle-once.cmd` | 领 1 个号完整跑一轮，跑完退出 | 分阶段验收 |
| `windows\account-cycle-resume.cmd` | 同上，但先清掉本地人工暂停状态 | 上一轮结果是 `PAUSED` 时 |
| `windows\account-cycle.cmd` | 真循环，一个号收口后立刻领下一个 | 验收全过之后，以及验证换号 |
| `windows\update.cmd` | `git pull --ff-only` + 同步依赖 | 每次拉新代码 |

平时用 `once`，卡在 `PAUSED` 用 `resume`，验换号才用 `account-cycle`。

## 出问题时去哪找证据

**登录段**：`windows\runs\ea-login\<时间戳>\`
- `steps.jsonl` 每步一行：页面判定、命中的 UI 标记、点击用的是 OCR 锚点还是比例兜底
- 同名 PNG 是那一步的窗口截图，邮箱和验证码已涂黑
- 遇到没见过的页面会立刻拍一张 `awaiting-<页面>.png`，不用等超时

**游戏段**：`windows\runs\<runId>\`
- `events.jsonl` 全部事件，`pilot-summary.json` 计数器汇总
- `result.json` 结果与停止原因
- `screenshots\*-unknown.png` 识别不出的画面

**服务端**（Mac 上的 `gpt_forge`，库路径见上）：

```sql
SELECT state, current_phase, observed_state, accepted_through, result, last_error_code
FROM apex_runs WHERE client_run_id = '<runId>';

SELECT type, COUNT(*) FROM apex_run_events e JOIN apex_runs r ON r.id = e.run_id
WHERE r.client_run_id = '<runId>' GROUP BY type;
```

失败时控制台会打印 `reason_code`、当前 `workflowPhase` 和异常正文，三者一起看。

## 挂机运行的环境要求

捕获走 DXGI 桌面复制，输入走 `SendInput`，两者都要求桌面处于正常渲染状态：

- **关显示器电源要看接口**：DisplayPort 关机会直接断开链路，GPU 认为显示器被移除，
  桌面复制立刻 `DXGI_ERROR_ACCESS_LOST`（HRESULT `0x887A0026`），实测就是这样炸的；
  HDMI 通常保留热插拔检测，关机没事。**接 DP 的机器等同拔线，别关。**
- **拔线或 KVM 切走不行** —— 同上，而且分辨率可能变化，
  配置里的 2560×1440 区域会全部错位。
- **假负载要当成唯一的显示输出用，插上并不等于配好**。它只是多加了一个输出：
  真显示器还在桌面上时，抓的仍是原来那个输出，把它关掉照样是拓扑变化 + access
  loss，dxcam 也不会自己挪到假负载上去（见下条）。正确做法是在「显示设置」里选
  **「仅在 <假负载> 上显示」**，并确认它能设成 **2560×1440**（便宜的假负载 EDID
  里常常只有 1080p/4K，设不出 1440p 就换一个或改 EDID），然后再关真显示器。
  这样跑起来之后拓扑不再变，索引也不会漂。
- 捕获层现在能扛住 access loss：丢掉失效的 camera（不能 release，
  释放已失效的 duplicator 会让进程崩）、重建、重试。分辨率切换、驱动更新、
  锁屏、全屏切换都会产生 access loss，长跑必然遇到
- **dxcam 0.3.0 自己的恢复是无限重试，而且不换输出**。
  `DisplayRecoveryHandler.handle` 是个 `while True`，最多 5 秒一轮，永不放弃；
  它只在 DXGI *抛异常* 时才重新枚举输出，而一个"已经不在桌面上"的显示器给的是
  `RuntimeError("Output is not attached to desktop.")`，被它当成又一次瞬时失败。
  于是 `grab()` 再也不返回，运行就停在两行英文 warning 之后，看起来像无缘无故不动了。
  `capture.py` 现在给这个循环加了 20 秒上限（`CaptureRecoveryTimeout`，故意不继承
  `RuntimeError`，否则会被那个循环吞掉），超时后由我们自己去还活着的输出上重建。
- **dxcam 的日志现在接进控制台**（`[dxcam] …`）。它恢复过程中说的话全是 `INFO`，
  本项目又没配过 logging，所以以前只有开场那两行 warning 能看见，
  恢复成功还是卡死完全分不出来。捕获出问题时还会打一行当前显示器列表。
- **锁屏、屏保、睡眠会停但不会崩** —— 前台守卫发现前台不是 Apex，
  `_pause_for_foreground()` 释放按键并暂停发送，等 Apex 回到前台才继续
- **RDP 会顶掉控制台会话**，断开后桌面停止渲染、捕获全黑。要远程盯就用不接管会话的方案，
  或者连上后别断开

挂机前把「电源和睡眠」的屏幕与睡眠都设为从不，登录选项的「需要登录」设为从不，关掉屏保。

## 已知没做的

- **邮箱验证码**：服务端 `otp()` 只会用 `totp_secret` 现算 TOTP，拿不到邮箱验证码。
  只配了邮箱、没配验证器的账号目前过不去，客户端会以 `OTP_TIMEOUT` 干净失败。
  邮箱凭据和 Outlook/IMAP 处理器在 `gpt_forge` 里是现成的（注册流程已经在读 EA 验证码邮件），
  需要的是在租约 API 上补一个接口，并且**只接受 `challengeStartedAt` 之后收到的邮件**。
  客户端协议不用改：`OtpChallenge` 那套本来就是为这个设计的。
- ~~两个账号连续切换~~：已于 2026-08-01 23:45 验证通过。
- **长时间稳定性**：目前最长一次运行 17 分钟。打到 20 级要几小时，期间会遇到 Apex 更新、
  EA 弹窗、网络抖动、匹配失败等还没碰过的情况。
- **未知画面**（本轮 6 张）：都是启动闪屏或被提示浮层遮住的局内画面，程序的反应是等待，
  1876 帧里只有 6 次且每次都自行恢复。属于调稳，不阻塞。
- **服务端可选优化**：`generate_totp` 会把只剩 1 秒的码也发出来。建议剩余不足 10 秒时直接
  返回下一个窗口的码。客户端现在会自己等，能用。
- **Windows 时钟**：`w32tm` 没有配置时间源。客户端已对时钟偏差留了 120 秒容差，但
  `LeaseKeeper` 判断租约是否过期时仍拿本地时钟比服务端时间，偏差过大会误判提前过期。

## 今天修掉的问题

按发现顺序，每一条都是实机跑出来的：

1. **失败没有正文**——`run_once()` 只用 `reason_code`，异常消息整条丢弃，两种完全不同的失败
   打印出来一模一样。这是"一天测下来没有进展"的真正原因，不是某个 UI 判定。
2. **密码打进账号框**——EA 邮箱页底部就印着 `Forgot your password`，而旧代码用
   `password in compact` 判断"是否已在密码页"，于是从来没输入过邮箱，直接把密码打进邮箱框，
   再点一个空白处，然后干等 60 秒。那 75 秒失败的真身。
3. **登录成功瞬间窗口失效**——EA 登录成功会销毁 520×867 的登录窗口换成主窗口，
   驱动全程握着旧句柄，`GetWindowRect` 直接抛错。现在句柄失效会自动重新发现窗口。
4. **收口写在捕获上下文外面**——`ea-login-check` 清理时截图服务已关闭，异常冲出 `finally`，
   租约没关闭。已移进上下文内部，清理异常一律吞掉并打印。
5. **两套 `INPUT` 结构**——`input_win32` 和 `ea_app_win32` 各自定义了结构相同的 `INPUT`，
   而 `ctypes.windll.user32` 是全进程单例，后构造的把 `SendInput.argtypes` 覆盖掉，
   pilot 第一次发输入就 `expected LP_INPUT instance instead of LP_INPUT`。
   `play.cmd` 不加载 EA 驱动，所以这个雷只在两个流程合体时才响。
6. **"Friends 0/2" 被当成账号 ID**——身份识别区下沿把好友栏框了进去，OCR normalize 后是
   九位字母数字。识别区已收窄并拒绝界面词。
7. **验证码跨时钟比较**——`received_at >= challenge_started_at` 一边是服务端时钟一边是
   Windows 时钟，快几秒就必然失败；`current < expires_at` 同样跨时钟，而 TOTP 的
   `expires_in` 是当前 30 秒窗口的剩余时间，可能只剩 1 秒。现在只用服务端自己的两个时间戳
   算寿命，不够用就等下一个窗口。
8. **验证页上登不出**——`sign_out` 只把纯登录页当作已登出，停在验证码页时转 8 圈后返回
   False，清理证据不完整，租约关不掉。停在登录流程里的任何页面都意味着没有会话。
9. **账号菜单点不开**——点的是徽标名字文本，真正的入口是名字右侧的 ⌄（Log out）或左上角
   三个杠（Sign out）。两个都试，逐个验证。实测 chevron 和 hamburger 各成功过一次，
   两个入口都有用。

## 今天新增的能力

- `windows\apex_automation\ea_pages.py`：页面判定、身份匹配、脱敏规则，纯函数，Mac 上可测
- `windows\apex_automation\ea_evidence.py`：每次登录一个目录，脱敏截图 + `steps.jsonl`，保留最近 20 次
- `ea-login-check` / `account-cycle --once` / `--resume`：有边界的验收入口，不会连续烧号
- EA 新设备验证：识别选择页、选验证器、取 TOTP、填码，最多 3 次
- 失败代码更准：新增 `LOGIN_INVALID`、`APEX_START_FAILED`、`OTP_TIMEOUT`

## 参考资料

- [EA 账号编排设计](ea-account-orchestrator-design.md)：状态机、检查点、`reasonCode` 枚举
- [EA 服务端到 Windows 端到端验证手册](ea-server-windows-end-to-end-validation.md)：原始验收步骤
- [远程上报接口](remote-reporting-api.md) / [OpenAPI](remote-reporting-openapi.yaml)
- [Windows 首局 Runner](windows-first-match-runner.md)、[OCR 优先校准](ocr-first-calibration.md)

### 今天的提交

```
63273ed 保存 EA 登录证据并说清失败原因
85324f1 一次性 EA 登录检查，用完把账号还回去
cdad609 挺过登录成功时被销毁的窗口
f377ca4 EA 已登录时拒绝跑登录检查
8d62168 从真正能打开账号菜单的控件登出
3757954 单次托管运行，验收期间不进循环
681d8ca 允许人工清除暂停状态
a42c07a 与游戏输入共用同一套 INPUT 结构
5679e8d 等身份期间把 EA 显示的任何页面都拍下来
a0e3b80 用验证器回答 EA 的身份验证
7f121d8 不再把 Provider 给的验证码全判成过期
```
