# feishu-gcal-sync

把飞书日历单向同步到 Google 日历，部署在自己的 VPS 上。

## 方案

- **方向**：只做 飞书 → Google 的单向镜像，Google 侧只读。
- **飞书端**：企业自建应用，以用户身份（user_access_token）读取主日历。
  - 权限：`calendar:calendar:readonly`、`offline_access`。
  - Token 接口用 `https://accounts.feishu.cn/oauth/v3/token`（v2 接口已标为历史版本）。
  - refresh_token 是一次性的，每次刷新都会换新的、旧的立即失效。新 token 先原子写盘再使用；刷新请求发出后没收到响应时，用同一个 token 重试一次。
  - refresh_token 的有效期以响应为准（目前约 7 天）。每轮同步时 access_token 快过期就顺带刷新，所以只要定时任务在跑就会一直续期。VPS 停机超过这个期限，就需要重新授权。
  - **授权满 365 天必须重新授权**，飞书强制要求。程序会在到期前 `REAUTH_WARN_DAYS` 天开始告警。
- **Google 端**：Service Account 写入一个专用副日历。
  - 在 Google 日历里新建副日历，共享给 SA 的 `client_email`，权限选「**做出更改并查看活动详情**」。不要选「做出更改（不公开活动显示为有空/忙碌）」，否则 SA 改不了私密事件。
  - 不写 attendees：SA 没有这个权限，也避免给参会人发邀请。
  - 提醒：在自己的账号里给副日历设置「活动通知」和「全天活动通知」，镜像事件使用日历默认提醒。
- **同步内容**：标题、时间、地点、描述、会议链接、会议室，以及在飞书中打开的链接。**不含参会人和组织者姓名**。
  - 飞书上标为「空闲」的日程在 Google 上不占用时间；私密日程在 Google 上设为私密；已拒绝的邀请默认不同步（`SKIP_DECLINED`）。
- **同步逻辑**：定期对账，默认每 10 分钟一次。
  1. 用 Google `events.list` 列出副日历里带 `src=fgs` 标记的事件。同步窗口默认是过去 7 天到未来 60 天，列的时候两边各多列 1 天，因为 Google 按日历时区判断全天事件，而窗口是 UTC。
  2. 用飞书 `instance_view` 拉取日程实例，重复日程已展开。拉取范围从窗口开始前 31 天到窗口结束后 1 天，如果有已有镜像超出这个范围，就再延伸到能完整覆盖它，另外留 1 天余量。
     - 往前多读 31 天，是为了覆盖已经开始的多日日程：飞书没说清 `instance_view` 是按"重叠"还是按"开始时间"返回结果。
     - 能完整覆盖每个已有镜像，开始于窗口之前的长日程被取消或拒绝后，才能确认它已经不在了。
     - 按 30 天切块查询（单次区间须小于 40 天），相邻块之间重叠 1 天。
     - 遇到 193103 或 193104（区间太长、实例超过 1000 个）就把区间对半再查。
  3. 用飞书实例 ID 经 uuid5 算出 Google 事件 ID，按 ID 对齐：
     - 窗口内新出现的日程会被插入。
     - 已有镜像只要还在本轮读取范围内，就会跟着飞书更新，哪怕它已经移出了窗口。
     - 读取范围内查不到、已取消或已拒绝的日程，镜像会被删除。日程被改期到读取范围之外（比如推迟到 60 天以后）也算查不到，镜像会先删掉，等它重新进入窗口时再创建。
     - 如果同一 ID 的事件之前删过，插入会返回 409，这时改用 update 恢复。
  - 飞书实例 ID 和内容 hash 存在 Google 事件的 `extendedProperties.private` 里。本地状态只有 refresh_token 和告警记录。
  - 防误删：
    - 飞书拉取失败时整轮中止，不碰 Google。
    - 飞书返回空、Google 上却有 3 个以上镜像时，拒绝执行。
    - 只有当镜像的完整时间段都在本轮飞书查询范围内、却查不到对应日程时才删除。
    - 同步只动带标记的事件，手动加的事件不受影响。
  - 在 Google 上手动改了镜像的标题、时间、地点或可见性，下一轮会被改回来。描述的改动检测不到，要等飞书侧这个日程有变化时才会被覆盖。
  - 描述按 HTML 转义写入，因为 Google 把描述当 HTML 显示。预约失败或已移除的会议室不显示。

## 准备工作

> 开始前先确认：公司政策是否允许把日历内容同步到个人账号？管理后台「日历权限」里的对外共享上限可以作为参考。另外，应用调用 OpenAPI 会出现在管理员的审计日志里。

### 1. 飞书自建应用

