# EA 自动切号真实服务器闭环验证手册

本文用于在 Windows 已安装 EA App 和 Apex 后，由新的开发会话继续实现并验证：真实服务器
领号、EA 登录、身份核验、启动 Apex、游戏内循环、20 级停止、退出登出、租约完成和下一号。

本文不使用本地 Mock。账号、凭据、OTP、租约和上报全部来自 `gpt_forge` 服务端。

## 1. 当前状态

已经完成并有自动化测试覆盖：

- `play.cmd` 的游戏内循环；
- 大厅等级与经验 OCR、稳定读数和远程上报；
- 20 级同帧阻止“准备”的 `TargetLevelPolicy`；
- HTTP 账号 Provider 的 claim/current/status/credentials/renew/OTP/close；
- fence、幂等、续租、checkpoint、崩溃恢复和完成证据；
- Apex 运营页面、账号领用规则、Runner 配置和租约审计。

尚未完成的是 Windows 真实 `EaAppDriver`。当前 `account-cycle.cmd` 会在领取账号前拒绝运行，
不会误领账号或输入凭据。需要实现的方法位于 `windows/apex_automation/ea_app.py`：

1. `ensure_started()`；
2. `current_identity()`；
3. `sign_in()`；
4. `verify_identity()`；
5. `start_apex()`；
6. `stop_apex()`；
7. `sign_out()`。

## 2. 验证原则

- 先部署和核验服务端，再允许 Runner 领取真实账号。
- 先完成只读 UIA 控件识别，再开放登录输入。
- 测试期间只允许两个指定账号处于“可领用”，其他账号全部人工暂停。
- 密码、Reporter Token、Provider Token、OTP 和 TOTP Secret 不发到聊天，不放截图，
  不写日志、checkpoint 或仓库。
- UIA 选择器优先使用 `AutomationId + ControlType + Pattern`；不使用固定坐标和 OCR 猜按钮。
- 遇到 Captcha、身份不一致、未知 EA 页面或无法确认退出时必须暂停，不继续启动 Apex 或释放账号。

## 3. 服务端部署

服务端仓库为 `gpt_forge`。部署前完成以下检查：

```bash
docker compose -f docker-compose.server.yml config
```

生产环境设置公网 HTTPS Origin：

```bash
export APEX_PUBLIC_BASE_URL="https://你的域名"
docker compose -f docker-compose.server.yml up -d --build
```

部署后确认：

- `GET /api/health` 正常；
- 已替换默认的 JWT、管理员账号和管理员密码；
- `/api/apex/*` 由登录后的运营页面访问；
- `/v1/runner/*` 可通过同一公网 HTTPS 域名访问；
- 代理日志不记录 `Authorization` 和请求正文；
- 数据库卷为持久化存储且已有备份。

服务端部署细节见 `gpt_forge/docs/apex-remote-reporting-server.md`。

## 4. 准备两个真实测试账号

两个完成排位之路的账号需要在 Apex 运营页推进到“升级中（leveling）”。逐项确认：

- 当前等级和最高可信等级均低于目标等级 20；
- 等级信任状态为 `trusted`；
- EA 账号处于 active，稳定 EA ID 已就绪；
- Steam 关联处于 linked，Steam 账号处于 active；
- 登录凭据和 2FA 状态为 ready；
- 没有登录冷却、账号隔离、人工暂停或占用型租约；
- 页面显示“可领用”。

真实领号会按 `automation_priority`、当前等级和阶段更新时间排序，并不是按页面点击的账号领取。
因此在测试前：

1. 暂停其他所有可领用账号，暂停原因填写“EA UIA 闭环测试隔离”；
2. 第一阶段只开放测试账号 A；
3. 单账号生命周期通过后，再开放测试账号 B；
4. 双账号连续切换时，确保只有 A、B 两个账号可领用。

不要直接修改数据库绕过 eligibility。运营页显示的不可领用原因必须全部处理完。

## 5. 创建测试 Runner

在 Apex 运营页的 Runner 标签中：

1. 创建一个专用测试 Runner，例如 `Apex Windows UIA Test`；
2. 页面会一次性签发 Reporter Token 和 Provider Token；
3. 下载得到 `account-cycle.private.json`；
4. 将文件放到 Windows 项目的 `windows\account-cycle.private.json`；
5. 不要把该文件提交到 Git、发到聊天或放进普通日志。

