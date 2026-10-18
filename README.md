# vyce-renew

vyce.cloud 免费 VPS（`Vps-Mini`）自动续期。免费机 24h 内只能续一次、每次续 2 天，忘续 2 小时后关机、关机满 3 天删除文件。

## 怎么跑

1. 优先用 repo secret `VYCE_SID`（`__Host-vyce_sid`，有效期约 14 天）直接调 `POST /api/servers/<id>/renew`。
2. cookie 失效（401）→ 自动走 GitHub OAuth 重新登录（Playwright 无头），GitHub 若要求设备验证码，脚本用 Gmail IMAP（`GMAIL_APP_PW`）自取 6 位码。
3. 续期成功/冷却期跳过都算成功；服务器不在账号下、登录失败等才 exit 1（GitHub 会发失败邮件）。
4. 重新登录拿到的新 cookie 会通过 GitHub API 写回 secret `VYCE_SID`（需要 `GH_PAT`，repo scope）。

## 触发

- `workflow_dispatch`：cron-job.org 每 6 小时打一次 GitHub API（原生 `schedule` 不 fire，只能当兜底）
- 24h 冷却期内调用返回 `429 renewal_cooldown`，属正常，脚本静默跳过

## 通知

- **只在真续期成功 / 失败时**推 TG（bot `@btppnode_bot` → chat `644320820`，不带 thread）
- 冷却期跳过、正常运行时**完全静默**（不会每天刷消息）
- 未配 `TG_BOT_TOKEN` / `TG_CHAT_ID` 就自动跳过通知

## Secrets

| 名称 | 内容 |
|---|---|
| `VYCE_SERVER_ID` | 服务器 id（`GET /api/servers` 里的 `id`） |
| `VYCE_SID` | `__Host-vyce_sid` cookie 值（可空，空则每次重新登录） |
| `GH_USER` / `GH_PW` | 登录 vyce 用的 GitHub 账号 |
| `GMAIL_APP_PW` / `GMAIL_USER` | 读 GitHub 设备验证码的邮箱 app password |
| `GH_PAT` | 用于回写 `VYCE_SID` secret 的 PAT（repo scope） |

## 相关

- 账号与机器信息：`btpp03/free-stack-inventory-hermes` → `ACCOUNTS.md` 第 21 条
- 这台机只有 IPv6 出网，SSH：`ssh root@<ipv6>`；面板自带 Web 终端可免 v6 进 root
