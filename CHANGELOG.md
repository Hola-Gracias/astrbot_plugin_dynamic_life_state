# Changelog

## [0.6.0] - 2026-11-07

### Added

- WebUI 天气面板改为可编辑表格：每个时段可修改天气现象、气温、降水、降水概率和风力。
- 支持编辑今天和明天的天气。明天的手动天气保存到 weather_overrides.json，次日 00:00 生成日程时优先使用。
- 今日旧状态没有天气字段时，页面会显示自动生成的预览天气；保存后直接附加到当前状态，无需重新生成日程。
- 天气保存与日程保存分离：天气有独立的保存按钮；修改天气不会自动重排已有日程，但重新生成日程时会使用手动天气。
- 新增“恢复自动”按钮，可把今天或明天的天气恢复为内置季节模型生成的结果。
- 日期导航新增“明天”，明天没有日程时只显示天气预设和空状态提示。
- 天气修改有独立的未保存状态；切换日期时会提示。

### Changed

- Generator 生成某天日程时优先读取 weather_overrides.json 中的手动天气。
- 手动保存日程时保留当前天气；如果当天状态还没有天气但存在手动天气覆写，会自动附带。
- 插件版本号更新为 v0.6.0。

[0.6.0]: https://github.com/Hola-Gracias/astrbot_plugin_dynamic_life_state/compare/v0.5.0...v0.6.0

---

## [0.5.0] - 2026-11-07

### Added

- 新增逐时段天气：凌晨、早上、上午、中午、下午、傍晚、晚上、深夜共 8 个时段，包含天气现象、气温、降水概率和风力。
- 新增自然月季节模型：按国内温带季风气候的四季变化和月内上中下旬渐进调整气温与天气概率，11 月由偏凉逐渐转冷，12 月至次年 2 月为冬季。
- 天气先生成、日程后生成：日程 LLM 获得逐时段天气、气温和季节穿搭约束，雨雪天会带伞或安排室内活动，高温避暑，低温保暖。
- 新增季节穿搭检查：11 月等寒冷月份出现短袖、短裤、凉鞋等明显不合理的衣物时，自动追加修正要求并重试一次。
- 天气、季节和天气概览加入生活状态持久化，并显示在 /dls show、/dls full、LLM Tool、对话注入和 WebUI 每日日程页面。
- 提示词新增占位符 {weather} 和 {season}；自定义提示词缺少时自动追加天气与季节约束块。
- WebUI 每日日程页面新增天气面板、季节标签和每个时间线卡片的天气标签；手动修改日程会保留天气数据。

### Changed

- 默认提示词加入天气、季节和天气联动日程的硬性约束。
- 生活状态数据结构新增 weather_summary、season 和 weather 字段；旧状态缺少这些字段时保持兼容。
- 插件版本号更新为 v0.5.0。

[0.5.0]: https://github.com/Hola-Gracias/astrbot_plugin_dynamic_life_state/compare/v0.4.0...v0.5.0

---

## [0.4.0] - 2026-11-07

### Added

- 新增 WebUI 插件页面“每日日程”（pages/schedule/），用于查看今天与最近归档日期的完整生活状态。
- 页面支持手动编辑今天的节日、今日概况、今日氛围和时间线（时间、日程、穿搭，以及心情、地点、备注等可选字段），并支持添加、删除和排序时段。
- 新增 Web API：GET schedule/state 读取指定日期日程，POST schedule/save 保存今天的修改。
- 保存后立即写回当前生活状态存储，后续对话注入使用修改后的内容；同时保留原来的实际生成时间。
- 页面提供只读模式：历史日程仅可查看，生成进行中时拒绝保存，避免与自动生成互相覆盖。
- 页面跟随 AstrBot 浅色/深色主题，支持 Ctrl+S 保存。
- 在 metadata.yaml 中声明 pages，并新增页面 i18n 标题。

### Changed

- 保存手动编辑仅更新当天正在使用的状态，不修改归档历史，避免污染后续生成的参考数据。
- 插件版本号更新为 v0.4.0。

[0.4.0]: https://github.com/Hola-Gracias/astrbot_plugin_dynamic_life_state/compare/v0.3.1...v0.4.0

---

## [0.3.1] - 2026-08-06

### Added

- 指令和 LLM Tool 输出新增节日行：`/dls show`、`/dls full` 和 `get_full_dynamic_life_state` 在日期后显示 `🎉 节日：<节日名>`，非节日为“无”。
- 新增 `GenerationResult` 结果对象（`succeeded`、`state`、`error`、`used_previous_state`、`auto_retries_exhausted`），生成结果与操作结果分离，不再用 `LifeState(status="failed")` 表达操作失败。

### Changed

