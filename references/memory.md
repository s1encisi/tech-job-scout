# 持久状态与恢复

- `state/jobs.sqlite3`：岗位目录、来源游标、增量事件、人工备注和资格台账。
- `memory/discovery-last-run.json`：最近一次采集报告。
- `reports/search-plan.json`：企业与用工类型队列，包括浏览器待办。
- `sources/registry.json`：从官方跳转新增的招聘源。
- `memory/browser-imported.json`：已导入浏览器文件摘要。
- `memory/checkpoint.json`：当前资格核验会话的简洁恢复索引。

恢复采集用 `collect --resume`；重新扫描列表首页不加该参数。跨会话首先读取来源状态与待办ID，需要正文时再读取证据文件。个人事实和策略保存在profile.json和policy.json，日常采集读取这些文件。
