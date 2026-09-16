# 数据契约与命令

所有路径以实际WORKSPACE/SKILL_ROOT为准。先运行--help；JSON必须UTF-8，时间必须ISO 8601带时区。
`templates/observation.example.json`是故意不能通过核验的结构示例，绝不能导入生产或计数。

## 核心命令
```powershell
python "$Skill/scripts/scout.py" --workspace "$Work" init
python "$Skill/scripts/scout.py" --workspace "$Work" begin
python "$Skill/scripts/scout.py" --workspace "$Work" plan
python "$Skill/scripts/scout.py" --workspace "$Work" evidence --run $Run --file $ActualCapture --url $ActualUrl --captured-at $ActualCaptureTime --method tool_export --tool-ref $ActualToolRef --completeness full_detail
python "$Skill/scripts/scout.py" --workspace "$Work" source --run $Run --file "$Work/inbox/source.json"
python "$Skill/scripts/scout.py" --workspace "$Work" lead --run $Run --file "$Work/inbox/lead.json"
python "$Skill/scripts/scout.py" --workspace "$Work" ingest --run $Run --file "$Work/inbox/batch.json"
python "$Skill/scripts/scout.py" --workspace "$Work" coverage --run $Run --file "$Work/inbox/coverage.json"
python "$Skill/scripts/scout.py" --workspace "$Work" checkpoint --run $Run --file "$Work/inbox/checkpoint.json"
python "$Skill/scripts/scout.py" --workspace "$Work" finalize --run $Run
```
调用run_daily.py时run_id已生成，不再begin/finalize。任何非零退出码都需查看原因，而不是删错误记录。

## evidence
实际正文导出，method只能browser_export/http_capture/tool_export/pdf_text；completeness为full_detail/partial/snippet/error。
不能用当前工具时间重标旧缓存；时间以实际获取正文为准。原始截图或PDF可另存并在报告引用，不当作文本文件直接登记。

## source精确字段
source_id、employer_key、company、kind、prefix、identity_evidence、authorization_evidence、reviewed_at、reviewer、ownership、ownership_evidence。
kind：official_company/official_ats/official_notice/government_notice/university_repost/third_party。
identity_evidence与authorization_evidence均为 `{ "evidence_id":"实际ID", "quote":"实际短原文" }`。
授权quote必须保留完整prefix URL，并来自身份可信的官网/正式公告跳转证据。shared ATS只登记具体企业租户路径/查询标识，不整站授权。
ownership不明填unknown，ownership_evidence=null；明确归类必须有真实股权/企业官方说明，不能从名称补编。

## observation精确字段
见example JSON。每个raw fact为null，或 `{ "value":"真实短原文", "evidence": {"evidence_id":"实际ID","quote":"包含value的原文"} }`。
job_type、match_track、industry和match_reason是有依据的分类/推断；不得标成官方原文。
job_id缺失填null，不自己编一个像官方编号的值。campaign必须是证据出现的批次；未知以unknown-cycle:具体来源标识隔离，不推测届别。
locations列表不拆分计数；同ID同时包含多地作为一岗。页面没有地点则空列表，不从公司总部推断。

### 投递链接
application_url为null或实际URL；有链接时必须有application_evidence，其结构为evidence_id/quote。URL必须原样出现在证据quote，或与该真实工具记录的最终页面URL一致，还必须落在已核实来源授权范围。有效官网域名不意味着可以猜测该域名下任意投递路径。无直接投递链接时保留未公开，不能自造。

### checks
字段固定axis/rule/expected/evidence。degree_program/major/graduation_year必需；其它已公开硬要求补充。
axis允许degree_program、major、graduation_year、student_status、experience_years、internship_days、internship_months、start_date、language、work_authorization、other。
rule：one_of（列表精确匹配）、min/max（数字）、on_or_before（ISO日期）、unrestricted（明确不限）、unpublished（未公开）、manual（语义歧义或不能自动判断）。
除unpublished外必须有evidence。unpublished的evidence=null。
在读master只对应在读学历项目；已获硕士学位、学信网、第一学历、学位证等复杂条件用manual/other，不把在读等同已毕业。
正式专业未知时，专业名单岗位不能pass；即使环境＋AI技能高度相关也一样。
expected中的英文标准枚举需能映射回确切中文原文；复杂“相关专业”或排除条件用manual，不强行枚举。
语言/证书/实习可用时间等未确认时unknown；不能编造熟练度或可到岗天数。
requirements_review保存完整硬要求是否复核、reviewer与真实tool_ref；不得自己填“已独立核验”冒充独立审计。

### 日期
deadline_at/opens_at为含时区ISO或null；有标准化截止必须有deadline_raw。
当前版本不完整自动解析自然语言日期；Codex应核对完整年份/总简章/时区并标注日末约定。
日期不确定填null并date_conflict=true，进入待核实；不要让空日期伪装无截止。

## coverage与checkpoint
coverage字段：task_id/industry/region/channel/query/result/tool_ref/pages_checked/next_cursor/notes。
plan提供固定父task_id。补充查询写notes，父任务done只能在其计划范围实际处理后填写。
checkpoint字段：phase/completed_task_ids/pending_task_ids/next_actions/blockers/evidence_ids；脚本自动补run_id和时间。

## 权威更新与导出
脚本只追加/更新SQLite和派生输出，不直接改画像；人工跟进只回收Excel“人工跟进”的前四列。
不支持在Excel主表修改职位事实、删除台账行或任意结构变更；需另行有据更新源记录。
历史不自动删，数据库备份每次保留。严格重复idempotent，疑似重复须审查，不提供破坏性自动模糊合并。

## lead：保留尚未证实的入口
单个JSON精确包含url/title_hint/company_hint/industry/found_at/tool_ref/reason。hint只保留搜索页真实显示的提示，不宣称官方事实；found_at带时区，tool_ref为实际工具引用。
没有完整正文时走lead，之后按URL关联正式岗位；原线索不删除。Excel“未核实入口”单列，绝不计入可投、已核实岗位或新增岗位。

## probe：仅记录本次访问结果
```powershell
python "$Skill/scripts/scout.py" --workspace "$Work" probe --run $Run --key $JobKey --status http_403 --detail '实际访问结果及真实工具引用'
```
status允许ok/http_404/http_410/http_403/http_429/timeout/login_required/captcha/parse_error/unknown。记录失败不会刷新last_verified，也不会推导关闭。
