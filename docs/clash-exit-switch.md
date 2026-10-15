# Windows Runner 的网络出口：上报与 Clash 切换

## 出口上报（所有机器，无需配置）

每次 EA 登录前，以及租约运行中每 30 分钟，Runner 上报 Forge `POST /v1/runner/network-exits`：

- **系统代理**：Windows 系统代理开启时，经它访问 `ipinfo.io`（失败再用 `ip-api.com`），得到 EA App 这类程序出去的 IP。
- **直连**：不经任何代理。开了 TUN 时它也会被 Clash 接管；没开 TUN 时就是本机宽带 IP。
- **Clash**：本机 Clash 当前给游戏流量用的节点，以及对局中 Clash 连接列表里 `r5apex.exe` 的连接是否经过这个节点。加速器（如 UU）接管 Apex 时，Clash 看不到游戏连接，Forge 会提示。

只上报 IP、国家、地区、城市、ASN、运营商和 Clash 节点名，不上报代理地址或密钥。上报在后台进行，失败不会影响登录和游戏。

## 找到本机 Clash（无需配置）

Runner 依次尝试，用 `GET /version` 验证，第一个能用的就是本机控制接口：

1. Clash Verge 运行配置 `%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\clash-verge.yaml` 里的 `external-controller-pipe`、`external-controller-unix`、`external-controller`（附带 `secret`）。
2. 默认命名管道 `\\.\pipe\verge-mihomo`。
3. `127.0.0.1:9097`、`127.0.0.1:9090`。

只访问本机。游戏用的节点组取自规则表最后一条 `MATCH`（YToo 订阅是 `Final → Proxies → 节点`）。机器特殊时，可在 `windows/account-cycle.private.json` 写 `{"clash": {"group": "...", "apiBase": "http://127.0.0.1:9097", "secret": "..."}}` 覆盖，三项都可省。

## 按账号国家切换节点（Forge 设备设置开关，默认关）

开关在 Forge「设备详情 → 设备设置 → 按账号注册国家切换 Clash 节点」，随租约下发。关闭时 Runner 只读取和上报。开启后，换号时（上一账号已退出、新账号登录前）：

1. 用 `tasklist` 确认 Apex 没在运行；在运行或无法确认时不切换。
2. 在节点组及其成员选择组里，按节点名的国旗找账号注册国家（HK / US / JP）的节点。「到期」「流量」这类说明条目没有国旗，会自动排除。
3. 随机排序，刚用过的排到后面，在本机测延迟（`generate_204`，4 秒），用第一个通的，最多测 6 个。每台机器只会用到自己能通的节点。
4. 切换后核对当前节点，断开 EA App 和 Apex 的旧连接。

找不到 Clash、没有可用的同国家节点、或游戏在运行时，保持当前节点继续登录，并把原因上报。运行中的复查从不切换。

游戏 UDP 要经过节点需开启 TUN，并关闭会接管 Apex 的加速器。
