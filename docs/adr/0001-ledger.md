# ADR-0001：SQLite为权威台账，Excel为可重建视图
状态：本交付采纳；日期：2026-09-15。
问题：每日直接覆盖Excel会丢历史，聊天记忆不能支持稳定去重/恢复。
决策：保存sources/evidence/jobs/observations/events/runs/coverage/annotations；时间化观测、备份、版本检查；Excel原子替换并留快照。
代价：增加本地数据库；Excel派生列不能直接作为源数据回写；只保留人工跟进表的受控列。
反方案：只用Excel或长聊天，无法可靠表达并发、历史与证据关系。
边界：本地SQLite/哈希不是抗恶意本机管理员篡改机制；共享网络盘多写入者不支持。
