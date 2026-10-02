# 供应商增量更新

2026-09-23，v0.100。本项目原创。复用[批量清单准备](supplier-batch.md)，先通过已认证卖方身份读取现有版本，再保存可审阅的差异；显式执行时沿用原修订号和请求编号。不自动删除目录外商品、不改审核决定、不发真实支付。

## 先预览

准备源清单后，保留prepare返回的plan_sha256：

```powershell
python -B tools/market_supplier_sync.py preview --directory work/prepared-supplier-batch --expected-plan-sha256 YOUR_PLAN_SHA256 --url https://YOUR-MARKET-HOST --credential-file /absolute/path/seller-credential.json --output work/supplier-delta --max-items 10
```

替换摘要、端点与身份路径为真实获准配置。preview只调用认证发现和seller_status，不上传商品正文，不改变市场；完整来源计划和比较用的摘要保存在本机。接入程序计算差异，AI只需查看小型结果，不必阅读整个目录。

预览目录绑定来源计划、卖方、端点和凭据编号。每次默认查至多10项（可设1至25），60秒后不启动新查询。首次取得的逐项状态固定保留；显式运行同一命令会补查尚未得到结果的项。状态读取失败、限流或身份错误不会被当成商品不存在；本次停止，无自动重试，每项最多4次尝试。

只有有效项全部核实后才返回preview_ready与delta_sha256，并生成delta.json；未完成时是preview_partial，不能执行。各项观察不是数据库同一时刻的全量快照。预览完成后需要新状态，应在新目录重新预览，保留旧证据；不能改旧差异或重新计算摘要绕过校验。

## 差异与执行边界

| action | 含义 | 显式apply的行为 |
|---|---|---|
| create | 指定商品版本不存在 | 创建草稿并提交审核；不自动上架 |
| unchanged | 正文、使用条件和有效售价一致 | 不写入 |
| reprice | 已批准且在售，仅有效售价变化 | 按原offer_revision改价 |
| submit_existing | 现有草稿与来源完全一致 | 按原revision提交审核 |
| revise_and_submit | 草稿或被驳回版本有实际变化 | 按原revision修订并提交审核 |
| waiting_review | 相同内容正在审核 | 不重复提交 |
| review_in_progress | 待审版本与来源不一致 | 等待审核处理，不覆盖 |
| review_rejected | 被驳回版本没有变化 | 保留驳回，不自动重审 |
| new_version_required | 已批准正文、许可或其他非价格条件变化 | 要求供应商提供新版本，不覆盖旧内容 |
| inactive_requires_operator | 已停售或被隔离 | 不改价、不恢复上架 |
| needs_changes | 来源行未通过离线校验 | 保留逐项错误 |

这里的“有效售价”来自当前offer，不来自可能保留原上架价格的metadata。正文与所有非价格字段参与比较；许可、用途、原站文件摘要等变化不会被误作单纯改价。字段顺序不构成变化，数组顺序仍按实际声明比较。

## 再执行

核对预览的目标、价格和操作后，使用保存的delta_sha256：

```powershell
python -B tools/market_supplier_sync.py apply --directory work/supplier-delta --expected-delta-sha256 YOUR_DELTA_SHA256 --url https://YOUR-MARKET-HOST --credential-file /absolute/path/seller-credential.json --state-directory work/supplier-sync-state --max-items 10
```

apply会向指定市场发送保存的新增、修订、提交或改价操作，只应在此对象和这些变化已获准时执行。该项目的本轮验证仅使用隔离本机数据，不代表授权操作外部真实卖方账号。凭据仍由可信程序读取，不进入模型上下文或结果。

执行固定原差异、端点、凭据、商品版本、修订和价格；不在冲突时重新读取后强行覆盖。预览后其他人改价或编辑草稿，服务端revision_conflict会停止该次执行。已有买方权益与批准正文不会因改价而改写；当前模块只调用原卖方接口，不自己操作数据库。

默认一次处理最多10个待执行商品、60秒后停止启动步骤；客户端单个在途请求仍可能等待至15秒。每步骤最多4次持久化尝试；未知、限流或业务拒绝停止，无自动重试。响应丢失后，显式重跑原命令沿同一请求恢复；成功步骤跳过，识别服务端replayed回执，不重复修订或再加一次价格版本。

| 状态 | 解释 |
|---|---|
| sync_partial | 可执行项尚未完成，核对interruption或继续原执行 |
| sync_needs_attention | 可执行步骤已处理，但仍有版本、驳回、审核冲突、停售/隔离或来源错误 |
| sync_applied | 计划内可执行步骤已获回执；不意味着待审商品已上架 |

结果是原预览与已保存执行回执，不保证当前市场状态，因此current_state_rechecked=false。需要最新状态时使用seller_status或新的预览；不要把历史成功回执当成当前售价证明。清单没有某件商品不会触发删除或下架。

## 验证与限制

已用原创数据和真实本机HTTP验证：新增、当前报价比较、草稿修订、待审/驳回保护、批准内容新版本提示、并发修改冲突、两个写步骤的丢响应恢复、分段执行、错误身份及旧权益保持可读。源码包从项目外目录独立导入，CLI也在项目外目录运行。

未调用真实供应商或钱包。可同步符合现有契约的API商品声明；API执行/结果交付、全量库存事件推送、自动撤回删除、真实商户及资金对账不在此模块内。它补的是既有资源网站目录的增量交接，不能证明整个AI超市已经商业可用。

API商品发生调用并取得客户端收货声明后，可沿用本批plan.json的source_id映射，使用[供应商收货核对工具](supplier-receipts.md)读取自己的历史记录；无需重提商品。该工具只读交接证据，不执行目录同步或资金结算。

API商品审核通过后，可使用[供应商服务configure步骤](provider-service.md)读取自己的指定商品版本，生成含原api和发行摘要的服务配置；无需从预览记录手抄或重新上架。来源清单中的source_id仍在原plan.json中，configure使用对应的id/version，不改变原映射。生成配置不等于市场连接部署或实际交付完成。
