# 耗时日志和组合包识别

本次性能工作只增加测量，没有调整 OCR 算法、轮询间隔、等待参数、租约协议或账号轮转条件。

## 日志位置

- `windows/runs/<runId>/events.jsonl`：每 30 秒一条 `PERFORMANCE_SUMMARY`，结束时补最后一段。记录截图、画布缩放、各 OCR 区域、状态观察、等级读取、动作、主循环和轮询休眠的次数、均值、总耗时、最大值及最近 256 次样本的 P95。指标存在嵌套关系，不能直接把各项相加。
- 同一文件中的 `ACTION_STARTED` / `ACTION_SENT` / `ACTION_CONFIRMED`：执行开始、执行完成及后置画面确认；完成事件有 `durationMs`，确认事件有 `confirmationMs`。周期性游戏动作只做聚合，避免大量日志。
- `STATE_DETECTED` / `STATE_UNKNOWN`：包含前一状态持续时间，能区分未知画面与最后一次已识别画面。上报为 `STATE_CHANGED`，保留未知状态和耗时。
- `SCREENSHOT_SAVED` / `EVIDENCE_SAVED`：编码耗时；异步截图另有排队时间。
- `windows/runs/<runId>/report-timings.jsonl`：后台事件/截图 HTTP 耗时、状态码和积压量。单文件 2 MiB，保留一个轮换文件；不会递归上报自身的上传日志。
- `windows/runs/ea-login/timings/YYYY-MM-DD.jsonl`：EA 截图/OCR 和相邻已记录步骤间耗时、外层工作流阶段耗时，关联已有公开 `leaseId`；保留 14 天，每天最多两个约 10 MiB 文件。原来的 20 次脱敏截图记录仍按原规则保留。EA 日志没有账号密码、完整邮箱、验证码或原始 OCR 文本。

EA 步骤间耗时包含点击、OCR、重试和等待；`WORKFLOW_PHASE` 记录阶段切换，异常退出时可以用最后一条阶段和最后一步判断停在哪。它们不表示游戏内部事件的精确发生时间。

## 组合包

只在已识别大厅读取右下角卡片的小区域；通过相邻的 `APEX` 和 `组合包` 标签定位数字，兼容上下两个卡片位置。不能用等级推算库存，不能把未显示卡片当成零。

两次 **DXcam 新捕获** 读到一致高置信度数字才发 `APEX_PACKS`。相同像素的新帧可以确认同一库存，缓存旧帧不算第二次确认。每次大厅访问最多三个普通读取尝试，不等待识别成功再排队；达到目标后，最多六次额外探测、约 4 秒预算做最后补读，单次 OCR/截图调用本身耗时不在硬超时控制内。读取失败不影响等级目标完成。

只处理已识别的手心输入法/网易 UU 远程通知：标题 OCR、所属进程、右下角小窗口尺寸、实际 X 图形全部匹配才允许点击。点击前重新检查前台、HWND 和窗口矩形，使用物理桌面坐标；每个会话最多两次，间隔至少 30 秒。确认遮挡窗口消失并恢复 Apex 前台，再从新帧读数。未知进程只记录 `NOTIFICATION_CANDIDATE`，不发送输入；可据真实机器日志补充经核实的进程白名单。不会点击“开启组合包”。

## 验证与上线顺序

```sh
windows/.venv/bin/python -m unittest discover -s tests/windows
RUN_PACK_OCR_TESTS=1 windows/.venv/bin/python -m unittest discover -s tests/windows -p test_pack_screenshots.py
```

先更新 Forge 服务端，再在两台 Windows Runner 当前任务结束后的空档更新 Controller。旧服务端尚不认识 `APEX_PACKS` / `PERFORMANCE_SUMMARY`，不能先更新 Controller。服务端只新增账号组合包字段和运行事件，不改租约内容。

更新后先检查两台机器各一个大厅访问的 `APEX_PACKS`、`NOTIFICATION_CANDIDATE` 和 `NOTIFICATION_CLOSED`，确认真实 HWND/进程与截图证据一致。累计 1–2 天日志后，按开荒/升级、Runner、leaseId 分组比较 EA 登录、动作执行、后置确认、状态等待、截图/OCR和上报耗时，再决定压缩哪一段。

当前自动关闭只启用于单显示器且捕获尺寸与主屏一致的环境；其他显示器拓扑只读取并记录遮挡，避免坐标映射错误。
