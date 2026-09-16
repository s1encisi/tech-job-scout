# 开发与验证

核心 Python 模块只使用标准库。先阅读 `CONTEXT.md`、`docs/SPEC.md` 和相关 ADR，保持“未知不通过、无证据不升级、人工输入可保留”的行为。

```sh
python -m compileall -q scripts tests examples
python -m unittest discover -s tests -v
python examples/demo.py --output demo-output
```

演示目录已经存在时使用新目录名。涉及资格、证据、持久化或导出行为的修改，应加入能检验业务边界的回归用例。测试样本仅使用虚构公司、保留域名和明确的虚构画像，不抓取真实网站。

提交前检查 `git diff --check` 和暂存文件列表，确认没有私人画像、数据库、原始招聘正文、账号配置或本地运行产物。先暂存预期源码，再执行 `python scripts/manifest.py build` 并暂存清单；CI 会以 LF 规范化文本后检查哈希。

CI 使用 Windows / Linux 与 Python 3.10 / 3.12 的四个组合，执行编译、测试、演示和清单检查。设置参考 [GitHub 官方 Python CI 文档](https://docs.github.com/en/actions/tutorials/build-and-test-code/python)。真实搜索、付费调用、定时注册与投递不属于 CI。
