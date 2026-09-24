# 浏览器采集导入

浏览器或搜索工具读取动态招聘页后，将实际导出的UTF-8正文存入工作区 `evidence/`。记录URL、采集时间和文件SHA-256；岗位字段的文字应当能在该正文中找到。

`capture.json` 示例（虚构保留域名）：

```json
{
  "company_id": "example-tech",
  "source_id": "登记后的来源ID",
  "pagination_complete": false,
  "next_cursor": "实际下一页地址或游标",
  "pages": 1,
  "total": null,
  "evidence": {
    "url": "https://jobs.example.test/positions",
    "captured_at": "2026-09-17T10:00:00+08:00",
    "path": "evidence/browser-export.txt",
    "sha256": "文件的实际SHA256"
  },
  "jobs": [{
    "source_job_id": "源站职位ID",
    "title": "源站职位名称",
    "url": "https://jobs.example.test/positions/实际ID",
    "locations": ["上海"],
    "employment_type": "internship",
    "recruitment_type": "日常实习",
    "description": "源站职责原文",
    "requirements": "源站要求原文"
  }]
}
```

```powershell
python scripts/discovery.py --workspace tech-workspace import --file capture.json
```

`employment_type` 为 `internship`、`fulltime` 或 `unknown`。页面没有说明时保留未知；“校园招聘”属于通道，不等于实习。完整分页后才填写 `pagination_complete: true`。

由 `run_daily.py` 启动的Codex会话，将这些JSON放入工作区 `inbox/browser/`，由外层在会话锁释放后导入。成功导入的文件摘要记录在 `memory/browser-imported.json`，原文件保留。导入问题写入运行报告，可修正后再次处理。

个人资格逐字段核验另用 [data-contract.md](data-contract.md)。

## 新租户与新企业

每日会话发现新招聘入口时，将登记参数保存为 `inbox/sources/entry.json`，字段为 `company`（ASCII ID）、`name`、`sector`、`url`、`official_page`、`capture`（工作区中官方跳转页导出的相对路径）。外层先登记这些来源，再导入岗位；capture文件中需要包含企业名称和目标招聘URL。

同批岗位capture可用 `source_url` 指定新登记的入口，代替尚未生成的 `source_id`。采集原文仍需保存职位链接、城市、职责和要求。

## 样本标签评估

填写复核CSV时，保留sample_id与job_key，使用实际复核者和带时区时间。由程序核对的内容标为automated，由助手复核的内容标为assistant；human只用于真实人工填写的标签。`sample_review.py evaluate`会检查样本身份、标签完整性并输出分来源统计。
