# Apex Controller Lab：模式选择、脱离跳伞与截图技术可行性

> 文档版本：0.1
> 日期：2026-07-27
> 状态：方案设计，尚未接入 Windows 或 Apex
> 目标：说明“选择模式并开始游戏 → 脱离队伍 → 不控制方向自由下落 → 截图留证”是否可实现，以及如何分阶段验证。

## 1. 结论

从输入自动化角度，以下流程可以实现：

```text
识别大厅
  → 打开模式面板
  → 选择预设模式
  → 点击开始/准备
  → 等待英雄选择或运输船阶段
  → 在满足条件时执行“脱离跟随”动作
  → 释放全部方向输入
  → 等待自然落地
  → 触发截图并记录本次运行
```

三个核心需求的结论如下：

| 需求 | 可行性 | 关键前提 |
|---|---|---|
| 选择模式、开始游戏 | 可行 | 分辨率、DPI、语言和界面布局固定；每次点击后验证界面状态 |
| 脱离跳伞队伍 | 条件可行 | 当前不是独立跳伞状态；实际按键绑定已经人工确认；界面出现可脱离提示 |
| 不控制方向、自然落地 | 可行 | 脱离后释放所有移动与视角输入；接受落点不可预测 |
| 游戏内截图 | 可行 | 使用 Apex 自带截图绑定、Windows 截图快捷键，或程序直接捕获游戏窗口 |

需要特别说明：

1. “点位固定”可以作为第一版前提，但不能把一次坐标点击等同于成功。加载延迟、弹窗、活动面板和焦点变化仍可能导致误操作。
2. 左 `Ctrl` 不能写死为脱离动作。EA 当前默认 PC 键位中，左 `Ctrl` 是“按住下蹲”；脱离动作应以实际游戏提示和用户键位配置为准。
3. 脱离后不给方向输入，并不代表物理意义上的垂直自由落体。角色可能继承脱离瞬间的速度、视角和飞行方向，但最终会在某处落地。
4. 菜单操作属于离散 UI 自动化；进入跳伞阶段后，如果只是“落到任意位置”，可以继续使用简单状态机。如果要求准确落点，就会升级为需要屏幕反馈的连续控制问题。

## 2. 范围与边界

本文档覆盖：

- 固定分辨率下的菜单点位操作；
- 基于截图的界面状态确认；
- 可配置的键盘、鼠标或手柄动作；
- 脱离后的输入归零；
- 截图保存和运行记录；
- 开发产物与 Windows/云 Windows 运行时的边界。

本文档不覆盖：

- 反作弊绕过或隐藏自动输入；
- 进程注入、游戏内存读取或修改；
- 在线对局中的自动战斗、搜集、寻路或精确落点；
- 未经云服务商允许的驱动或虚拟硬件修改。

把自动化接入真实 Apex 对局可能违反 EA 规则并产生账号风险。第一阶段应限定在自建界面、Windows 手柄测试程序，以及明确允许的测试环境中。

## 3. 输入方式选择

用户当前提出的流程同时包含：

- 通过屏幕点位选择模式：天然适合鼠标；
- 按 `Ctrl` 脱离：属于键盘输入；
- 模拟摇杆：属于手柄输入。

因此第一版最快的实现是“混合输入适配器”，而不是强制所有动作都通过虚拟手柄完成：

```text
菜单点位       → 鼠标适配器
脱离动作       → 可配置键盘按键
移动/视角测试  → 虚拟手柄适配器
截图           → 截图服务直接执行
```

如果后续坚持纯手柄模式，则菜单选择不能再描述为“点击坐标”，而要改为：

```text
恢复到已知焦点
  → D-Pad/左摇杆移动若干格
  → A 键确认
```

两种方式可以共用同一个状态机，只替换 `InputSender`。

## 4. 总体架构

