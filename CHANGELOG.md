# Changelog

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
