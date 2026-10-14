# Windows Runner 的 Clash 出口切换

账号租约的 `exitCountry`（HK、US、JP）决定本机选择哪个节点。它表示注册出口的国家/地区，不要求重用注册时的 IP。没有 `clash` 配置时保持原行为。

将下面的 `clash` 对象合入该 Windows Runner 的 `windows/account-cycle.private.json`，节点名替换为本机 Clash 中的真实名称。私有配置和密钥不要提交。

```json
{
  "clash": {
    "apiBase": "http://127.0.0.1:9090",
    "secret": "填写本机控制接口密钥",
    "group": "游戏出口",
    "nodes": {"HK": "香港节点", "US": "美国节点", "JP": "日本节点"},
    "proxyUrl": "http://127.0.0.1:7890",
    "verifyUrl": "https://ipinfo.io/json"
  }
}
```

Clash 使用规则模式、开启 TUN、关闭系统代理和 IPv6；允许识别 Windows 进程（`find-process-mode` 不要设为 `off`）。`游戏出口` 应为可选择的节点组，节点需要支持游戏的 UDP 流量。控制接口和 HTTP/mixed 代理端口必须属于同一个 Clash 实例。

把以下规则放在订阅的通用分流规则前面。节点组名称必须与 `clash.group` 完全相同；更换探测服务时同时更换域名规则。

```yaml
rules:
  - PROCESS-NAME,EADesktop.exe,游戏出口
  - PROCESS-NAME,r5apex.exe,游戏出口
  - DOMAIN,ipinfo.io,游戏出口
  # 此处接原有的其他规则
```

Runner 按 API 返回的顺序检查规则，识别 `ProcessName` 等实际 API 类型，并跳过被禁用的规则。游戏和出口探测必须都指向所切换的组；更靠前的规则若可能把流量送到别处，就给出调整规则的提示。只知道进程名时，单独一条 `PROCESS-PATH` 不能证明其安装路径正确，补充上述进程名规则即可。

切换发生在退出上一个账号后、提交新账号登录前；已经登录当前账号时不切换。切换后只清理 `EADesktop.exe` 和 `r5apex.exe` 的旧连接，并通过指定的本机代理端口检查实际国家。控制密钥仅发送给本机控制接口，探测不携带密钥、不接受重定向，也不受 `NO_PROXY` 绕过。探测服务应返回包含 `country`/`countryCode`/`country_code` 和 `ip`/`query` 的 JSON；只返回两位国家代码的服务也能核对国家，但不能记录 IP。

EA 创建 checkpoint 会单独保存注册出口，后续绑定或 2FA 重试不会覆盖；该事实沿用现有注册任务诊断字段展示。游戏登录前只检查一次出口，并在 Runner 日志中显示本次国家和 IP。没有新增监控、数据库表或环境恢复流程。HTTP 探测 IP 不等于已观测到游戏 UDP 的 IP；缺失历史国家时沿用 HK 默认值。

本机测试验证接口和编排逻辑；Windows 首次启用仍需在自然换号时，核对 Clash 连接面板中 `EADesktop.exe`、`r5apex.exe` 的实际节点及 Runner 的登录、进局结果。配置检查与 HTTP 出口探测不能单独证明 Windows TUN 接管了所有游戏流量。