```text
ScenarioOrchestrator
├── StateDetector
│   ├── WindowCapture
│   ├── AnchorMatcher
│   └── ScreenStateClassifier
├── InputSender
│   ├── BrowserTestSender
│   ├── WindowsMouseKeyboardSender
│   └── WindowsVirtualGamepadSender
├── ScreenshotService
│   ├── EvidenceCapture
│   └── UserScreenshotTrigger
└── RunRecorder
    ├── events.jsonl
    ├── screenshots/
    └── result.json
```

职责划分：

- `ScenarioOrchestrator`：执行流程、超时、重试与失败停止。
- `StateDetector`：判断当前是大厅、模式面板、匹配中、跳伞阶段还是落地阶段。
- `InputSender`：把抽象动作转换成鼠标、键盘或手柄信号。
- `ScreenshotService`：为程序判断提供窗口帧，同时在指定节点保存纪念截图。
- `RunRecorder`：保存每次动作、截图、耗时和最终状态，便于复盘。

## 5. 状态机设计

建议使用显式状态机，避免用一串固定 `sleep + click` 贯穿全流程：

```text
UNKNOWN
  ↓
LOBBY_READY
  ↓
MODE_PANEL_OPEN
  ↓
MODE_SELECTED
  ↓
MATCHMAKING
  ↓
LEGEND_SELECT_OR_LOADING
  ↓
DROPSHIP_OR_FOLLOWING
  ↓
DETACHED
  ↓
FALLING
  ↓
LANDED
  ↓
SCREENSHOT_SAVED
```

每个状态转换必须包含四个部分：

```text
前置状态确认
  → 执行动作
  → 等待目标状态
  → 保存证据或失败停止
```

示例：

```text
确认 MODE_PANEL_OPEN
  → 点击“目标模式”点位
  → 最多等待 3 秒
  → 检查目标模式高亮/名称
  → 成功后进入 MODE_SELECTED
```

## 6. 需求一：选择模式并开始游戏

### 6.1 点位方案是否可行

可行。若以下条件保持固定，目标控件的屏幕点位通常具有较高稳定性：

- Windows 显示分辨率固定，例如 `1920 × 1080`；
- Windows DPI 缩放固定，例如 `100%`；
- Apex 语言固定；
- 游戏使用固定的全屏、无边框或窗口模式；
- UI 缩放和安全区域固定；
- 不移动窗口，不更换显示器；
- 活动弹窗已处理。

不要只保存绝对像素，建议同时保存归一化坐标：

```text
normalizedX = pixelX / windowWidth
normalizedY = pixelY / windowHeight
```

示例配置：

```json
{
  "environment": {
    "resolution": "1920x1080",
    "dpiScale": 1.0,
    "language": "zh-CN",
    "displayMode": "borderless"
  },
  "targets": {
    "modeButton": { "x": 0.112, "y": 0.812 },
    "preferredMode": { "x": 0.284, "y": 0.436 },
    "readyButton": { "x": 0.846, "y": 0.884 }
  }
}
```

以上坐标只是数据格式示例，不是 Apex 的真实点位。真实点位需要在目标 Windows 环境中人工标定。

### 6.2 为什么仍需要截图验证

即使坐标不变，以下状态仍会改变点击结果：

- 页面还没加载完成；
- 上一次运行停留在不同子菜单；
- 存在活动、公告或错误弹窗；
- 当前模式已经被选中；
- 鼠标焦点不在游戏窗口；
- 匹配按钮暂时不可用。

第一版可以采用“固定点位 + 小区域截图验证”：

```text
截取按钮附近 120 × 60 像素
  → 检查预设图标、文字或高亮色
  → 确认无误后点击
```

推荐策略：

- 点击前确认控件存在；
- 点击后确认高亮或界面状态发生变化；
- 最多重试一次；
- 仍不成功就停止，不继续盲点后续坐标。

## 7. 需求二：脱离跳伞队伍

### 7.1 `Ctrl` 是否可行

如果用户在目标 Windows 上看到的实际提示或自定义键位明确为 `Ctrl`，发送一次或按住指定时长的 `Ctrl` 可以执行对应动作。

