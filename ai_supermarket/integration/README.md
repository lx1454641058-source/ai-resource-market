# AI超市机器接入包

**当前接入先看[按角色选择入口](start.md)**：用户AI任务入口、独立供应商双端演练、资源站批量上架/增量同步和API收货核对。只读当前需要的说明。原站文件和API已有本地工程交付实现，真实资源站与支付仍未联调。下文保留较早入口的协议说明，历史试点数字不代表当前版本。

第一次做本地无模型核验，从[本地试用入口](local-trial.md)开始：一条prepare命令创建隔离样例，生成的START-HERE会用真实回执接续取用编号，不需要先配置HTTP服务或手工复制编号。已有本项目固定货架的任务用[项目接入说明](../../docs/ai-supermarket-project-connection.md)，不要重新建立空市场。下面按需要阅读高级接入说明。

新增[本机负载测量](load-probe.md)：仅在临时合成市场运行，区分短时搜索速度、完整工程流程、默认限流和账本正确性；不是生产容量或真实AI收益认证。

卖方可先用[离线准备助手](seller-preparation.md)生成内部演练请求，检查草稿/修订的字段、正文大小及使用契约。它返回短字段错误，不联网、不读取凭据或代替审核；已购权益、当前修订和真实账号仍须由服务端核对。

新增可选[同账本总预算](shared-budget.md)：跨根任务同时约束归属方及账本累计费用，含未知预留、并发、暂停和恢复；仅受信运营者可显式配置，不向模型增加管理工具，不替代真实身份或支付授权。

源码交付见[候选包说明](../../docs/ai-supermarket-package-guide.md)：构建、核对摘要后解压，再在独立环境验收；没有自动安装、部署或账户授权。运行包与个人总管原有便携包分别构建。

新增[独立服务进程恢复演练](process-recovery-drill.md)：覆盖落库后响应丢失、撤权/商品隔离后的重放拒绝，以及入驻、交付和四库恢复的组合链路。使用临时合成数据，未操作真实账户。

支付准备见[支付宝资格与交易验收材料](alipay-readiness.md)：包含候选资金路径、未发送的渠道询证稿和12项待执行用例；尚未接通付款、分账或退款。

已备[有限账户试点](pilot.md)：3个原创虚构任务×3组，最多12次模型调用；默认prepare仅生成审阅材料，真实execute需对应方案授权。14项试点专项通过，自动结构检查不替独立质量验收。

可选[远程任务预算绑定](task-budget-gateway.md)：运营将买方凭据绑定到获准任务，服务在静态资料read前核对共享预算；包含查询和撤销。依赖预留不授权或执行外部模型/工具，也不代表已付款。

整任务用量接入见[宿主计量说明](measurement.md)：搜索、资源与已有检查可自动记录。[既有Codex适配](codex-measurement.md)将模型调用与任务预留、用量及费用待核状态关联；本地替身验证不等于真实账户验收，缺数据不报成本收益。

已有[受控自助入驻](enrollment.md)：运营签发限定买方/卖方邀请，接入方在本机保存凭据后领取账号，再使用下述工具；响应丢失可凭原文件恢复。它不提供审核权限，不代表已开放无限制公众注册。

来源：本项目原创。支持本地标准输入输出、Python调用及[带凭据的HTTP服务与远程适配](http-service.md)，开放市场与旧目录使用不同账本。这里的配置不会自动安装；下文本地入口的`--principal/--role`是受信宿主参数，不是公网身份认证，远程身份由服务端凭据绑定。

**账本v2：**已修复程序检查与AI反馈不能共存的问题，并提供保留历史的更正。新建账本直接使用v2；已有v1文件会返回`upgrade_required`，须按[分源反馈与复制升级](feedback-evidence.md)生成新账本并核验后再切换，不能沿用旧文件假定自动升级。

**接口0.3.0：**商品草稿可暂缺使用契约，但提交审核必须提供`usage_contract`；审核增加`contract_checked`。买方resolve返回兼容预检，未满足条件时阻止新获取。接入方身份见[凭据网关](identity-gateway.md)；HTTP及适配器已经过本机网络测试，尚未公开部署或开放公众注册。

## 运行环境与入口

Windows本机使用已安装的稳定Python 3.12测试；默认`python`目前指向3.14预发行版本。以下命令从项目根目录运行。

```powershell
py -3.12 -m ai_supermarket.exchange_cli --principal buyer-example --role buyer config
py -3.12 -m ai_supermarket.exchange_cli --principal vendor-example --role seller config
py -3.12 -m ai_supermarket.exchange_cli --principal operator-example --role reviewer config
```

命令分别生成买方、卖方、审核角色的MCP配置，使用当前解释器和绝对脚本路径。宿主应只注册所需角色；不要把审核配置交给卖方。买方七工具、卖方八工具、审核四工具分开提供。新数据库默认在`work/ai-supermarket/exchange.sqlite`，不会读取旧目录、迁移旧账本或自动上架20项旧资料。

MCP是AI客户端调用工具的协议；客户端须支持本项目固定的2025-11-25标准输入输出协议。输出配置只证明配置已生成，真实客户端兼容需单独测试。

按客户端支持方式加载[买方技能](ai-supermarket-buyer/SKILL.md)或[卖方技能](ai-supermarket-seller/SKILL.md)。它们是待分发的项目文件，没有安装到全局技能目录。

## 连通诊断与直接调用

```powershell
py -3.12 -m ai_supermarket.exchange_cli --principal buyer-example diagnose
py -3.12 -m ai_supermarket.exchange_cli --principal vendor-example --role seller describe
py -3.12 -m ai_supermarket.exchange_cli --principal vendor-example --role seller call seller_draft --input work/draft.json
```

