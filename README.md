# feishu-gcal-sync

把飞书日历单向同步到 Google 日历，部署在自己的 VPS 上。

## 方案

- **方向**：只做 飞书 → Google 的单向镜像，Google 侧只读。
- **飞书端**：企业自建应用，以用户身份（user_access_token）读取主日历。
  - 权限：`calendar:calendar:readonly`、`offline_access`
  - 授权一次后保存 refresh_token；它是一次性、会轮换的，每次刷新后必须先落盘。
- **Google 端**：Service Account 写入一个专用副日历。
  - 在 Google 日历里新建副日历，共享给 SA 的 `client_email`，权限设为"更改活动"。
  - 不写 attendees（SA 没有这个权限，也避免给参会人发邀请），参会人放进描述。
  - 提醒在自己账号里给这个副日历设置"默认通知"。
- **同步逻辑**：定期对账。
  1. 用飞书 `instance_view` 拉取时间窗口内的日程实例（重复日程已展开；单次查询区间须 < 40 天）。
  2. 用 Google `events.list` 拉取副日历同一窗口内的事件。
  3. 按飞书实例 ID 对齐，执行 insert / patch / delete。
  - 飞书实例 ID 和内容 hash 存在 Google 事件的 `extendedProperties.private`，除 refresh_token 外不需要额外状态。

## 待办

- [ ] 确认飞书自建应用能否在公司租户发布
- [ ] 飞书 OAuth 授权与 token 刷新
- [ ] Google SA 写入
- [ ] 对账同步主循环
- [ ] token 刷新失败时的告警
- [ ] VPS 部署
