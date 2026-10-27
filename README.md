# feishu-gcal-sync

把飞书（Lark）日历单向镜像到 Google 日历。部署在自己的服务器上，由 systemd 每 10 分钟对账一次。

- 镜像标题、时间、地点、描述、会议链接、会议室和「在飞书中打开」链接；**不包含参会人和组织者**。
- 重复日程按实例展开。飞书上「空闲」的日程在 Google 上不占用时间，私密日程保持私密，已拒绝的邀请默认不同步。
- 只改动自己创建的事件，Google 日历上手动添加的事件不受影响。
- 授权失效、配置错误、接口故障通过 Telegram 告警，并支持心跳监控（如 healthchecks.io）。

## 适用范围

**支持**

- 一个飞书用户的主日历 → 一个 Google 副日历，单向。
- 飞书（feishu.cn）和 Lark（larksuite.com）的企业自建应用。
- 同步窗口：过去 `SYNC_PAST_DAYS` 天到未来 `SYNC_FUTURE_DAYS` 天（默认 7 和 60）。
- 飞书各版本的租户。默认每 10 分钟一轮，每月约 1.3 万次飞书 API 调用；基础免费版租户的所有自建应用共用一个月度上限，启用定时任务前先按「[调整同步间隔](#调整同步间隔)」调大间隔。

**不支持**

- 双向同步。在 Google 上改镜像的标题、时间、地点、忙闲或可见性，或者删掉镜像，下一轮都会改回来；描述的改动检测不到，飞书侧这个日程下次变化时才会被覆盖。
- 多个用户或多个日历，飞书里的共享日历、订阅日历。
- 参会人、附件、提醒设置。镜像事件按副日历的默认提醒（见「已知限制」）。
- 卸载清理。停用后已同步的事件留在副日历里，不需要了就删除整个副日历。

**窗口边界上的行为**

- 窗口外的新日程不创建，进入窗口后再创建。
- 日程改期到窗口外较远处（比如推迟到几个月后），镜像先删除，重新进入窗口时再创建；只推迟到窗口外一点的，镜像直接改到新时间。
- 已经过去的日程：结束时间早于窗口起点 1 天以上的镜像不再更新，也不会被删除，作为历史保留。

## 工作原理

每轮对账做这几件事：

1. 列出 Google 副日历里窗口内带 `src=fgs` 标记的事件。
2. 用飞书 `instance_view` 读取日程实例（重复日程已展开）。读取范围比窗口往前多 31 天，并延伸到能完整覆盖每个已有镜像；按 34 天分块查询，默认配置下通常每轮 3 次请求。
3. Google 事件 ID 由飞书实例 ID 经 uuid5 推导，按 ID 对齐后插入、更新、删除。

防误删：

- 飞书读取失败时整轮中止，不碰 Google。
- 飞书返回空、Google 上却有 3 个以上镜像时拒绝执行（确实清空了就用 `fgs sync --allow-empty`）。
- 只有当镜像的完整时间段都在本轮飞书读取范围内、却找不到对应日程时才删除。

飞书授权：

- 以用户身份读取日历，只需要 `calendar:calendar:readonly` 和 `offline_access`。
- refresh_token 只能用一次，有效期约 7 天，每次刷新都换新的。access_token 有效期约 2 小时，快到期时程序在一轮开头刷新，新 token 先写盘再使用，所以只要定时任务在跑，授权就一直有效。
- 两种情况需要重新运行 `fgs auth`：定时任务停了超过约 7 天（恢复后第一轮告警）；授权满 365 天（飞书强制，提前 `REAUTH_WARN_DAYS` 天开始每天提醒）。

## 准备

> 如果用的是公司租户，先确认公司政策允许把日历内容同步到个人账号。应用的 OpenAPI 调用会出现在管理员的审计日志里。

### 1. 飞书自建应用

1. 在[飞书开发者后台](https://open.feishu.cn/app)（Lark 用户在 [open.larksuite.com/app](https://open.larksuite.com/app)）创建企业自建应用，在「凭证与基础信息」里记下 App ID 和 App Secret。
2. 在「权限管理」里以**用户身份**（user_access_token）开通 `calendar:calendar:readonly` 和 `offline_access`。
3. 在「安全设置 → 重定向 URL」里添加 `http://localhost:8765/callback`。
4. 确认「安全设置」里的「刷新 user_access_token」开关是开着的（默认开启）。
5. 可用范围只选自己，创建版本并发布。管理员在管理后台给这个应用开了「免审」（或「自建应用免审规则」覆盖到它）时直接生效，否则要等管理员审核。

### 2. Google

1. 用个人账号创建一个 GCP 项目并启用 Google Calendar API。项目**不要挂在任何组织下**：2024-05-03 之后创建的组织默认禁止创建 Service Account 密钥。
2. 创建 Service Account，生成 JSON 密钥。
3. 在 Google 日历里新建一个副日历，共享给 Service Account 的 `client_email`，权限选「**做出更改并查看活动详情**」。不要选「做出更改（不公开活动显示为有空/忙碌）」，否则 Service Account 改不了私密事件。Google Workspace 账号的管理员可能禁止把日历共享到组织外，这时改用个人 Gmail 账号的日历。
4. 在副日历设置的「集成日历」里记下日历 ID，并按需设置「活动通知」和「全天活动通知」。

### 3. Telegram（可选）

1. 找 [@BotFather](https://t.me/BotFather) 创建 bot，拿到 token。
2. 给 bot 发一条消息，然后打开 `https://api.telegram.org/bot<token>/getUpdates`，找到 `chat.id`。

不配置 Telegram 时，告警只写日志。

## 配置

配置从 `--env-file` 指定的文件读取，默认是 `$FGS_ENV_FILE` 或 `./.env`。同名环境变量优先。模板见 [.env.example](.env.example)。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FEISHU_APP_ID` | 必填 | 自建应用的 App ID |
| `FEISHU_APP_SECRET` | 必填 | 自建应用的 App Secret |
| `FEISHU_BRAND` | `feishu` | `feishu` 或 `lark` |
| `FEISHU_REDIRECT_URI` | `http://localhost:8765/callback` | 必须和开发者后台登记的完全一致 |
| `GOOGLE_SA_KEY_FILE` | 必填 | Service Account JSON 密钥的路径 |
| `GOOGLE_CALENDAR_ID` | 必填 | 副日历 ID |
| `FGS_STATE_DIR` | `./data` | 状态目录：飞书 token、主日历 ID、告警记录、运行锁。用 `deploy/` 里的 service 部署时必须是 `/var/lib/feishu-gcal-sync`（service 只允许写这个目录） |
| `SYNC_PAST_DAYS` | `7` | 同步窗口往前的天数 |
| `SYNC_FUTURE_DAYS` | `60` | 同步窗口往后的天数。调大会增加每轮的飞书请求（读取范围每 34 天 1 次） |
| `SKIP_DECLINED` | `true` | 不同步已拒绝的邀请 |
| `REAUTH_WARN_DAYS` | `14` | 365 天授权到期前多少天开始提醒 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 空 | 告警发到 Telegram |
| `FGS_HEARTBEAT_URL` | 空 | 每轮对账跑完后 GET 这个地址，用来发现定时任务本身停了。Google 拒绝写入（哪怕全部被拒）、用量超限暂停写入的轮次也算跑完，这些情况另有告警 |

## 本地试用

```bash
uv sync
cp .env.example .env      # 填好配置；本地要删掉 FGS_STATE_DIR，GOOGLE_SA_KEY_FILE 改成本机的密钥路径
uv run fgs auth           # 浏览器授权
uv run fgs status         # 查看授权状态，检查 Service Account 对日历有没有写权限
uv run fgs sync --dry-run # 只打印计划
uv run fgs sync
uv run fgs test-alert     # 测试 Telegram 和心跳地址
```

| 命令 | 作用 |
| --- | --- |
| `fgs auth [--manual]` | 飞书授权。`--manual` 不启动本地回调，改为把浏览器跳转后的地址粘贴回终端 |
| `fgs sync [--dry-run] [--allow-empty]` | 执行一轮对账 |
| `fgs status` | 查看授权状态、Google 日历权限和未恢复的告警 |
| `fgs test-alert` | 发一条测试告警，并检查心跳地址 |

全局参数放在子命令前面：`fgs [--env-file <文件>] [-v] <命令>`，`-v` 输出调试日志。

## 部署（systemd）

### 服务器要求

- Linux + systemd，系统自带 Python ≥ 3.11（如 Debian 12、Ubuntu 24.04），git，[uv](https://docs.astral.sh/uv/)（装好后确认 `command -v uv` 有输出）。
- 能访问飞书开放平台、Google API 和 Telegram。中国大陆的服务器连不上 Google，用 feishu.cn 时选香港、日本、新加坡等境外机房即可：境外访问 open.feishu.cn 会解析到飞书的海外节点。DNS 用服务商默认的，不要改成国内公共 DNS。
- 不需要开放入站端口。
- 时钟要同步（`timedatectl` 显示 `System clock synchronized: yes`），Google 会拒绝用错误时钟签名的请求。

### 安装

所有 `fgs` 命令都以 `fgs` 用户运行（`sudo -u fgs …`）。`fgs auth` 和 `fgs sync` 用 root 运行会被拒绝，因为 root 写出的状态文件会让定时任务读不了自己的 token。uv 默认装在 `~/.local/bin`，不在 sudo 的 PATH 里，所以下面用 `sudo "$(command -v uv)"`。

```bash
sudo useradd --system --home-dir /var/lib/feishu-gcal-sync --shell /usr/sbin/nologin fgs
sudo install -d -o fgs -g fgs -m 700 /var/lib/feishu-gcal-sync
sudo install -d -o root -g fgs -m 750 /etc/feishu-gcal-sync

sudo git clone https://github.com/wongsean/feishu-gcal-sync.git /opt/feishu-gcal-sync
cd /opt/feishu-gcal-sync
sudo "$(command -v uv)" sync --locked --no-dev --compile-bytecode --python-preference only-system

sudo install -o root -g fgs -m 640 .env.example /etc/feishu-gcal-sync/env   # 然后编辑填写
# 先在本机执行：scp <密钥>.json <your-server>:sa.json
sudo install -o root -g fgs -m 640 ~/sa.json /etc/feishu-gcal-sync/service-account.json && rm ~/sa.json
```

- `--python-preference only-system`：用系统 Python。uv 自己下载的解释器在 root 的家目录下，`fgs` 用户读不到。
- `--compile-bytecode`：venv 属于 root，`fgs` 用户写不了 `__pycache__`，安装时先编译好。

### 授权

先在本机开 SSH 端口转发，再在服务器上运行 `fgs auth`，然后在本机浏览器里打开它打印的链接：

```bash
ssh -L 8765:127.0.0.1:8765 <your-server>
sudo -u fgs /opt/feishu-gcal-sync/.venv/bin/fgs --env-file /etc/feishu-gcal-sync/env auth
```

不方便转发端口时加 `--manual`。之后每年需要再授权一次，或者在收到授权失效告警时授权。

### 检查

```bash
alias fgs='sudo -u fgs /opt/feishu-gcal-sync/.venv/bin/fgs --env-file /etc/feishu-gcal-sync/env'
fgs status           # 授权状态、Service Account 写权限
fgs sync --dry-run   # 只打印计划，不写 Google
fgs test-alert       # 测试 Telegram 和心跳地址
```

### 启用定时任务

```bash
cd /opt/feishu-gcal-sync
sudo cp deploy/feishu-gcal-sync.service deploy/feishu-gcal-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feishu-gcal-sync.timer
journalctl -u feishu-gcal-sync -f
```

建议配置 `FGS_HEARTBEAT_URL`（如 [healthchecks.io](https://healthchecks.io)，周期 10 分钟，宽限 1 小时）。定时任务停了、服务器宕机、配置文件读不了，这些情况程序没法自己报警，只有心跳能发现。

同一份 token 只能由一个实例使用。refresh_token 只能用一次，两个实例（包括本地调试）会互相把对方的授权作废。换服务器时，先在旧机器上 `sudo systemctl disable --now feishu-gcal-sync.timer`，新机器部署好后重新 `fgs auth`。token 文件不用备份，需要备份的是 `/etc/feishu-gcal-sync`。

### 调整同步间隔

每轮计入飞书 API 调用量的请求通常是 3 次（授权接口不计），每 10 分钟一轮的话每月约 1.3 万次。飞书**基础免费版**里，同一租户的所有自建应用共用[每月 1 万次](https://open.feishu.cn/document/platform-notices/platform-updates-/custom-app-api-call-limit)的上限，用完后返回 `99991403`，到下月 1 号恢复。付费版没有这个上限。免费版请把间隔调到 20 分钟以上：

```bash
sudo systemctl edit feishu-gcal-sync.timer
```

```ini
[Timer]
OnUnitActiveSec=
OnBootSec=2min
OnUnitActiveSec=30min
```

空的 `OnUnitActiveSec=` 会先清掉原有的触发设置（连同 `OnBootSec`），不写的话原来的 10 分钟仍然生效。保存后执行 `sudo systemctl restart feishu-gcal-sync.timer`，心跳检查的周期也跟着改。用 drop-in 而不是直接改 `/etc/systemd/system` 下的文件，是因为升级时那个文件会被覆盖。

### 升级

```bash
sudo systemctl stop feishu-gcal-sync.timer
while [ "$(systemctl show -P ActiveState feishu-gcal-sync.service)" = activating ]; do sleep 5; done
cd /opt/feishu-gcal-sync && sudo git pull --ff-only
sudo "$(command -v uv)" sync --locked --no-dev --compile-bytecode --python-preference only-system
sudo cp deploy/feishu-gcal-sync.service deploy/feishu-gcal-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo -u fgs /opt/feishu-gcal-sync/.venv/bin/fgs --env-file /etc/feishu-gcal-sync/env sync --dry-run
sudo systemctl start feishu-gcal-sync.timer
```

先停定时任务并等当前一轮结束，再更新代码。

## 告警

| 情况 | 何时告警 |
| --- | --- |
| 飞书授权失效（过期、被撤销或已被使用） | 立即，需要运行 `fgs auth` |
| 接近 365 天授权上限 | 提前 `REAUTH_WARN_DAYS` 天开始，每天一次 |
| 飞书应用配置问题（权限、可用范围、刷新开关） | 立即 |
| 飞书租户本月 API 调用量用完（`99991403`） | 立即；下月 1 号恢复后发恢复通知 |
| Google 日历读不了（未共享、密钥无效） | 立即 |
| Google 拒绝写入（个别事件有问题，或共享权限不够） | 立即；心跳照常，其余事件照常同步。权限可用 `fgs status` 检查 |
| Google 用量超限 | 立即；之后 3 小时内只读不写 |
| 飞书返回空日程、Google 上却有 3 个以上镜像（防误删，本轮不做修改） | 立即；确认确实清空后，以 `fgs` 用户运行一次 `fgs sync --allow-empty` |
| 其他异常（程序报错、运行锁文件打不开） | 立即 |
| 配置文件写错 | 立即（要能读到 Telegram 配置和状态目录） |
| 网络错误、5xx、限流 | 持续 1 小时以上才告警 |
| 恢复正常 | 发一条恢复通知 |

同一个问题 12 小时内只提醒一次。

## 已知限制

以下行为依赖飞书和 Google 接口没有写明的细节，代码按下面的假设处理，尚未用真实数据确认：

- 全天日程：`instance_view` 对全天日程只返回 `date`、不带 `timestamp`，结束日期不含当天。两者都有时代码按定时日程处理；结束日期按不含处理，并保证至少 1 天。
- 跨查询边界的日程，`instance_view` 是按重叠还是按开始时间返回。代码对两种情况都做了容错：查询块互相重叠，往前多读 31 天，删除前要求镜像的完整时间段都查过。
- `instance_view` 是否返回 `self_rsvp_status`。如果不返回，`SKIP_DECLINED` 不起作用。
- Google：删除过的事件用同一 ID 重新插入会返回 409，代码改用 PUT 恢复它。
- Service Account 创建的事件按副日历的默认通知提醒。

其他：

- 跨过窗口末端、长于 3 天左右的日程（比如休假），以及一个多月前开始、仍在进行的日程，会把读取范围拉长，多花一次或几次 `instance_view` 请求。
- 描述按 HTML 转义写入，因为 Google 把描述当 HTML 显示。预约失败或已移除的会议室不显示。

## 开发

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

测试全部离线运行，不需要真实的飞书或 Google 账号。
