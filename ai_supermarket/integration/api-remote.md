# 认证API调用、结果接收与任务入口

2026-09-23，v0.104，本项目原创。承接[API商品与本地交付](api-product.md)，提供默认关闭的机器路由、固定任务客户端和任务入口。配置匹配的采购与API宿主后，默认 `purchase` 模式可在同一任务中取得收费权益、调用API并接收文件；也保留独立 `api` 模式。没有模型调用、真实付款或公开部署。

## 服务端接入

v0.105新增[供应商固定HTTP连接器](api-provider.md)，可复用execute/lookup的签名、范围检查与响应处理。它不替代供应商独立授权、持久防重或真实支付。

可信运营程序先按既有流程配置身份、任务预算和 `ApiDeliveryHost`，再构造：

```python
app = MarketHTTP(
    market_database, identity_database,
    allowed_hosts=["approved-market.example"],
    budget_database=budget_database,
    enforce_task_budget=True,
    api_hosts={credential_key_id: {provider_id: configured_api_host}},
)
```

这是程序接入示例，不是修改生产配置的命令。映射只接受凭据编号，不接受Token。API宿主必须是对应市场、买方、任务、策略和提供方；底层原站执行器/查询器、网络超时、凭据、允许传输的数据由宿主配置。提供方的次数账本须持久且共享，不能为每个连接创建新账本。

不传api_hosts时所有新路由均关闭，原服务行为不变。启用时强制已有TaskBudgetGateway，沿用认证、明确Host、TLS（仅本机IP可HTTP）、请求大小、限流及并发保护。暂停/关闭预算、任务/父任务失效、撤销凭据或任务绑定都会阻止调用和结果读取。次数来自原预付权益，本版无额外依赖费用；这不是新增付款授权。

每次请求重新认证；提供方执行中及结果返回前也核对身份/任务。授权检查使用请求内临时闭包，不改共享宿主回调、不保存Token。`/v1/tools`在已配置买方的说明中提供 `resource_api`：独立status、task_id、purpose、providers以及routes。路径放入routes，避免查询路径与状态字段重名。

## 四个固定入口

均为POST，JSON正文仅包含：

```json
{
  "acquisition_id": "original-acquisition-id",
  "request_id": "original-call-id",
  "input": {"warehouse": "fictional"},
  "expected_release_digest": "原resolve保存的64位SHA256"
}
```

以上为字段示意，摘要占位符必须替换。完整正文最多16KiB，输入还须通过商品声明检查。任务、用途、买方、提供方连接、额度和目录由服务端推导，正文不能覆盖；不接受Payment-Proof。

| 路径 | 行为 |
|---|---|
| /v1/api/call | 首次执行原调用；重复请求复用原状态 |
| /v1/api/status | 只查询本地次数账本，不访问原站 |
| /v1/api/recover | 显式查询原站的原operation_id，不重发执行 |
| /v1/api/result | 只接收已完成的原结果；不存在或未知时不执行API |

reserved返回HTTP 202并保留占用；完成调用返回api_call_completed，且client_result_received=false。它不证明客户端拿到了结果。所有摘要响应都移除服务端路径和完整数据。

result入口返回有界JSON包，包含原结果、许可文本、清单、使用事件及输入摘要；不会把数据放进普通模型工具回复。客户端验证原商品/版本、任务、用途、权益、发行、许可和输入摘要，以及结果字节数/摘要后再保存。服务器先保存/验证文件，发送网络响应不等于收到客户端回执。

## 买方任务入口

先由宿主创建交付目录，再启动项目已有CLI：

```powershell
python -B -m ai_supermarket.remote_cli --url https://APPROVED-MARKET-HOST --credential-file /absolute/path/buyer.json task-mcp --task-id APPROVED_TASK --purpose personal_use --goal-file /absolute/path/goal.txt --output-root /absolute/path/results --mode api
```

这仍是显式接入的MCP服务（供AI调用的工具接口），不会修改全局客户端设置。任务/用途须与服务端绑定相同。模式启动核对resource_api并只做一次初查，保存原探测结果，重启不重复查货。模型获得以下流程：

1. 从initial_discovery选候选，必要时用market_search补查；market_resolve查看输入、许可和次数条件。
2. market_use取得免费或已有权益，保持原发行摘要；不能重新领取额度。
3. market_api_call传入已选权益编号、符合商品字段的紧凑input_json字符串及稳定request_id。
4. 收到api_result_received后，按返回路径读取所需数据，继续制作和检查用户成果。

未知状态用market_api_status先查；需要原站证据时显式market_api_recover；已完成但文件接收失败则market_api_receive。四个操作都复用原编号，不换编号掩盖未知调用。重启保留选品、权益、输入和次数上限。所有API网络尝试计入原任务max_calls，默认24次，查询和取结果也计数；另有客户端同上限的持久计数，不自动追加。

独立api模式不执行新收费购买或钱包操作。需要新购时，使用下述统一purchase入口；free_text模式仍只处理免费静态文本。不能把合成付款测试说成真实自主付费闭环已完成。

## 同一任务中的购买与API调用

可信宿主在同一个MarketHTTP上按同一凭据编号配置 `purchase_hosts={key_id: purchasing_host}` 和 `api_hosts={key_id: {provider_id: api_host}}`，两者须绑定同一买方、市场、任务、用途、身份策略与共享预算。调用上述CLI时把 `--mode api` 改为 `--mode purchase`（也可省略，默认purchase）。这是本地开发接入说明，不代表生产配置已变更。

