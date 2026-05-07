# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，
版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [v0.0.9] - 2026-05-07

修复 cron job 在 DB 中累积、AstrBot WebUI "future task" 列表里堆一堆同名条目的问题。

### 修复
- `terminate()` 现在会显式 `delete_job(self._cron_job_id)`。AstrBot 的 `add_basic_job(persistent=False)` 仍会向 DB 写一行，且没有任何自动清理路径——之前每次 reload / 重启都会留一行残骸，长期累积。
- `_setup_cron()` 改为"无条件清理同名行 + 新建一条"。原先的"幂等复用"分支只在 `len==1 && cron 匹配 && enabled` 时复用，遇到老版本残留 / 配置漂移 / 中途取消时会落到 cleanup-and-rebuild，并依赖每次 delete 都成功才能收敛；任何一次失败都会留下残骸。新版本把"DB 同名行 ≤ 1"做成简单不变量。

### 变更
- 不再 reach 进 `cron_manager._basic_handlers` / `_schedule_job` 这种私有 API，全部走公开的 `add_basic_job` / `delete_job` / `list_jobs`。代价是每次 reload `job_id` 会变（WebUI 看到的 ID 变），但行数恒为 1、handler 始终绑在当前 self 上，进程重启 / 同进程 reload 都能正确触发。

### 兼容性
- 配置项不变。
- 升级后第一次 reload 会自动清理掉历史累积的同名 cron job。

---

## [v0.0.8] - 2026-05-02

修复 `recommend_album` 作为 LLM tool 调用时 120s 超时（[#1](https://github.com/10knamesmore/astrbot_plugin_daily_album/issues/1)）。

### 修复
- `recommend_album` 工具调用不再卡住 agent。AstrBot 对每个本地工具有 120s 硬超时，而推荐流水线（联网搜索 + LLM 抽取候选 + 文案生成）耗时常常贴近或超过这个上限。现在 tool 立即返回 ack 字符串，真正的推荐放到后台任务执行，由 sender 自己把专辑卡片送达会话。

### 变更
- 工具触发的推荐**会**写入会话历史（之前 `record_history=False` 跳过了，旧注释里"agent 会重复写"的假设并不成立——tool 返回 None 时 agent 历史里实际没有专辑信息）。后续被问到"刚才推荐的什么"时 LLM 能正确引用。
- `_run_recommend` / `_send_to_sessions` 改为通过 `target_sessions` / `prompt_override` / `sessions_override` 传参覆盖，不再临时 mutate `self.config`。这同时消除了 tool 后台任务可能并发污染 config 的隐患。
- 插件 `terminate()` 会取消正在跑的后台推荐任务，避免 reload / 关闭时留野协程。

### 兼容性
- 配置项不变。
- `/album_today`、cron 推送行为不变。

---

## [v0.0.7] - 2026-04-22

发送侧重构，支持 Telegram 平台；新平台后续可低成本接入。

### 新增
- Telegram 平台支持。`target_sessions` 现在可以混配 `telegram:PrivateMessage:<chat_id>` / `telegram:GroupMessage:<chat_id>`，TG 端会收到推荐文案 + 网易云直链卡片。
- 平台无关的 sender 抽象，未注册平台自动用纯文本兜底，至少保证文案能送达。

### 变更
- 推荐文案改为 **per-session 生成**。旧版固定使用 `target_sessions[0]` 的人格，多人格群只能共享同一段文字；现在每个 session 都按自己的人格生成。

### 兼容性
- `target_sessions` / `record_history` / `recommend_prompt` / `push_time` 等配置键不变。
- 旧的 aiocqhttp（QQ）UMO 行为完全等价，仍发网易云音乐卡片。
- 历史文件 `album_history.json` 结构不变。

---

## [v0.0.6] - 2026-04-22

让 LLM 记住每天推过什么。

### 新增
- 新增 `record_history` 配置（默认开启）。每次每日推荐发送成功后，会把这条推荐写入目标 session 的对话历史，方便后续 LLM 被问起时能引用。
- 写入历史的 user 消息以 `[系统标记：...]` 包裹，明确告诉 LLM 这是定时任务触发，不是用户实际发送。

### 变更
- `tool_recommend_album` 工具调用路径会跳过历史写入，避免与主 agent 自身的工具调用历史持久化重复。

---

## [v0.0.5] - 2026-04-22

修复定时任务在重启 / reload 时被多次注册的问题。

### 修复
- 插件 reload 或进程重启不再重复注册定时任务。原本每次启动都会新增一条同名 cron job，DB 中堆积多条，最坏情况下出现重复触发。
- 新版本会先复用配置一致的现有任务（无 DB 写入），不一致时全删后重建。

### 变更
- 完善全插件类型标注，对类型敏感的开发者更友好。

---

## v0.0.4 及以前

插件最初版本提供：

- 每天定时向配置的群 / 私聊推送一张专辑推荐
- LLM / 联网搜索 / 自定义脚本 三种推荐来源，可加权随机
- LLM 用当前会话人格生成文案
- 自动从网易云搜对应专辑发音乐卡片（aiocqhttp 平台），LLM 核验是否匹配（兼容 Deluxe / Remastered 等版本）
- 找不到时人格化生成"去其他平台手搜"提示
- `/album_today`、`/album_history` 命令；`@llm_tool("recommend_album")` 工具
- 推荐去重，避免短期内重复推同一张专辑