配置中的 `reportUrl` 和 `leaseUrl` 必须是公网 HTTPS 地址。Runner 页面应显示两个 Token
均已创建且设备未撤销、未暂停。

## 6. Windows 基线检查

前提：EA App 和 Apex 已安装，Apex 使用项目当前支持的 `2560x1440`、中文、全屏环境。

在 `windows` 目录执行一次环境安装：

```powershell
.\setup.ps1
```

在不启用自动切号前先运行一次 `play.cmd`，确认原游戏内闭环仍能：

- 进入大厅并清理覆盖页；
- 选择目标模式、取消补满并准备；
- 匹配、跳伞、近战和返回大厅；
- 在大厅产生等级与经验读数；
- F8 能停止并释放所有输入。

保留本次 `windows\runs\<runId>`，不要只保留截图。

## 7. 采集 EA App UIA 证据

截图用于理解页面，UIA 控件树用于实现可靠选择器，两者都需要。每个状态都执行：

1. 保存一张完整、未缩放、未裁剪的 EA App 截图；
2. 双击 `windows\probe-ea-uia.cmd`；
3. 将生成的 `windows\runs\ea-uia-*.txt` 与截图使用相同状态名归档。

需要采集：

| 状态 | 必需 |
|---|---|
| EA 登录页 | 是 |
| EA 登录后的主页 | 是 |
| Apex 游戏详情页，可见启动按钮 | 是 |
| 账号菜单展开，可见登出 | 是 |
| Apex 启动中 | 是 |
| Apex 已运行时的 EA 页面 | 是 |
| Apex 退出后的 EA 页面 | 是 |
| OTP 页面 | 遇到时采集 |
| Captcha 或其他安全验证 | 遇到时采集 |

探针不点击、不输入、不读取 `ValuePattern`，并自动脱敏邮箱和数字验证码。密码、当前 OTP、
Token 和 TOTP Secret 不得出现在截图或交接材料中。

## 8. 新会话实现 EA 驱动

新会话先读取本手册、`ea_app.py`、`account_orchestrator.py` 和全部 UIA 证据，再实现真实
Windows 驱动。实现必须满足：

- 能识别 EA 未启动、登录页、OTP、已登录、未知页；
- 登录前若已有其他账号，先确认 Apex 未运行，再安全登出；
- 只在持有有效租约时获取和填写凭据；
- OTP 只通过 Provider 的 challenge 请求取得，并校验 challenge 与时效；
- 登录后核验租约中的稳定 EA ID，不以昵称或邮箱代替；
- 身份不一致时不启动 Apex，并进入人工暂停；
- 启动 Apex 后用真实进程事实确认启动成功；
- 停止时等待所有 Apex 进程退出，再执行 EA 登出；
- Captcha 进入人工暂停，不尝试绕过；
- 所有异常文本必须脱敏，不能包含密码、Token 或 OTP。

在任何真实 claim 之前，驱动应先完成 EA App 进程、顶层窗口和关键 UIA 控件的只读 preflight。
preflight 失败时必须在调用 Provider `claim()` 前退出。

## 9. 分阶段真实验证

### 9.1 只读 preflight

不领取账号、不输入、不启动 Apex，只验证：

- EA App 进程和窗口可发现；
- 当前 UI 状态识别正确；
- 登录框、启动按钮、账号菜单和登出按钮选择器唯一；
- 未知页面会 fail closed。

### 9.2 单账号登录和身份核验

只开放账号 A，运行 `account-cycle.cmd`。首次真实 claim 后核对：

- 服务器产生 CLAIMED 租约，Runner 和 accountId 正确；
- Windows checkpoint 只包含 leaseId、fence、accountId 和阶段，不含凭据；
- EA 登录成功，OTP 按 challenge 获取；
- 登录后的稳定 EA ID 与租约一致；
- 任一失败都会暂停，不会继续领取账号 B。

若本阶段失败，不要直接在运营页释放租约。先确认 Apex 已退出、EA 已登出，再使用“确认安全
释放”；无法确认时保持 `EXPIRED_UNCONFIRMED` 或隔离账号，保留审计证据。

### 9.3 单账号 Apex 生命周期

仍只开放账号 A，验证：