- 强制重新生成失败时保留当天原有成功状态：`/dls regen` 回复“重新生成失败，已保留原状态”并展示旧状态，不再用失败状态覆盖原本可用的当天状态。
- 自动生成失败不再持久化 `status == "failed"` 的标记状态。
- 每个自然日自动生成最多尝试 3 次，达到上限后自动路径不再调用 LLM，只能通过 `/dls regen` 手动重试；失败计数仅保存在内存中，插件重载（如修改配置触发 AstrBot 重载插件）后自动清零，`/dls regen` 不受次数限制且其失败不计入自动次数。
- `/dls show` 和 `/dls full` 在自动生成达上限时提示“今日自动生成次数已达上限，请使用 /dls regen 手动重试”。
- `fake_tool_call` 注入方式改为复用与 `extra_user_content_parts` 相同的 `<life_state>` 文本，两种注入方式内容完全一致，仅载体不同；移除了独立的 JSON payload 和 `current_*` 键。

[0.3.1]: https://github.com/Hola-Gracias/astrbot_plugin_dynamic_life_state/compare/v0.3.0...v0.3.1

---

<details>
<summary>📋 历史更新日志</summary>

## [0.3.0] - 2026-07-30

### ⚠️ 注意

本次更新修改了默认提示词与占位符，建议手动修改一下提示词。
可复制 README 中的提示词模板。

### Added

- 新增 `{holiday}` 提示词占位符，非节日或查询失败时为"无"。
- 新增状态 `holiday` 字段，并将节日名称加入临时上下文和 fake tool call 注入。
- 新增 `{timeline_optional_fields}` 提示词占位符，按字段注册表动态生成可选字段清单。

### Changed

- 将状态日期、每日生成任务和时间查询固定为北京时间（`Asia/Shanghai`）。
- 使用内置中国大陆 `CN` 日历，并固定以 `observed=False`、简体中文名称查询节日。
- 节日查询异常时记录警告并按"无"继续生成，不阻断主流程。
- 时间线可选字段（`mood`、`location`、`note`）改为注册表驱动：新增字段只需修改 `OPTIONAL_TIMELINE_FIELDS`，解析、持久化、历史参考、文本注入、fake tool call 和指令输出均按注册表动态渲染。
- 未知、空值和非字符串的可选字段会被丢弃，LLM 幻觉产生的未注册字段不再进入状态。
- 持久化和各注入模式只输出实际存在的可选字段；fake tool call 的 `current_*` 键按 `current_` + 原始字段名动态生成。

### Removed

- 移除时区配置、`{timezone}` 提示词占位符和状态中的 `timezone` 字段。

### Migration

- 旧状态缺少 `holiday` 字段时按"无"加载，已有的 `timezone` 字段会被忽略。
- 已存在的当天状态不会自动重写，需执行 `/dls regen` 或等待下一自然日生成。
- 自定义提示词仍包含 `{timezone}` 时使用当前内置默认模板，但不覆盖用户配置。

### Documentation

- 更新 README 的依赖、内置时区、节日占位符、注入格式和状态数据示例。
- 保持 README 可复制提示词模板与配置默认模板一致。

### Maintenance

- 新增 `holidays>=0.101,<1` 依赖。

[0.3.0]: https://github.com/Hola-Gracias/astrbot_plugin_dynamic_life_state/compare/v0.2.0...v0.3.0

---

## [0.2.0] - 2026-07-30

### Added

- 新增配置时区字段，状态日期、午夜任务和时段查询均使用该时区。
- 新增旧数据安全备份：旧版或无法识别的 `life_state.json` 与 `history/` 会整体移动到 `legacy_backup_<UTC时间戳>/`。
- 新增日期、时区、存储迁移。

### Changed

- 将生活状态统一为某个日期的全天状态，内部范围采用 `[00:00, 24:00)`。
- 将 `life_state.json` 和历史文件改为直接保存单个状态对象。
- 固定在配置时区每日 `00:00` 运行生成任务。
- 插件初始化或重载时不主动补生成；当天状态缺失时由午夜任务、首次对话或当天状态查询生成。
- 时段匹配改为当地墙上时钟的当天分钟数，具体区间优先于命名时段。
- 跨午夜区间只保留午夜前部分，例如 `23:00-02:00` 仅匹配 `23:00-24:00`。
- `/dls show HH:MM` 始终查询今天对应时间，完整状态指令和 LLM Tool 统一按日期查询。
- LLM 输出顶层字段改为 `date`，并严格校验其与目标日期一致。
- 提示词占位符改为 `{date}`、`{timezone}`、`{persona}`、`{history_states}` 和 `{extra_requirements}`。
- 注入文本、指令输出和 fake tool call 改为输出日期、时区与当前日期时间。
- `generated_at` 改为记录模型调用的真实完成时间。

### Removed

- 移除 `BusinessCycle`、`business_date`、`cycle_start` 和 `cycle_end`。
- 移除 `generate_time` 配置及业务周期计算逻辑。
- 移除午夜前时间映射到次日的查询规则。

### Migration

- 本版本为破坏性升级，不转换或拆分旧状态。
- 自定义提示词包含旧版占位符时，会记录警告并回退到新版默认模板，但不会覆盖用户配置。
- 备份失败时插件中止初始化，并保留或回滚原文件。
- 备份目录仅供人工查看或恢复，不参与新版历史查询或提示词生成。

### Documentation

- 更新 README 的配置、指令、存储格式和时间段说明。

[0.2.0]: https://github.com/Hola-Gracias/astrbot_plugin_dynamic_life_state/compare/v0.1.4...v0.2.0

</details>

---

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
并且遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
