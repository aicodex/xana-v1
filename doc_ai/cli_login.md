---
title: cli_login
type: doc
status: active
---

# cli_login

`xana login` / `xana logout`：Kimi OAuth 设备码登录。实现在 `src/xana/cli/commands/login.py`，协议侧在 `src/xana/llm_driver/providers/kimi_oauth.py`（device flow 端点、`KimiTokenStore`、region 解析都归 llm_driver 的 kimi provider，cli 只编排）。

## device flow

kimi-code 同款设备码流程，全程无浏览器回调依赖：

1. `POST /api/oauth/device_authorization`（只带 `client_id`，form-urlencoded）→ 得到 `user_code` / `device_code` / `verification_uri_complete` / `interval`。
2. 打印授权链接和授权码，轮询 `POST /api/oauth/token`（`grant_type=urn:ietf:params:oauth:grant-type:device_code`）直到用户在网页确认。`authorization_pending` 继续等、`slow_down` 间隔 +5s、轮询期网络抖动不中断（截止 900s 内）；`expired_token` / `access_denied` / 超时 → `KimiAuthError`。
3. token payload 经 `KimiTokenStore.save_payload()` 原子写入 `~/.xana/credentials/kimi.json`（tmp + replace，chmod 0600；损坏 JSON 静默视为未登录）。
4. `GET /models` 拉服务端模型目录，把目录里的 **k3** 和 **kimi-for-coding** 登记进 `system.json` 的 `models[]`：entry 形如 `{"name": <id>, "type": "kimi", "api_key": "", "context_window": <服务端声明>}`，服务端未声明 `context_length` 时按 1M（1,048,576）处理；同名 entry 就地更新 type / context_window。`default_model.chat` 为空则设为 k3（无 k3 取首个已登记），`agent` 跟随。
5. 模型拉取失败不算登录失败：token 已写好，提示之后重跑 `xana login` 补登记。

网络边界收敛在 `LoginFlow`（`authorize` / `poll` / `fetch_models` 三个方法），测试用假实现替换即可，不碰真实 OAuth host。

## region

`--region` > `$XANA_KIMI_REGION` > 默认 `mainland-cn`。已知区域决定 oauth host 与 api base：`mainland-cn` → `auth.kimi.com` / `api.kimi.com/coding/v1`；`global` → `auth.kimi.ai` / `api.kimi.ai/coding/v1`。token 与 region 一起存进 `kimi.json`。

## 幂等与退出

- 已登录（`KimiTokenStore.load()` 非空）时直接提示"重新登录请先 `xana logout`"，不动现有凭证。
- `run_logout` 删除凭证文件；本就没登录则打印提示返回 0。
- `KimiAuthError` → rc 1；Ctrl-C → rc 130；成功 → rc 0。

## token 续期

token 到期刷新由 `KimiTokenStore`（llm_driver 侧）在使用时自动完成（`grant_type=refresh_token`，过期前 60s  margin）；刷新失败抛 `KimiAuthError`，消息引导重新 `xana login`。