- EA App 发起 Apex；
- Apex 进程出现并进入大厅；
- Runner 上报 `RUN_STARTED` 和 `LOBBY_PROGRESS`；
- 等级低于 20 时进入原 `play.cmd` 游戏内循环；
- F8/Ctrl+C 会退出 Apex、EA 登出、显式 close 为 RELEASED，并结束程序，不领下一号。

### 9.4 20 级完成

账号 A 稳定读取到 20 级时验证：

- 同一帧不再发送“准备”；
- `LOBBY_PROGRESS` 和 `RUN_FINISHED` 已被服务端连续确认；
- Apex 正常退出，所有相关进程结束；
- EA App 登出成功；
- Runner 显式 close 为 `TARGET_REACHED`；
- 租约进入 COMPLETED；
- 账号阶段从 `leveling` 推进到 `ready_for_sale`；
- `RUN_FINISHED` 本身没有提前释放租约或修改业务阶段。

### 9.5 两账号连续切换

开放 A、B，其他账号保持暂停。运行 `account-cycle.cmd`，验收顺序：

1. A 被领取、登录、游玩到 20、退出并登出；
2. A 的报告和租约完成后才领取 B；
3. B 登录前不存在 A 的 Apex/EA 会话残留；
4. B 的稳定 EA ID 核验成功后才启动 Apex；
5. B 到 20 后同样完成；
6. 服务端无 `COMPLETION_PENDING`、`EXPIRED_UNCONFIRMED` 或孤儿 RUNNING 租约。

## 10. 中断和恢复验证

正常闭环通过后，再分别验证：

- 游戏中强制结束 Runner：重启后不得重打一局，应人工暂停 orphan run；
- close 请求响应丢失：使用相同 operationId 查询/重试，不得重复推进阶段；
- 断网后恢复：本地 outbox 补传并保持 `(deviceId, runId, seq)` 幂等；
- 旧 fence 上报：只能进入审计，不得覆盖当前等级；
- Runner 被暂停或 Token 被轮换：旧 Token 立即失效且不泄露到日志；
- EA 身份不一致：不启动 Apex，账号被暂停或隔离等待人工处理。

## 11. 失败时交接材料

新会话需要以下材料，缺一项容易误判：

- 失败步骤和本地时间；
- 脱敏后的完整屏幕截图；
- 对应状态的 `ea-uia-*.txt`；
- `windows\runs\account-cycle-status.json`；
- 当前 run 目录中的 `manifest.json`、`events.jsonl`、`result.json`、`report-state.json`；
- 运营页租约详情、Runner 状态、run 详情和不可领用原因截图；
- EA App、Apex 相关进程是否仍存在。

不要提供 `account-cycle.private.json`、密码、Token、OTP、TOTP Secret 或未脱敏服务日志。

## 12. 完成标准

只有全部满足才算闭环：

- 连续两个真实账号从服务器领取并完成；
- 每个账号登录后身份可证明且与租约一致；
- Apex 可启动、可正常退出、无残留进程；
- 每次返回大厅都有账号维度的等级/经验记录；
- 20 级不再开始下一局；
- 清理完成后才 close 租约，完成后才领取下一号；
- 两个账号均进入 `ready_for_sale`；
- 无敏感信息落盘或进入上报；
- 中断恢复不会重复游玩、重复完成或错绑账号。

## 13. 新会话交接提示词

```text
请继续完成 Apex 的真实服务器账号自动切换闭环。

先完整阅读：
/Users/jie/Desktop/code/apex-controller-lab/docs/ea-server-windows-end-to-end-validation.md

Runner 仓库：
/Users/jie/Desktop/code/apex-controller-lab

服务端仓库：
/Users/jie/Desktop/Gpt_register/gpt_forge

当前远程上报、真实 HTTP 租约 Provider、20 级策略、checkpoint 和运营页面已经实现。
不要实现本地 Mock，不要改用 Steam，不要使用固定坐标或 OCR 猜 EA App 按钮。

请先查看我提供的 EA App 截图和 ea-uia-*.txt，基于 UI Automation 实现真实
EaAppDriver。任何真实 claim 前先完成只读 preflight。然后严格按文档顺序做：只读
preflight、单账号登录核验、单账号 Apex 生命周期、20 级完成、双账号连续切换。

不得要求我发送密码、Reporter/Provider Token、OTP 或 TOTP Secret。遇到 Captcha、
身份不一致、无法确认 Apex 已退出或未知 EA 页面时必须 fail closed 并保留证据。
```
