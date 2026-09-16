# 方法与官方资料
核验日期：2026-09-15。网址可能跳转到产品新的官方文档站，使用原入口并记录实际落点。

1. Matt Pocock，The /grill-with-docs Skill（作者原文，2026-08-24更新）：https://www.aihero.dev/skills-grill-with-docs
   核验要点：对照文档质询、CONTEXT.md保存术语、ADR记录关键取舍；原版是对用户访谈。本包改为执行者文档自审，不声称原版就是自主自问自答。
2. Matt Pocock，源码：https://github.com/mattpocock/skills/blob/main/skills/engineering/grill-with-docs/SKILL.md
   当前版本委托grilling/domain-modeling；本包不复制其实现，也不依赖它已安装。
3. 与用户叫法一致的公开衍生版本：https://raw.githubusercontent.com/arthur-debert/dodot/main/.agents/skills/grill-me-with-docs/SKILL.md
   对照Spec、CONTEXT和ADR；仅用于核实术语，不安装其额外命令或照搬项目专属流程。
4. OpenAI Build skills：https://developers.openai.com/codex/skills/
   SKILL.md的name/description、可选scripts/references/agents、渐进加载、.agents/skills、$skill显式调用。
5. OpenAI Non-interactive mode：https://developers.openai.com/codex/noninteractive/
   codex exec、JSON事件日志、workspace-write、skip-git-repo-check。不要自动git init。
6. OpenAI Developer commands：https://developers.openai.com/codex/developer-commands
   --search启用实时web_search。运行前仍验证本机--help，不假定版本一致。
7. OpenAI Config basics：https://developers.openai.com/codex/config-file/config-basic
   cached/live等搜索模式不同；本包请求live，但正文仍必须实际读取。
8. OpenAI Scheduled tasks：https://developers.openai.com/codex/app/automations/
   技能可配合调度；本地项目运行需本机/应用具备执行条件；网页任务不能直接访问用户本地文件夹；CLI不提供Scheduled管理界面。
9. OpenAI Agent approvals & security：https://developers.openai.com/codex/agent-approvals-security
   最小权限；不因网络失败擅自开启danger-full-access。

本包工程设计（预算、30小时时效、三值资格、12行业矩阵等）是本任务选择，不是上述来源声明的行业标准。
本包代码为本次独立编写；参考方法使用概述，不分发外部仓库原文或字体。
