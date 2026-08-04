# Apex Controller Lab

一个同时包含浏览器流程实验、Windows Apex 自动运行器和远程账号托管上报的项目。

## GitHub 与 Windows 更新

私有仓库通过 SSH 克隆：

```bash
git clone git@github.com:flyingPokonyan/apex-controller-lab.git
```

Windows 第一次克隆后，进入 `windows` 目录运行 `setup.ps1`。以后双击
`windows\update.cmd` 即可执行安全的 `git pull --ff-only` 并同步 Python
依赖。完整录像、原始全屏标定图、`.venv`、运行日志和发布压缩包不会上传；
运行所需的裁剪模板和配置会随仓库更新。

## Windows 托管运行：新机器从零开始

这里的“托管运行”指 `account-cycle`：Windows Runner 从 ApexForge 领取一个可用账号，
完成 EA 登录、Apex 运行、等级/经验上报和安全收口后，再决定是否领取下一个账号。

### 1. 每个人、每台机器都创建自己的 Runner

使用者需要有这个 GitHub 私有仓库的 SSH 访问权限，以及一个可登录
[ApexForge 控制台](https://apex.pokonyan.com/apex) 的账号。登录后进入
**Apex → Runner**：

1. 点击“创建设备”；
2. 输入能区分机器的名称，例如 `上海-测试机-01`；
3. 浏览器会自动下载 `account-cycle.private.json`；
4. 把它放到仓库的 `windows\account-cycle.private.json`。

一台 Windows 机器对应一个 Runner，不要让多台机器共用同一份配置。不同使用者也不要
互相传配置文件；Runner、账号池和运行记录都归属于创建它的 ApexForge 用户。

`account-cycle.private.json` 包含设备 ID、Reporter Token、Provider Token 和公网接口：

```json
{
  "schemaVersion": 1,
  "enabled": true,
  "deviceId": "dev_...",
  "reportUrl": "https://apex.pokonyan.com/v1/runner/reports",
  "reportToken": "<private>",
  "leaseUrl": "https://apex.pokonyan.com/v1/runner/account-leases",
  "providerToken": "<private>"
}
```

这个文件不会随 Git 下载，因为它已加入 `.gitignore`。Token 只在创建或轮换时显示，
不要提交到 Git、发到聊天、截图或放进普通日志。文件丢失后，推荐在控制台新建 Runner，
确认新机器可用后再撤销旧 Runner。

### 2. 克隆并安装 Windows 环境

只有三个前置会导致硬失败，其余都可以边跑边调：

- **Python 3.10 或更高**（代码使用 `zip(strict=)`）。`setup.ps1` 直接调用 `py -3 -m venv`，
  版本不对会在这一步失败。
- **EA App 已安装并处于打开状态、窗口可见**。Runner 只查找已存在的 `eadesktop.exe`
  窗口，不会替你启动 EA。当前是否已登录无所谓：托管流程发现账号与租约不符时会自己登出重登。
- **2560×1440、中文、全屏、主显示器**。识别区域是按这个环境标定的。

在 PowerShell 中执行：

```powershell
git clone git@github.com:flyingPokonyan/apex-controller-lab.git
cd apex-controller-lab\windows
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

然后把上一步下载的 `account-cycle.private.json` 放进当前 `windows` 目录。

### 3. 第一次运行

双击 `windows\account-cycle-once.cmd`。它领取一个账号、完整跑一轮就退出，不会进入循环。
确认这一轮的行为符合预期后，再用 `account-cycle.cmd` 进入连续领号。

不需要在它之前跑一串预检——下面那些检查入口是失败之后用来定位问题的，不是必经关卡。

### 4. 入口一览

日常入口：

| 入口 | 会做什么 | 什么时候使用 |
| --- | --- | --- |
| `account-cycle-once.cmd` | 领取一个账号并完整运行一轮，然后退出 | 平时就用它 |
| `account-cycle.cmd` | 一个账号收口后继续领取下一个账号 | 单账号跑通后，需要连续挂机时 |
| `account-cycle-resume.cmd` | 清除本地暂停后再运行一轮 | 上一轮结果是 `PAUSED`，且暂停原因已确认解决 |

出问题时再用这些定位，它们都不会进入循环：

| 入口 | 会做什么 | 什么时候使用 |
| --- | --- | --- |
| `run.ps1 account-cycle-check` | 只验证配置、网络和 Provider 鉴权，不领号、不碰 EA | 怀疑是 Token 或接口地址问题 |
| `ea-preflight.cmd` | 只读识别 EA 窗口和当前页面，不领号 | 怀疑 EA 页面识别有问题 |
| `ea-login-check.cmd` | 领 1 个号只验证登录再登出，不启动 Apex | 专门调登录时。**要求 EA 先手动登出并停在登录页**，已登录状态下会直接退出 |

`account-cycle-check` 看到“Runner 配置与 Provider 鉴权正常”即为通过。若提示
`INVALID_PROVIDER_TOKEN`，应在控制台重新创建 Runner 或轮换 Token；若配置里仍是
`192.168.*`，请重新下载公网配置或把两个 URL 改为上面的 HTTPS 地址。

任何时候都可以按 `F8` 紧急停止。遇到验证码、身份不一致、未知 EA 页面或无法确认退出时，
Runner 会暂停并保留证据，不应直接切到持续循环反复消耗账号。

准备长时间无人值守之前，先读
[EA 自动切号进度 · 挂机运行的环境要求](docs/ea-account-cycle-progress-20260801.md#挂机运行的环境要求)：
DisplayPort 关显示器会直接让桌面复制 `ACCESS_LOST`，RDP 断开会让捕获全黑，睡眠和屏保都要关。
第一次手动跑不用管这些。

### 5. 更新、重装与测试产物

- 日常更新双击 `windows\update.cmd`；它更新代码和依赖，不会覆盖私有配置或运行记录。
- 想全新 clone 时，先把 `account-cycle.private.json` 单独备份；也可以不保留它，改为在
  ApexForge 控制台创建新 Runner 并下载新配置。
- **删掉旧 Runner 配置之前，先在控制台确认这台设备没有活动租约**，有就先安全释放。本地
  配置一旦删除，那个账号只能等租约自然过期才会回到池子里。
- `windows\runs\` 包含截图、状态、事件和断网待补传 outbox。确认真实运行已经上报前不要
  删除。判据是每个 run 目录里 `report-outbox.jsonl` 的最大 `seq` 是否已经被
  `report-state.json` 的 `acceptedThrough` 覆盖：

  ```powershell
  Get-ChildItem .\windows\runs -Directory | ForEach-Object {
    $o = Join-Path $_.FullName 'report-outbox.jsonl'
    if (Test-Path $o) {
      $max = (Get-Content $o | ForEach-Object { ($_ | ConvertFrom-Json).seq } | Measure-Object -Maximum).Maximum
      $s   = Join-Path $_.FullName 'report-state.json'
      $ack = if (Test-Path $s) { (Get-Content $s -Raw | ConvertFrom-Json).acceptedThrough } else { 0 }
      if ($max -gt $ack) { "$($_.Name)  outbox=$max acked=$ack  ← 还没上报完" }
    }
  }
  ```

  没有输出就说明全部已确认送达，可以整个删掉。
- 新 clone 后需要重新运行 `setup.ps1`，但不需要修改运行代码。

## 菜单流程实验（首页）

当前首页把“进入游戏到出生前”的流程做成可重复运行的状态机：

```text
继续页
  → 可选活动弹窗
  → 大厅
  → 选择娱乐模式
  → 准备/开始匹配
  → 选择英雄（支持备选）
  → 可选枪械配装页
  → 停止在出生前
```

浏览器版的画面捕获、状态识别和点击全部是页面内部模拟，不会向 macOS、Windows 或 Apex 发送输入。它用于先验证分支、超时、日志与状态转换；Windows 接入时替换 `StateDetector` 和 `InputSender`，不重写流程。

需要覆盖的三个分支：

- 启动后是否出现活动弹窗；
- 首选英雄是否被占用；
- 当前娱乐模式是否提供枪械/配装选择页。

## 技术方案

- [**现状汇总**：有哪些采集数据、闭环做到哪一步](docs/current-status.md)
- [升级速度与停滞：8 月 3 日两段跑的复盘（含每分钟经验实测）](docs/xp-rate-and-idle-stall-20260804.md)
- [能力证据矩阵：每条能力靠什么帧成立、还缺什么](docs/capability-evidence.md)
- [本地菜单自动化验证与 Windows 接入清单](docs/menu-automation-local-validation.md)
- [模式选择、脱离跳伞与截图技术可行性](docs/apex-ui-drop-screenshot-feasibility.md)
- [Windows 首场匹配运行器](docs/windows-first-match-runner.md)
- [Windows 新号训练任务运行器](docs/windows-first-tutorial-runner.md)
- [可恢复任务运行器设计（讨论稿）](docs/resilient-task-runner-design.md)
- [远程上报 API 规范（账号、事件、等级经验）](docs/remote-reporting-api.md)
- [远程上报 OpenAPI 3.1](docs/remote-reporting-openapi.yaml)
- [EA App 自动登录与 20 级切号编排设计](docs/ea-account-orchestrator-design.md)
- [EA 自动切号真实服务器 Windows 闭环验证手册](docs/ea-server-windows-end-to-end-validation.md)
- [菜单改用文字证据：离线测量与待实测项](docs/ocr-first-calibration.md)
- [观察会话清单](docs/observation-session-checklist.md)

## Windows 首场匹配验证

日常入口是 `windows\play.cmd`。它从「继续」开始，先清掉攀爬设置、欢迎/纪念、
活动公告、奖励/升级和账户/好友/设置/进度导览，再统一处理选未上榜、匹配、脱离跳伞
与近战。快速大厅识别和低频覆盖层 OCR 可以分开跑，但所有真实输入只由一套 capability
调度器发送；识别不出、验证码或 OCR 异常都会停住并留证，不会点击覆盖层下面的大厅。
同一台机器重复双击时只有第一个会话能取得运行锁。

自动切号入口是 `windows\account-cycle.cmd`。会话抽取、20 级停止策略、租约
checkpoint、续租、终态恢复和真实 Provider HTTP 已经实现；EA App 没有对外暴露内部 UIA
控件树，所以登录走 Win32 加 OCR 的混合驱动。实施状态见
[EA App 自动登录与 20 级切号编排设计](docs/ea-account-orchestrator-design.md#19-当前实现状态)，
当前实测进度见 [EA 自动切号进度](docs/ea-account-cycle-progress-20260801.md)。

调试登录不要用 `account-cycle.cmd`：它是持续循环，一次失败清理完就会去领下一个真实
账号。改用 `windows\ea-login-check.cmd`，它只领一个租约、只验证登录、不启动 Apex，
无论成功失败都会关闭租约并退出，并把每一步的页面判定、点击定位方式和脱敏截图留在
`windows\runs\ea-login\<时间戳>\`。证据里不含邮箱全文、密码、Token 或 OTP。
需要控件树时，让 EA App 停在登录页后双击 `windows\probe-ea-uia.cmd`，脱敏结果保存到
`windows\runs\ea-uia-*.txt`。

两个入口使用不同的私有配置：固定账号的 `play.cmd` 读取
`windows\runner.private.json`；动态领号的 `account-cycle.cmd` 读取
`windows\account-cycle.private.json`。后者应直接使用 Apex 运营页“创建设备”时下载的
托管配置，不要加入静态 `accountId`。两份私有配置都已加入 `.gitignore`。

第一次使用先在 `windows` 目录运行 `setup.ps1`，然后直接双击 `play.cmd`。`run.ps1 start/live`
是旧监督器和线性回归入口，不再作为自动循环的推荐启动方式。

`run.ps1 validate` 只在**包含原始全屏样本的发布包**里有意义。源码仓库出于隐私原因不含
`calibration/raw/`，所以在全新 clone 上它会把每张样本报成 `SKIP` 并输出“本次没有验证任何
东西”——这不是故障，但也不能当作可用性检查。

固定账号的 `play.cmd` 默认保持纯本地运行；放入完整的 `windows\runner.private.json`
后才会绑定指定账号、验证 Steam 稳定 ID（已配置时）、异步上报并在断网后补传。
托管 `account-cycle.cmd` 则强制要求本页开头说明的设备配置和远程上报。`accountId`
固定对应服务端 `apex_profiles.public_id`，不是昵称、EA/Steam 用户名或数据库自增 ID；
手动模式配置示例见 `windows\runner.private.example.json`，完整接口见
[远程上报 API 规范](docs/remote-reporting-api.md)。

结算返回大厅已按操作者确认的完整交互实现为 `TAB → 等待 1500ms → 长按 SPACE 2000ms`，
并以大厅态作为后置确认；不再等待或识别中间确认页。该序列和跳伞的 `LCTRL` 2 秒长按
都还需要下一次目标机器 `play` 实跑验证。

`windows/` 已包含针对 `2560×1440`、中文、全屏环境的截图门控运行器。它覆盖新手任务完成后的首场匹配：继续、准备、自动选择英雄、跟队时按 `LCTRL` 脱离、`E` 跳伞、自由落体、落地确认与最终截图。真正的跳伞指挥官画面还缺一张标定图，当前不可达而不会猜测。

每次稳定进入大厅后，Runner 会在发送大厅动作前读取两次等级和经验；两次一致才记录
可信值，最多尝试三帧，失败只记录 `FAILED` 而不会猜值或阻塞后续匹配。Apex 不在前台
时任务会释放输入并暂停观察；任何时候按 `F8` 紧急停止。

区域 OCR 和外置清障字典已经接入。`match` 档默认仅识别留证，需要 `--arm-safe-obstacles` 才允许字典中的 `Enter`/`Esc` 安全动作；`tutorial` 档在配置里声明了 `ocr.autoArm`，双击入口即为已武装。未知页面在两种情况下都不会盲点。

区域可以声明 `singleLine`，这样跳过文字检测直接识别。检测占了 OCR 的绝大部分开销，且开销与输入尺寸基本无关，所以只裁小区域省不下时间——大厅模式名实测 4.7ms（单行）对 234ms（带检测），置信度相同。

运行状态可从 `windows/runs/status.json` 查看。每次会话使用唯一目录，`events.jsonl`
是本地权威事件流；启用远程上报后还会生成连续序号的 `report-outbox.jsonl` 和确认游标，
Token 不会写入这些文件、manifest、截图或错误事件。

## 观察模式（标定用）

```bat
windows\observe.cmd
```

真实截屏、跑全套识别、逐帧记录得分与 OCR 文本，**整条命令不构造输入发送器，没有任何代码路径能发出按键或点击**，可以一边正常游戏一边开着。

它存在的原因是离线验证给不出阈值余量：每个模板都是它所验证的那张原图的逐字节裁剪，所以离线分数恒为 `1.000`。观察会话产出 `observations-summary.json`，其中 `separation` 表示最差的真阳性比最好的假阳性高多少——这才是定阈值的依据。采集清单见[观察会话清单](docs/observation-session-checklist.md)。

## Windows 新号训练验证

完整新号录像已经转成独立的 `tutorial` 任务配置。它覆盖训练任务清单、训练结束返回大厅、可选的“账户/好友/设置/进度”导览，以及明确选择“未上榜（三人赛）”。EA 登录或验证码只会触发人工暂停，不会被自动填写。

在包含完整离线样本的发布包中，可先运行 `validate-tutorial.cmd` 做纯离线验证，再运行 `start-tutorial.cmd` 启动任务；等价命令是 `.\\run.ps1 validate tutorial` 和 `.\\run.ps1 start tutorial`。GitHub 源码仓库出于隐私原因不包含原始全屏样本，但包含实时运行所需的裁剪模板。当前只完成了录像回放验证，走路、准星对齐和最后一组标记仍需在目标 Windows 上做首轮实测标定，不能把离线通过表述成真实完成训练。

## 本地运行

```bash
npm install
npm run dev
```

## 验证构建

```bash
npm run build
```
