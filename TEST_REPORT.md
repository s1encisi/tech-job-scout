# 验证报告

## 2026-09-16 本地复验

环境：Windows，Python 3.11.14。公开整理后的 **70 项离线测试全部通过**；其中 67 项为原有回归用例，新增 3 项检查公共画像不含已确认个人事实、无网络演示与已有目录保护。

```sh
python -m unittest discover -s tests -v
python -m compileall -q scripts tests examples
python examples/demo.py --output workspace/demo-verified
```

实际离线演示经过证据注册、来源登记、资格评估、SQLite 入库、重复观测跳过、伪造引文隔离和 Excel 导出。结果：1 个虚构岗位、重复写入新增 0、隔离 1、未核实入口 1、13 个工作表。机器可读结果见 [expected-summary.json](examples/expected-summary.json)。未执行真实检索，因此任务完成 0/24，状态为 `partial`。

Windows 子进程正常结束及超时清理已实测。首次在受限沙箱中执行时，系统拒绝 `taskkill` 清理测试自身的子进程；使用正常本机权限重新运行后全部通过，没有因此放宽 runner 的保护逻辑。

## 检查范围

| 领域 | 覆盖的行为 |
| --- | --- |
| 资格 | 未知不默认通过、明确不符优先、高分不覆盖硬条件、专业排除语义 |
| 证据 | 伪造引文、缺失引用、源文件篡改、过期或部分正文、采集时间异常 |
| 来源与状态 | ATS 租户边界、第三方来源降级、投递链接约束、404 不当作关闭 |
| 台账 | 幂等写入、岗位编号区分、URL 参数保留、旧观测拒绝覆盖、备份可读 |
| 导出 | 13 表 XML 结构、公式样文本、人工备注回收、坏结构拒绝覆盖 |
| 运行 | 配置漂移阻断、独占锁、断点大小、默认 dry-run、实际本地子进程超时 |
| 公共演示 | 模板个人事实全部 unknown、禁止网络时可运行、已有输出目录拒绝覆盖 |

CI 配置包含 Ubuntu / Windows × Python 3.10 / 3.12 四个组合，结果以 [GitHub Actions](https://github.com/s1encisi/env-ai-job-scout/actions/workflows/ci.yml) 的实际运行记录为准，不从配置存在推断通过。

## 历史证据

原始 v1.0.0 包记录了 Linux / Python 3.13.5 的 67 项测试结果，以及独立工作簿读取器的 13 表结构与渲染检查。原始输出保留在 `tests/offline-results.txt` 和 `tests/workbook-inspection.txt`；它们属于历史记录，不是本次 Windows 或远端 CI 的输出。

## 尚未验证

本次未执行真实招聘搜索、动态网站采集、长期连续运行、Windows 计划任务实际触发，也未在 Microsoft Excel / WPS 中进行 GUI 兼容性验收。未测量招聘召回率、资格判断的真实样本准确率、吞吐、模型调用成本或求职结果。

证据哈希只检验内容完整性，不能独立证明网站身份、工具返回或自然语言解释正确。检索预算目前主要由执行协议约束。没有注册定时任务、调用招聘登录接口、提交简历或调用付费模型 API。
