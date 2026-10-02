# 资源站按原商品编号核对API收货

v0.110，2026-09-23，本项目原创。供应商可用既有卖方身份查询自己商品的历史逐次收货声明，并复用批量上架计划中的source_id映射回原站商品。查询不授权新调用、不确认套餐全部履约、不付款或结算。

## 机器接口

现有 `/v1/tools/call` 增加只读卖方工具 `seller_api_receipts`：

```json
{"tool":"seller_api_receipts","arguments":{"id":"vendor:stock","version":"1.0.0","after":0,"limit":20}}
```

需要有效卖方凭据，商品须属于该卖方。买方和审核角色没有此工具，不能传入他人卖方身份。普通买方工具数量不变。id/version必填，after默认0、limit默认20且最多50。历史查询不依赖买方凭据当前仍有效，也不重新检查或宣称当前付费权益有效；卖方自己的身份每次仍认证。

返回items中的receipt_id、供应商已知的operation_id、provider、发行/清单/结果摘要和确认时间；不提供买方主体、凭据编号、任务编号、用户输入、结果正文、订单或权益编号。evidence始终为authenticated_client_statement。以operation_id对应原业务操作，以receipt_id去重：同一操作可能由同一买方不同凭据各声明收货，不能按回执条数计费或当作调用次数。

has_more表示当次查询还有后续页，继续时传next_after。next_after是当前市场原账本的位置，只用于同一商品版本、同一市场的增量读取；不用于推算市场总量或业务次数。无新记录时位置不变，has_more=false只表示本次没有更多；未来新增声明仍需显式再次查询。分页不是跨多请求的一致快照，不承诺数据库重建/恢复后继续使用原位置。保留原账本，恢复由既有运营流程处理，不自动重建或重扫。

未收到客户端声明的调用不会出现在该列表中。空列表不能证明供应商没有执行、客户端没有收取数据、调用已退款或不需结算。商品暂停/下架后仍可核对既有历史声明；真正的资金、整包履约和退款须使用相应有权流程。记录与原使用事件/清单不符时拒绝返回，不自动修复。

## 复用原站source_id导出

先使用已有批量准备工具生成plan.json并保留原plan_sha256，参见[批量接入](supplier-batch.md)。无需重新上架或修改原商品：

```powershell
python -B tools/market_supplier_receipts.py --directory work/supplier-plan --expected-plan-sha256 PLAN_SHA256 --source-id original-stock --url https://market.example --credential-file PRIVATE_SELLER_CREDENTIAL_FILE --output work/stock-receipts-page1 --after 0 --limit 20
```

命令从已核验的原计划找到source_id对应的超市商品/version/provider；先核对远程卖方身份，再读取一页，保存receipts.json及不含秘密的连接绑定。网址和凭据路径由宿主固定，不能把凭据内容交给模型。每次最多两次请求（身份发现、读取），不调用模型、不自动翻页或重试。每阶段最多保留4次尝试，失败保留目录。返回错误或错卖方/错商品/错提供方/错分页/额外字段时拒绝保存。

下一页或新的增量观察使用新输出目录，after填上次next_after，保留旧导出快照。相同输出目录只允许相同连接、原计划、source_id和分页参数；文件冲突保留，不覆盖旧观察。原计划被修改先停止核对，不能靠换摘要冒称旧绑定不变。输出含source_id、seller、plan_sha256及市场页，接入方可据此对应自己的业务操作记录；本工具不修改原站数据库或发送回调。

## 实现和限制

服务端复用现有meta、usage_events、acquisitions和submissions，不迁移数据库；读取绑定的历史记录，仅投影上述必要字段。依赖运行环境现有SQLite JSON函数，在本项目运行环境及解压源码包验证。当前原型没有新增专门查询索引，大规模收货账本的吞吐和查询延迟未验收，不能据此承诺生产容量。实际供应商、独立网站信任和商业对账尚未联调。
