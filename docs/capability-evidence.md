# 能力证据矩阵与当前进度

> 更新：2026-07-29 深夜。所有 ROI、置信度、阈值余量均取自当天目标机器
> （`2560×1440`、中文、全屏）的实时 DXGI 帧，不是录像帧。

## 为什么需要这张表

加一条能力需要**两张**帧，不是一张：

1. **触发帧** —— 用来写识别规则；
2. **动作后应看到的帧** —— 用来写后置确认，`toggle` 和 `commit` 必须有。

第一轮采集只按"有哪些画面"列清单，漏了第二类，结果是做到某条能力才发现少东西。
以后每加一条能力，先在这张表里补一行，再决定要不要采集。

## 现有能力（`windows/config/enter-game.zh-CN.json`）

| 优先级 | 能力 | 触发画面 | 动作 | 分级 | 后置画面 |
|---|---|---|---|---|---|
| 90 | dismiss-climb-settings | CLIMB_SETTINGS_MODAL | 点「保持原样」 | idempotent | — |
| 80 | title-continue | CONTINUE | 点「继续」 | idempotent | — |
| 70 | post-match-return-lobby | POST_MATCH_SUMMARY | `TAB` | idempotent | 三种大厅态 / 攀爬弹窗 |
| 60 | lobby-open-mode-panel | LOBBY_SELECT_REQUIRED、LOBBY_READY_TRAINING | 点主按钮 | idempotent | 模式面板两态 |
| 55 | mode-panel-focus-target | MODE_PANEL_TARGET_VISIBLE | 点未上榜卡片 | idempotent | 悬停态 / 大厅 |
| 55 | mode-panel-confirm-hovered | MODE_PANEL_TARGET_HOVERED | 点「选择模式」 | commit | LOBBY_READY_UNRANKED |
| 40 | dropship-detach | DROPSHIP_FOLLOWING | `LCTRL` | **toggle** | LAUNCH_READY / FREEFALL |
| 40 | dropship-launch | LAUNCH_READY | `E` | commit | FREEFALL / IN_MATCH_ALIVE |
| 10 | in-match-melee | IN_MATCH_ALIVE | `V`，1–5 秒 | periodic | 周期动作无后置 |

每条的触发帧和后置帧都已具备。

## 有意不做能力的画面

能自动过去的画面就让它自动过。能力越少，越不容易在计划外的画面上出错。

| 画面 | 为什么不做 |
|---|---|
| 英雄选择 | 超时自动选，不需要输入 |
| 娱乐模式开局选武器 | 实测超时会自动分配一套，照样进对局 |
| 控制模式选出生点 | 同上，预计也会超时自动分配（待确认） |
| 观战 / 死亡回放 | 此时任何输入都无意义；近战闸门刻意不覆盖它 |
| 登录过场视频 | 约 60 秒后自行结束，主动长按空格探测不划算 |
| 断连回到继续页 | 继续页已有能力，断连恢复是它的副产品 |

## 仍缺的证据

| 缺什么 | 挡住什么 |
|---|---|
| 大厅按下「准备」之后的排队中画面 | `lobby-start-match`，唯一挡住全自动闭环的一条 |
| 模式面板「自由式」栏的实时帧 | 选择娱乐模式 |
| 娱乐模式大厅的模式名 | `lobby-ready-entertainment` |
| 娱乐模式结算页 | 娱乐模式的 post-match（可能与匹配不同） |

无法主动制造、碰到再补：断连弹窗、匹配惩罚提示（必须归入人工暂停）、
公告 / 奖励 / 升级页。

## 实测阈值余量（模板证据）

离线验证对每个模板都给 `1.000`，因为模板是所验证那张原图的逐字节裁剪，不含信息量。
实时帧上的真实余量：

| 状态 | 阈值 | 实测最低 | 余量 |
|---|---|---|---|
| CONTINUE | 0.72 | 1.000 | 0.280 |
| MODE_PANEL_MATCH | 0.68 | 0.811 | 0.131 |
| DROPSHIP_FOLLOWING | 0.72 | 0.805 | 0.085 |
| LAUNCH_READY | 0.72 | 0.780 | 0.060 |
| LEGEND_SELECT | 0.72 | 0.769 | 0.049 |
| LOBBY_READY | 0.72 | 0.758 | 0.038 |
| LANDED | 0.62 | 0.622 | **0.002** |
| FREEFALL | 0.62 | **从未匹配** | — |

后两个已改用文字证据。`LOBBY_TRAINING_READY` 还曾与 `LOBBY_READY` 在同一帧上
同时匹配——离线互斥、实时不互斥。

## OCR 性能

| 方式 | 耗时 |
|---|---|
| 全屏 det+cls+rec | 2100–6800 ms |
| 单行紧致 ROI，跳过检测 | 4–16 ms |
| 一帧跑完全部区域 | 约 31 ms |

决定开销的是**文字检测**，且其成本与输入尺寸基本无关——只裁小区域省不下时间，
必须紧到只含一行字才能跳过检测。这是 `singleLine` 存在的原因。

## 采集数据位置

会话产出在 Windows 的 `windows/runs/<时间戳>/`，其中：

- `observations.jsonl` —— 逐帧得分、OCR 文本、决策；
- `observations-summary.json` —— 每状态得分分布与 `separation`；
- `screenshots/` —— 分类变化与去重后的定时截图。

原始全屏帧不进仓库；结论一律回填到本文与
[菜单改用文字证据](ocr-first-calibration.md)。
