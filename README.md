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
- [菜单改用文字证据：离线测量与待实测项](docs/ocr-first-calibration.md)
- [观察会话清单](docs/observation-session-checklist.md)

## Windows 首场匹配验证

日常入口是 `windows\play.cmd`。它从「继续」开始，先清掉攀爬设置、欢迎/纪念、
活动公告、奖励/升级和账户/好友/设置/进度导览，再统一处理选未上榜、匹配、脱离跳伞
与近战。快速大厅识别和低频覆盖层 OCR 可以分开跑，但所有真实输入只由一套 capability
调度器发送；识别不出、验证码或 OCR 异常都会停住并留证，不会点击覆盖层下面的大厅。

结算返回大厅已按操作者确认的完整交互实现为 `TAB → 等待 800ms → 长按 SPACE 2000ms`，
并以大厅态作为后置确认；不再等待或识别中间确认页。该序列和跳伞的 `LCTRL` 2 秒长按
都还需要下一次目标机器 `play` 实跑验证。

`windows/` 已包含针对 `2560×1440`、中文、全屏环境的截图门控运行器。它覆盖新手任务完成后的首场匹配：继续、准备、自动选择英雄、跟队时按 `LCTRL` 脱离、`E` 跳伞、自由落体、落地确认与最终截图。真正的跳伞指挥官画面还缺一张标定图，当前不可达而不会猜测。

真实输入默认不会启动；先在 Windows 中运行 `.\\windows\\run.ps1 validate` 做离线识别，再显式运行 `.\\windows\\run.ps1 start --mode match` 启动可恢复的常驻匹配任务。启动后可在 `http://127.0.0.1:8765` 查看状态、关键截图和事件，并暂停、继续、停止或释放输入。Apex 不在前台、加载黑屏或暂时无法识别时，任务会释放输入并暂停观察，而不是直接退出；任何时候按 `F8` 紧急停止。

区域 OCR 和外置清障字典已经接入。`match` 档默认仅识别留证，需要 `--arm-safe-obstacles` 才允许字典中的 `Enter`/`Esc` 安全动作；`tutorial` 档在配置里声明了 `ocr.autoArm`，双击入口即为已武装。未知页面在两种情况下都不会盲点。

区域可以声明 `singleLine`，这样跳过文字检测直接识别。检测占了 OCR 的绝大部分开销，且开销与输入尺寸基本无关，所以只裁小区域省不下时间——大厅模式名实测 4.7ms（单行）对 234ms（带检测），置信度相同。

原来的 `.\\windows\\run.ps1 live` 仍保留为固定首场链路的线性回归入口，不建议作为日常任务入口。运行状态可从 `windows/runs/status.json` 查看，每次会话的事件和关键截图保存在对应的时间目录中。

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
