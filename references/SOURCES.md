# 来源与接口依据

本轮核验日期：2026-09-17。

| 来源 | 官方入口 | 使用的公开字段与机制 |
| --- | --- | --- |
| 腾讯 | https://careers.tencent.com/jobopportunity.html | `/tencentcareer/api/post/Query`的Posts、Count；`ByPostId`的职责与要求 |
| 网易 | https://hr.163.com/job-list.html | `/api/hr163/position/queryPage`的list、pages、total；职位详情query |
| 米哈游 | https://jobs.mihoyo.com | 官网脚本关联的`ats.openout.mihoyo.com/ats-portal/v1/job/list`与`job/info` |
| 字节跳动 | https://jobs.bytedance.com/experienced/position | 官网脚本中的`/api/v1/search/job/posts`契约；本轮HTTP 405，转浏览器 |
| 结构化网页 | https://schema.org/JobPosting | JobPosting JSON-LD字段与规范化职位URL |

网易用工类型0=全职、1=实习、2=派遣，来自官网`commons.c65656b8.chunk.js`的公开枚举。米哈游hireType 0=社会、1=校园，channelDetailIds=[1]，来自官网主脚本`umi.6f1adbea.js`及`3344.713d43a3.async.js`；列表和详情均以实际响应交叉检查。网站接口可能更新，适配器模式变化会反映到来源状态。

其余企业目录项是待处理入口，尚未逐家验证实时岗位。官方身份核验、动态网页和新增租户的处理见discovery.md。

Codex CLI使用本机`--version`和`exec --help`预检；本项目不绑定模型名称。CI配置参考 https://docs.github.com/en/actions/tutorials/build-and-test-code/python 。