首次发现核对两个宿主；API描述匹配时，在原6个采购工具上增加4个API工具，没有额外模型请求或发现请求。若没有API宿主或任务/用途不匹配，保留原采购工具，但API详情不会保存为可购买选择。API提供方须在首次发现范围内。已保存的旧purchase发现结果不自动增加工具，也不重查；需要新任务接入新增能力。服务端每次调用仍重新认证和核验范围。

AI流程为：初查 → resolve核对商品 → use取得权益或把原账单交宿主 → 宿主核验付款后回传权益 → market_api_call → 接收结果文件 → 继续制作用户成果。免费API与已有权益也走同一入口，无需另切模式。宿主专用的resume_payment仍不在模型工具列表，账单与支付凭证不进入模型工具参数。

购买交接只接受原发行摘要、原API声明和许可绑定，保存选品对应的权益。`next_action=market_api_call` 表示可以继续调用，`host_transfer_required`不冒称结果已经接收。采购编号与调用编号分别持久保存；支付未知使用market_purchase_status，并由可信宿主核对原订单；API未知使用market_api_status/recover，不重发执行或购买。重启和重复取用不重记金额或增加次数；调用次数耗尽不自动续购。所有网络尝试继续计入任务原max_calls。

API调用成功不自动生成整包履约或支付宝收货回执；market_receipt仅用于既有ZIP文件收货。真实钱包、供应商接入、API包履约标准仍未完成。

## 宿主SDK

已有AI宿主可直接组合 `RemoteApiResources`，无需再启动一个模型：

```python
client = RemoteApiResources(remote_market, task_id, purpose, output_root)
target = {
    "id": selected_id,
    "version": selected_version,
    "acquisition_id": original_acquisition_id,
    "release_digest": original_release_digest,
    "license_sha256": original_license_sha256,
}
result = client.invoke(target, approved_inputs, original_request_id)
```

target应由已核对的resolve与获取回执产生，许可摘要来自原许可UTF-8文本；不能使用模型编造的已付款/权益声明。客户端固定端点、凭据编号、任务、用途和输出根目录，发送前保存完整原意图。结果文件、LICENSE.txt、manifest.json和本地receipt.json使用原子创建，冲突文件保留。缓存也必须再次通过服务器认证，不能离线绕过撤销或有效期。

`status(request_id)`、`recover(request_id)`、`receive(request_id)`只需要原本地意图，模型无需再传完整输入；没有自动循环重试。客户端异常或网络未知仍保留原请求。固定端点不跟随跳转，不从代理环境自动选择路径。

## 已验证与未完成

本机HTTP已验证认证、任务范围、原权益归属、暂停/撤销、断线后原结果恢复、摘要与许可拒绝错配、额度耗尽、文件冲突保护；MCP子进程在项目外目录完成初查、选品、获取、调用和结果接收。测试全部使用原创虚构API数据，不含真实供应商或支付。

接入方采用此入口后，任务开始会优先查超市；无法保证没有接入的AI也会访问。小摘要与文件接收有工程证据，真实Token、时间和人民币收益仍待对照评估。实际供应商独立部署/信任关系、真实钱包、API包履约口径及退款分账对账仍需继续完成。

## 每次API结果的客户端收货声明（v0.108）

收到结果后，可使用原工具 `market_api_receive`，传 `request_id` 和 `action="receipt"` 确认本次收货。默认 `action="receive"` 保持原行为；`action="receipt_status"` 查询原声明。工具数量不变。宿主SDK对应 `client.acknowledge(request_id)` 与 `client.acknowledge(request_id, status_only=True)`，不自动附加调用。

客户端先重读原意图、绑定、结果、许可和清单，验证任务、权益、发行、输入及文件摘要，再发送请求。本地文件变更即停止并保留原文件。回执另存 `server-receipt.json`，不覆盖原 `receipt.json`。两种新动作各使用一个网络尝试槽位，计入API客户端和任务原上限；无模型额外调用。完整API结果不回传模型。

固定认证HTTP接口新增 `POST /v1/api/receipt` 与 `/v1/api/receipt_status`，由原 `resource_api.routes` 发现。请求为原四个字段 acquisition_id/request_id/input/expected_release_digest，加 usage_id/manifest_sha256。manifest_sha256 为按原 canonical 规则序列化清单的 SHA256。服务端只查询原调用账本和交付事件，不执行、恢复供应商、不生成新结果、不扣API次数、不付款。原凭据、预算、任务、权益和有效期检查保持；过期/暂停/撤销后该接口拒绝访问，历史审计需另走有权运营者的流程，不能以查询回执绕过授权。

服务端以原买方、预算根、任务、凭据编号和请求编号隔离记录，绑定原交付事件及清单、输入摘要；同请求重复确认返回同一回执和时间。状态查询尚未确认时返回 awaiting_api_receipt；记录成功返回 api_receipt_recorded。响应丢失后显式查询原回执；不换编号、不循环重试。旧服务端没有新路由时不可冒称确认成功。

evidence=authenticated_client_statement 仅表示已认证客户端声明收到了这次结果。恶意或有缺陷的客户端仍可能虚报，不能据此证明理解、任务质量或供应商整包履约。package_fulfillment_confirmed、channel_fulfillment_confirmed、task_completed、payment_executed 均为 false；不写订单整包收货指针，不发送支付宝履约或结算。运营者可通过原 acquisition_id/usage_id 关联已有证据；供应商可用自己的卖方身份[核对历史逐次回执](supplier-receipts.md)，真实商业对账仍未完成。