但不能把它当作 Apex 的通用默认规则。EA 当前官方默认 PC 键位中，左 `Ctrl` 是“按住下蹲”，并且游戏允许用户重新绑定按键。因此实现中必须使用动作语义，而不是把业务逻辑写成固定键名：

```text
错误：sendKey("CTRL")

正确：performAction("DETACH_FROM_JUMPMASTER")
      ↓
配置解析为本机已经确认的按键或手柄按钮
```

建议配置：

```json
{
  "bindings": {
    "detachFromJumpmaster": {
      "device": "keyboard",
      "key": "CTRL",
      "mode": "tap",
      "durationMs": 80,
      "verifiedAt": "manual-calibration"
    }
  }
}
```

只有经过目标机器人工确认后，才写入具体键值。

### 7.2 触发条件

脱离动作不能只依赖“进入对局后等待 N 秒”，至少需要确认：

- 已进入运输船/跟随跳伞阶段；
- 当前角色处于跟随状态；
- 屏幕上出现“脱离/停止跟随”类提示；
- 当前没有覆盖菜单或断线提示。

流程：

```text
检测到 FOLLOWING 状态
  → 读取已确认的 detachFromJumpmaster 绑定
  → 发送按下
  → 80ms 后释放
  → 检查跟随提示消失或飞行 HUD 变化
  → 标记 DETACHED
```

如果玩家本身是 Jumpmaster，业务语义可能变成“开始跳伞”而不是“脱离跟随”，状态机应分开处理，不能共用一个盲按动作。

## 8. 需求三：不控制方向，自然落地

完成脱离后，程序立即发送“全部输入归零”：

```json
{
  "leftX": 0,
  "leftY": 0,
  "rightX": 0,
  "rightY": 0,
  "pressedButtons": []
}
```

键盘适配器同时执行：

```text
释放 W/A/S/D
释放鼠标按钮
释放所有仍处于按下状态的功能键
```

随后不再发送移动或视角指令，交给游戏自身物理系统处理。

该方案的验收标准应写成：

> 角色成功脱离跟随，自动化不再提供移动/视角输入，并最终进入落地状态。

不应写成：

> 角色垂直落到固定位置。

原因是脱离瞬间可能已经存在水平速度与视角方向。运输船路径、队友跳伞方向和脱离时机不同，最终落点也会不同。

### 8.1 如何判断落地

第一版可以采用双条件：

1. 最长等待时间，例如 90 秒，超时即失败；
2. 周期性截图检测落地 HUD，例如跳伞提示消失且常规地面 HUD 稳定出现多帧。

为了避免单帧误判，建议连续三次确认：

```text
t0：检测为 LANDED
t0 + 300ms：仍为 LANDED
t0 + 600ms：仍为 LANDED
→ 确认落地
```

## 9. 游戏内如何触发截图

这里需要区分两个目的。

### 9.1 给用户保存一张纪念截图

可选方案：

#### 方案 A：Apex 自带 Screenshot 绑定

EA 官方辅助功能文档确认，PC 版键鼠设置的 `Miscellaneous` 中存在 `Screenshot` 动作，并允许更改键位。可以预先绑定一个不冲突的按键，然后由输入发送器触发。

优点：

- 游戏焦点内单键触发；
- 不需要打开 Windows 截图浮层；
- 适合在落地或目标事件后保存纪念图。

注意：应在目标机器人工确认具体绑定和保存目录，不能假设所有安装都使用相同默认键。

#### 方案 B：Xbox Game Bar

Windows 官方快捷键：

```text
Win + Alt + Print Screen
```

截图通常保存到：

```text
Videos/Captures
```

优点是与游戏平台无关；缺点是云 Windows、远程桌面或精简系统可能禁用 Game Bar，组合键也可能被远程客户端截获。

#### 方案 C：Steam 截图

如果通过 Steam 启动，可以使用 Steam 当前配置的截图键。默认值可能被用户修改，因此仍应从 Steam 设置中人工确认，不在业务代码中写死。

### 9.2 给程序判断当前界面