`diagnose`核对数据库及工具列表，不调用模型或付款；空市场正常返回0项。`describe`给出该角色的精确参数。商品草稿请求示例：

```json
{
  "slug": "project-brief",
  "version": "1.0.0",
  "metadata": {
    "title": "产品简报模板",
    "summary": "按目标与风险组织产品简报。",
    "kind": "template",
    "keywords": ["产品简报", "brief"],
    "input": "目标和已有事实",
    "output": "包含目标与风险的简报",
    "check": "检查目标和风险两节，并核对原始事实",
    "license_name": "LicenseRef-Local-Demo",
    "license_text": "本项目原创演示资料，仅用于本地个人链路演练；对外分发及训练用途另行确定。",
    "source_statement": "当前项目自行编写，无第三方素材。",
    "allowed_uses": ["personal_use"],
    "price_cents": 0,
    "usage_contract": {
      "version": 1,
      "delivery": "static_markdown",
      "capabilities": ["read_markdown"],
      "permissions": [],
      "dependencies": [],
      "sample": {"input": "为一个资料整理工具写简报", "expected": "包含目标与风险两节", "check": "两节均非空且不添加虚构事实"}
    }
  },
  "content": "# 产品简报\n\n## 目标\n填写目标。\n\n## 风险\n填写风险。\n",
  "request_id": "draft-example-1"
}
```

示例许可只适用于本地演练，不能据此公开出售。生产商品必须由实际权利方提供与用途一致的许可和来源证据。当前可提交个人/商业使用声明，训练用途未开放。

状态流程为草稿→待审核→批准/驳回；来源许可、样例、内容和契约实际检查后，审核方才能记录批准。`checks`必须含rights_verified、sample_checked、content_checked、contract_checked四个布尔值；批准时均须为true。审核勾选是声明，不是自动法律核验、恶意内容检测或独立质量保证。

批准后的买方链路：search → resolve核对compatibility → 条件满足后acquire → read → 使用 → feedback。个人用途资料须明确`purpose=personal_use`。付费商品在使用条件满足后仍返回`payment_unavailable`；不会生成付款成功、卖方到账或真实退款记录。

## 商品契约与受信宿主策略

`usage_contract.version=1`只交付静态Markdown；capabilities使用稳定能力编号，必须包含read_markdown。permissions声明需要的file_read、file_write、network或code_execution，不代表已授予这些权限。dependencies最多8项，每项为`id`、`license_name`、`max_cost_cents`；id建议包含准确版本，如tool-a:1，金额为每任务人民币分的额外成本上限，null明确未知、0明确免费。所有依赖上限相加比较，不仅看最大一项。

宿主默认只声明read_markdown、无附加权限、无已就绪依赖、额外预算0。受信接入者可使用`--host-policy`指定以下格式文件；配置不能来自模型参数或卖家正文，也不能代替实际操作系统/工具权限、付款授权和[共享预算预留](shared-budget.md)：

```json
{"capabilities":["read_markdown"],"permissions":[],"dependencies":[],"extra_budget_cents":0}
```

resolve返回ready、contract_missing、incompatible、permission_required、dependency_missing、cost_unknown或extra_budget_exceeded及缺失项。ready仅证明经过审核的声明与宿主策略匹配，不保证实际任务成功；execution_authorized始终为false。服务不安装或执行依赖。搜索保持短摘要，预检不增加买方工具数量。

旧已批准商品若没有契约会明确标contract_missing；卖方须用新内容版本补齐，不能覆盖已批准版本。新获取被阻止，已有权益仍允许受限的原资料读取；TaskResources在使用前重新预检，发现缺声明/撤权即停。普通停售的已购版本仍可通过预检使用，隔离版本不可读取。

## 任务前检查与恢复

Python宿主可以复用`TaskResources`，传入`Exchange`实例；该类在任务边界搜索，AI从返回候选中选择。开放市场默认商业用途，个人用途应显式传`purpose='personal_use'`，该用途会贯穿搜索与获取。原本地目录仍默认`project_local`，不会静默改变旧接口行为。

商品声明每任务额外依赖费用时，TaskResources还必须绑定受信TaskBudget；不绑定会返回shared_budget_required。它只准备预算并读取资料，不自动调用依赖；不替代真实付款或宿主权限。详见[主子任务共享预算](shared-budget.md)。

v0.64起，此助手对本项目Exchange自动绑定本次use预检的发行条件；条件与摘要从同一数据库快照读取，实际取用或读取时变化即停。搜索本身不锁定条件，外部选品摘要仍须显式传给取用或交付入口。旧未绑定请求重放时不会自动改号或升级；保留错误与原记录，按[发行绑定说明](resource-delivery.md)处理。

```python
from ai_supermarket.exchange import Exchange
from ai_supermarket.host import TaskResources

market = Exchange('work/ai-supermarket/exchange.sqlite', 'buyer-example')
task = TaskResources(market, 'task-001', '产品简报', purpose='personal_use')
# 将task.candidates交给AI选择；它可以不选。以下由AI返回实际候选ID/版本后调用。
# result = task.use(chosen_id, chosen_version, 'read-brief-1')
```

每个任务保存：任务编号、目标约束、当前用途、选择的商品版本、获取编号、读取请求编号以及待处理错误。上下文压缩后先恢复状态，已有权益不重购；没有新状态不要循环查询。技能提醒只是尽力触发，只有真正接入宿主任务入口才能验证覆盖率。

## 当前边界与发布要求

本地准入、免费交付、费用计算、HTTP身份隔离及远程适配可以验证；公众注册、正式HTTPS部署、支付渠道、多卖家结算、真实AI兼容、负载与备份恢复仍需后续完成。不将本地role/principal入口直接暴露为公网服务。项目商业规则、任务和上线门槛以[主计划](../../docs/ai-supermarket-master-plan.md)为准。