1. 在[开发者后台](https://open.feishu.cn/app)创建企业自建应用。
2. 在「权限管理」里开通 `calendar:calendar:readonly` 和 `offline_access`。
3. 在「安全设置 → 重定向 URL」里添加 `http://localhost:8765/callback`。
4. 可用范围只选自己，然后创建版本并发布。租户默认开启「小范围使用免审」时可以直接上线，否则需要管理员审核。
5. 确认「安全设置」里的「刷新 user_access_token」开关是开着的（默认开启）。

### 2. Google

1. 用个人账号创建一个 GCP 项目，**不要挂在任何组织下**：2024-05-03 之后创建的组织默认禁止创建 SA 密钥。然后启用 Google Calendar API。
2. 创建 Service Account，并生成一个 JSON 密钥。
3. 在 Google 日历里新建副日历，共享给 SA 的 `client_email`，权限选「做出更改并查看活动详情」。
4. 在副日历的「集成日历」里记下日历 ID，并设置「活动通知」和「全天活动通知」。

### 3. Telegram

1. 找 [@BotFather](https://t.me/BotFather) 创建 bot，拿到 token。
2. 给 bot 发一条消息，然后打开 `https://api.telegram.org/bot<token>/getUpdates`，找到 `chat.id`。

## 本地试跑

```bash
uv sync
cp .env.example .env     # 填好配置；本地可以删掉 FGS_STATE_DIR，默认用 ./data
uv run fgs auth          # 浏览器授权
uv run fgs status        # 查看 token 状态，检查 SA 对日历是否有写权限
uv run fgs sync --dry-run
uv run fgs sync
uv run fgs test-alert
```

## 部署到 VPS（systemd）

需要 Python ≥ 3.11 和 [uv](https://docs.astral.sh/uv/)。uv 默认装在 `~/.local/bin`，不在 sudo 的 PATH 里，所以下面用 `sudo "$(command -v uv)"` 调用它。

**所有 `fgs` 命令都要以 `fgs` 用户运行（`sudo -u fgs …`）。** 用 root 跑会被拒绝，因为 root 写出的状态文件会让定时任务读不了自己的 token。

```bash
sudo useradd --system --home-dir /var/lib/feishu-gcal-sync --shell /usr/sbin/nologin fgs
sudo install -d -o fgs -g fgs -m 700 /var/lib/feishu-gcal-sync
sudo install -d -o root -g fgs -m 750 /etc/feishu-gcal-sync

sudo git clone <本仓库> /opt/feishu-gcal-sync
cd /opt/feishu-gcal-sync
# 用系统 Python，避免 venv 指向 root 用户目录下、fgs 用户读不到的解释器
sudo "$(command -v uv)" sync --frozen --no-dev --python-preference only-system

sudo install -o root -g fgs -m 640 .env.example /etc/feishu-gcal-sync/env   # 然后编辑填写
sudo install -o root -g fgs -m 640 <密钥>.json /etc/feishu-gcal-sync/service-account.json
```

首次授权（之后每年一次，或收到授权失效告警时）：先在本机开一个 SSH 端口转发，再在 VPS 上运行 `fgs auth`，然后在本机浏览器里打开它打印出来的链接。

```bash
ssh -L 8765:127.0.0.1:8765 <your-vps>
sudo -u fgs /opt/feishu-gcal-sync/.venv/bin/fgs --env-file /etc/feishu-gcal-sync/env auth
```

不方便转发端口时，可以加 `--manual`：浏览器跳到打不开的 localhost 页面后，把地址栏里的 URL 粘贴回终端。

启用定时任务：

```bash
sudo cp deploy/feishu-gcal-sync.service deploy/feishu-gcal-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feishu-gcal-sync.timer
journalctl -u feishu-gcal-sync -f
```

建议配置 `FGS_HEARTBEAT_URL`（比如 [healthchecks.io](https://healthchecks.io)，周期设 10 分钟、宽限 1 小时）。定时任务停了、VPS 宕机、配置文件读不了，这些情况程序都没法自己报警，心跳是唯一能发现的办法。配置好以后运行一次 `fgs test-alert`，它会同时测试 Telegram 和心跳地址。

## 告警

| 情况 | 何时告警 |
| --- | --- |
| 飞书授权失效（refresh_token 过期、被撤销或已被使用） | 立即，需要运行 `fgs auth` |
| 接近 365 天授权上限 | 提前 `REAUTH_WARN_DAYS` 天开始，每天一次 |
| 应用配置问题（权限、可用范围、刷新开关） | 立即 |
| 配置文件写错 | 立即（尽力而为：要能读到 Telegram 配置和状态目录） |
| Google 写入失败、日历未共享或权限不对 | 立即 |
| Google 用量超限 | 立即；之后 3 小时内只读不写，以免拉长封禁时间 |
| 网络错误、5xx、限流 | 持续 1 小时以上才告警 |
| 恢复正常 | 发一条恢复通知（发送失败会在下一轮重试） |

同一个问题 12 小时内只提醒一次。定时任务本身停了是没法自己报警的，要靠 `FGS_HEARTBEAT_URL` 兜底。

## 上线前要用真实数据验证

- [ ] 全天日程：`instance_view` 返回的结束日期是否不含当天（代码按不含处理，并保证至少 1 天）
- [ ] 跨查询边界的日程，`instance_view` 是按重叠返回还是按开始时间返回（代码已做容错：查询块之间互相重叠，删除前要求镜像的完整时间段都查过）
- [ ] `instance_view` 是否返回 `self_rsvp_status`（不返回的话 `SKIP_DECLINED` 不起作用）
- [ ] Google：删除后用同一 ID 重新插入，409 后改用 PUT 能否恢复事件
- [ ] SA 创建的事件是否按副日历的默认通知提醒

## 待办

- [ ] 确认公司政策允许同步到个人账号
- [ ] 确认飞书自建应用能否在公司租户发布
- [x] 飞书 OAuth 授权与 token 刷新
- [x] Google SA 写入
- [x] 对账同步主循环
- [x] token 刷新失败时的告警（Telegram）
- [ ] VPS 部署

## 开发

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