状态识别不应该反复按截图快捷键并等待文件落盘。推荐使用 Windows 的窗口捕获接口直接获得画面帧：

```text
Windows.Graphics.Capture
  → 选择 Apex 窗口或目标显示器
  → 持续取得画面帧
  → 裁剪状态识别区域
  → 模板匹配/文字识别
```

Microsoft 官方说明 `Windows.Graphics.Capture` 可以捕获显示器或应用窗口，用于生成视频流或静态快照。首次使用通常需要用户通过系统选择器确认目标窗口，系统可能显示捕获边框。

程序截图建议统一命名：

```text
runs/<run-id>/screenshots/
├── 001-lobby-ready.png
├── 002-mode-selected.png
├── 003-matchmaking.png
├── 004-following.png
├── 005-detached.png
├── 006-landed.png
└── 007-final-user-shot.png
```

截图元数据记录：

```json
{
  "stage": "LANDED",
  "capturedAt": "2026-07-27T20:15:32.482+08:00",
  "windowSize": [1920, 1080],
  "stateConfidence": 0.96,
  "trigger": "three-frame-confirmation"
}
```

## 10. 开发环境与 Windows 运行环境边界

开发机器只用于编写代码、运行纯模拟测试和构建发布产物，不属于最终运行链路。开发机器使用 macOS、Linux 还是 Windows，不影响运行架构。

```text
开发环境
├── 编写状态机和输入协议
├── 运行假大厅/假跳伞测试
├── 执行自动化测试
└── 构建 Windows 发布包
```

实际执行时，全部组件必须部署在同一台 Windows 或云 Windows 中：

```text
Windows / 云 Windows
├── ScenarioOrchestrator
├── WindowCapture
├── WindowsMouseKeyboardSender
├── 可选 VirtualGamepadSender
├── Apex / Windows 测试程序
└── runs/ 证据目录
```

运行时不从开发机器接收逐帧输入，也不依赖开发机器截图或触发动作。状态机、截图、输入发送和运行记录都在 Windows 内部完成。若使用 Parsec 或 Moonlight，它们只提供人工查看和维护入口，不是自动化链路的组成部分。

云 Windows 上线前必须先验证：

1. 服务商允许运行目标游戏；
2. 反作弊不阻止该云虚拟机；
3. 允许安装所需输入驱动；
4. 截图接口能获得有效画面而不是黑屏；
5. Windows 内部输入发送器能直接作用于目标测试程序。

## 11. 配置与接口建议

### 11.1 场景配置

```json
{
  "scenario": "select-mode-detach-freefall-capture",
  "preferredMode": "configured-manually",
  "inputProfile": "keyboard-mouse-v1",
  "timeouts": {
    "uiTransitionMs": 5000,
    "matchmakingMs": 600000,
    "dropshipMs": 180000,
    "landingMs": 90000
  },
  "screenshot": {
    "evidence": true,
    "userShotAfterLanding": true,
    "provider": "windows-graphics-capture"
  }
}
```

### 11.2 输入接口

```ts
interface InputSender {
  clickNormalized(x: number, y: number): Promise<void>;
  tapAction(action: InputAction, durationMs?: number): Promise<void>;
  setGamepad(frame: GamepadFrame): Promise<void>;
  releaseAll(): Promise<void>;
}

type InputAction =
  | "OPEN_MODE_PANEL"
  | "SELECT_PREFERRED_MODE"
  | "READY"
  | "DETACH_FROM_JUMPMASTER"
  | "TAKE_USER_SCREENSHOT";
```

### 11.3 状态识别接口

```ts
interface StateDetector {
  capture(): Promise<CapturedFrame>;
  detect(frame: CapturedFrame): Promise<DetectedState>;
  waitFor(state: GameState, timeoutMs: number): Promise<DetectedState>;
}
```

## 12. 分阶段验证计划

### 阶段 A：跨平台纯模拟测试

