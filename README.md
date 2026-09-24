# Tech Job Scout

面向公开招聘来源的岗位发现与求职台账工具。包含来源适配、增量采集、证据记录、资格核验、SQLite 状态和表格导出。

## 离线运行

使用 Python 3.10+：

```bash
python -m unittest discover -s tests -v
python examples/demo.py --output demo-output
python examples/discovery_demo.py --output tech-workspace/offline-demo
```

示例使用合成输入。实际 CLI 入口为 `scripts/discovery.py` 与 `scripts/scout.py`；命令详情使用 `--help` 查看。`SKILL.md`、`references/` 与 `templates/` 提供核心工作流规则。

私人画像、真实求职台账、采集日志和运行报告不随仓库发布。工具不自动投递简历或发送消息。

许可证见 [LICENSE](LICENSE)。
