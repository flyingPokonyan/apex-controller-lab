# Apex Controller Lab

一个完全运行在浏览器内部的 Apex 菜单流程与手柄输入信号实验室。

## GitHub 与 Windows 更新

私有仓库通过 SSH 克隆：

```bash
git clone git@github.com:flyingPokonyan/apex-controller-lab.git
```

Windows 第一次克隆后，进入 `windows` 目录运行 `setup.ps1`。以后双击
`windows\update.cmd` 即可执行安全的 `git pull --ff-only` 并同步 Python
依赖。完整录像、原始全屏标定图、`.venv`、运行日志和发布压缩包不会上传；
运行所需的裁剪模板和配置会随仓库更新。

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

## 爱心轨迹实验（`/heart`）

它把等弧长采样的爱心轨迹转换为 60 Hz、范围为 `[-1, 1]` 的左右摇杆输入帧，并提供两种接收器进行验证：

- 画笔模式：左摇杆直接控制二维画笔速度；
- 角色模式：左摇杆使用角色本地坐标，右摇杆控制朝向，并模拟死区与运动响应。

页面不会调用系统键鼠、虚拟手柄或游戏接口。完成一次轨迹后，可以导出本次模拟产生的 `InputFrame[]` JSON。

## 技术方案

- [**现状汇总**：有哪些采集数据、闭环做到哪一步](docs/current-status.md)
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

第一次使用先在 `windows` 目录运行 `setup.ps1`，再运行 `run.ps1 validate` 做不发送输入
的离线检查。日常运行只双击 `play.cmd`；`run.ps1 start/live` 是旧监督器和线性回归入口，
不再作为自动循环的推荐启动方式。

远程上报默认关闭，不影响纯本地使用。Apex 管理页面以后为所选账号和设备生成
`runner.private.json`，放到 `windows\runner.private.json` 后，`play.cmd` 会自动绑定该
账号、验证 Steam 稳定 ID（已配置时）、异步上报并在断网后补传。`accountId` 固定对应
服务端 `apex_profiles.public_id`，不是昵称、EA/Steam 用户名或数据库自增 ID；配置示例见
`windows\runner.private.example.json`，完整接口见[远程上报 API 规范](docs/remote-reporting-api.md)。

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