- 增加假大厅与模式面板；
- 使用固定点位选择预设模式；
- 加入随机加载延迟和弹窗，验证状态机不会盲点；
- 增加假运输船、跟随、脱离和自由落地阶段；
- 在关键节点保存 Canvas 截图。

验收：不产生系统输入，完整流程能重复执行并留下运行记录。本阶段只验证算法，不代表实际运行时需要开发机器参与。

### 阶段 B：Windows 普通应用测试

- 复制状态机和配置；
- 接入 Windows 窗口捕获；
- 使用自建 Windows 测试程序验证鼠标、键盘和截图；
- 使用 Windows 手柄测试器验证虚拟手柄轴和按钮。

验收：Windows 能收到输入，截图与状态转换一致。

### 阶段 C：云 Windows 兼容性闸门

- 验证管理员权限、驱动安装和重启保留；
- 验证窗口捕获；
- 验证 Windows 内部输入发送与目标窗口焦点；
- 单独验证 Apex 能否在该云环境启动。

任何一项失败都停止，不进入下一阶段。

### 阶段 D：规则与账号风险复核

菜单和对局自动化可能触及 EA 对第三方软件与不公平优势的限制。接入真实在线对局之前，应重新核对 EA 当前规则；本文档不提供反作弊规避方案。

## 13. 主要风险

| 风险 | 影响 | 缓解方式 |
|---|---|---|
| UI 更新导致点位变化 | 选错模式或按钮 | 固定环境；点击前后截图验证；失败立即停止 |
| 弹窗覆盖目标按钮 | 点击错误区域 | 先识别大厅锚点；建立弹窗处理状态 |
| `Ctrl` 绑定假设错误 | 执行下蹲或无动作 | 在目标机器上人工确认动作绑定；按动作名配置 |
| 非 Jumpmaster 状态不同 | 无法按预期脱离 | 区分 Jumpmaster、跟随、独立跳伞状态 |
| 输入没有完全释放 | 落点持续偏移 | 集中维护按键状态；进入 DETACHED 后调用 `releaseAll()` |
| 截图为黑屏 | 无法判断状态 | 使用无边框模式；提前验证 Windows.Graphics.Capture |
| 云服务阻止反作弊/驱动 | 无法运行 | 购买前询问；按小时小规模验证 |
| 自动化违反游戏规则 | 账号处罚 | 限制在测试环境；不做反作弊绕过 |

## 14. 最终建议

第一版不需要解决精确跳伞控制。按当前需求，可以把目标收敛为：

```text
固定环境选择模式
  → 开始匹配
  → 识别跟随跳伞状态
  → 执行经过人工确认的脱离动作
  → 释放全部方向输入
  → 确认自然落地
  → 保存截图和运行记录
```

这是一个有限状态自动化问题，技术上可实现。决定可靠性的不是按键发送本身，而是：

- 每一步是否先确认当前状态；
- 具体键位是否经过目标机器标定；
- 输入是否能够完整释放；
- 是否保存足够证据判断成功与失败。

## 15. 参考资料

- [EA：《Apex Legends》PC 及主机控制设置](https://help.ea.com/zh/articles/apex-legends/pc-and-controller-settings/)：当前默认 PC 键位、手柄布局和重新绑定入口。
- [EA：Apex Legends Accessibility Features for PC](https://www.ea.com/able/resources/apex-legends/pc/features)：键鼠设置中存在可配置的 Screenshot 动作；默认手柄的左右摇杆分别用于移动与视角。
- [Microsoft：Screen capture](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture)：`Windows.Graphics.Capture` 可取得显示器或应用窗口帧并生成静态截图。
- [Microsoft：Xbox Game Bar 录制与截图](https://support.microsoft.com/id-id/accessibility/windows/use-a-screen-reader-to-record-your-screen-with-xbox-game-bar)：游戏截图快捷键和默认保存位置说明。
- [EA：Play by the rules in Apex Legends](https://help.ea.com/uk/help/apex-legends/apex-legends/play-by-the-rules-in-apex-legends/)：第三方软件、作弊和账号风险说明。
