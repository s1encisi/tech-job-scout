# 环境＋AI 岗位每日追踪｜Codex Skill v1.0.0

**用途：让Codex真实检索岗位，核验公开资格，并在固定本地目录维护Excel、证据、历史和人工跟进。**
这是可安装技能＋Python脚本＋自审文档＋测试，不是一张已搜索完成的招聘表。

## 当前交付状态
已生成技能、画像模板/策略、112个未核实企业搜索词、台账/校验/Excel脚本、每日runner与Windows调度脚本。
未在你的电脑安装，未创建定时任务，未运行首轮真实招聘检索，未确认具体网站或你的本机Codex权限。
离线测试结果见TEST_REPORT.md；生产可用性必须经首次真实运行验收。

## 最快接入方法：交给Codex执行
解压后在Codex中打开本包目录，粘贴下方文本。Codex需要有真实搜索/网页正文能力、Python 3.10+及文件写入权限。

```text
请定位解压后的 env-ai-job-scout 目录并安装为本地Codex skill。
先检查实际目录；读取README.md、SKILL.md、docs/SPEC.md和TEST_REPORT.md。
采用用户级 ~/.agents/skills/env-ai-job-scout 或当前项目 .agents/skills/env-ai-job-scout；不要重复安装。
如目标已存在，只比较并报告差异，不覆盖。
在一个固定本地工作区初始化台账，所有个人事实默认unknown，按本人明确确认的信息配置，不能推断我的正式专业、毕业届别或实习到岗时间。
运行测试并检查实际Codex版本、实时搜索、浏览/正文抓取与写Excel能力。
完成一次受控的真实检索与Excel导出；说明真实核验数量和未完成事项，不凑数，不自动投递。
本次先不注册定时任务。首次验收通过后再按我指定的时间接线每日运行。
```

## Windows / PowerShell手动安装
先把压缩包解压为一个目录；下面命令从包含`env-ai-job-scout`的目录执行。
命令不初始化Git、不写远端、不改变Codex全局配置、不存登录凭据。

```powershell
$Source = Join-Path (Get-Location) 'env-ai-job-scout'
$Skill = Join-Path $HOME '.agents\skills\env-ai-job-scout'
if (-not (Test-Path (Join-Path $Source 'SKILL.md'))) { throw '请先定位实际解压目录。' }
if (Test-Path $Skill) { throw '目标技能已存在，请先比较；禁止直接覆盖。' }
New-Item -ItemType Directory -Force -Path (Split-Path $Skill) | Out-Null
Copy-Item -LiteralPath $Source -Destination $Skill -Recurse
$Work = Join-Path $HOME 'env-ai-job-workspace'
python "$Skill/scripts/scout.py" --workspace "$Work" init
python -m unittest discover -s "$Skill/tests" -v
```

在Codex CLI/IDE中显式调用：
```text
$env-ai-job-scout
在已初始化的固定工作区执行一次真实检索和增量更新。
```

## 每天运行的两种接线
### 首选：本地任务计划程序＋Codex CLI
CLI需要已经通过官方方式安装且登录；本脚本不读取或输出auth文件，不擅自发起付费API。
先预检：
```powershell
python "$Skill/scripts/run_daily.py" --workspace "$Work"
```
只会输出dry-run命令，不检索、不创建调度。首次真实运行：
```powershell
python "$Skill/scripts/run_daily.py" --workspace "$Work" --execute
```
首次验收通过后再设置每日时间。下面09:00只是示例，需改成实际希望时间；触发使用电脑本地时区，不等同招聘截止时区。
```powershell
# 只预览，不注册
& "$Skill/scripts/register_daily.ps1" -Workspace "$Work" -At '09:00'
# 确认机器时区和触发时间后，显式注册
& "$Skill/scripts/register_daily.ps1" -Workspace "$Work" -At '09:00' -Register
```
不自动放宽PowerShell执行策略；被本机策略拦截时由用户审核脚本与系统设置。
任务使用当前Windows交互登录会话和有限权限，机器需开机且网络/Codex登录有效；错过触发后按StartWhenAvailable补跑，不保证关机时执行。
只注册一次；已有同名任务拒绝覆盖。runner全程单实例锁，日志和失败报告保留。超时只结束自身启动的进程树；无法确认清理完成则保留锁，不导出、不并发重启。

### 备选：桌面产品的Scheduled任务
按当前产品界面选择固定本地项目并显式调用本skill。确保技能和持久工作区可访问，不使用每次丢状态的临时worktree。
需要本地文件的任务依赖电脑/应用实际运行条件；网页侧任务不能直接访问电脑文件夹。
CLI/IDE本身不提供Scheduled管理界面；本包没有伪造创建成功，也没有创建ChatGPT每日提醒来冒充本地Codex运行。

## 真正会生成什么
工作区结构：
```text
profile.json / policy.json       用户事实与规则（每日不改）
state/jobs.sqlite3               权威台账与历史
state/run.lock                  单实例锁
inbox/<run_id>/                  本批结构化观测
quarantine/                     证据或结构失败样本
memory/checkpoint.json           断点恢复索引
memory/last_run.json             上次运行结果
reports/<run_id>.*               日报、工具日志、实际能力
exports/环境_AI_岗位追踪_latest.xlsx
exports/环境_AI_岗位追踪_<run_id>.xlsx
backups/                        数据库/旧Excel备份
evidence/<run_id>/               原始证据文本与哈希索引
```
Excel共13表：总览、可投_已核实、资格待确认、待核实线索、历史与排除、未核实入口、新增与变更、全部岗位、人工跟进、来源注册表、检索覆盖、证据索引、字段说明。
只在“人工跟进”B:D写投递状态/优先级/备注；下次导出会回收保留。不要在派生主表改事实。
可投数只包含已核实开放且公开资格通过的相关岗位。未知届别/正式专业会使很多岗位留在待确认表，这是刻意的真实性边界。

## 环境与依赖
Python 3.10+；核心台账和便携OOXML导出只用标准库，无须给你电脑安装私有artifact工具包。
脚本不自带搜索服务；它编排实际Codex搜索和工具。没有真实正文保存能力则不能提供已核实新岗。
Codex参数按2026-09-15官方文档编写，但执行前读取本机--help；不兼容就停，不切危险权限。
默认不绑定具体模型名称，避免硬编码退役型号。模型选择、账户配额/计费遵循你实际Codex配置。

## 日常注意
无结果也要输出检索覆盖、阻断和状态，不能只报“今天没有岗位”。
对方网站可能下线或限流；来源核验、资格匹配和稳定运行都无法仅凭skill文件保证100%。
30小时时效、预算和评分是本项目工程选择，不是行业认证。规则修改需要用户批准并运行回归测试。
日期原文、抓取时间和标准化截止时间分开；不靠模型记忆修改画像，不把网页指令写入长期记忆。

## 资料
方法与Codex官方出处见references/SOURCES.md；自审过程的结论记录见docs/SELF_REVIEW.md。
