# 资源网站的API连接器

v0.120：已有固定JSON GET接口的网站可复用[原接口适配器](provider-json.md)，减少HTTP请求与响应检查代码，继续接到原ProviderOperations。授权和业务结果判断仍由网站提供，不支持任意URL、自动重试或自动收费。

v0.106新增[供应商持久操作账本](provider-operations.md)，可直接处理本协议的验签、原请求保存、一次派发与原结果查询。以下网络契约不变；网站仍提供真实授权器和业务执行/查询函数。

2026-09-23，v0.105，本项目原创。`api_provider.ApiProviderClient` 将已有 `ApiDeliveryHost` 的execute/lookup连接到供应商固定HTTP端点，减少每家重复实现签名、响应核验和未知恢复的工作。协议为本项目接入约定，不是支付宝或第三方通用标准。真实供应商仍需实现自身授权、持久防重和原操作查询；本连接器不收款、不自动上架、不签发权益。

## 分工与接入步骤

| 参与方 | 要做的事 |
|---|---|
| 资源网站 | 从原系统导出API商品清单，说明许可、输入、操作、次数、期限、结果大小；接入执行/原操作查询并持久保存结果 |
| 超市运营方 | 沿用批量上架与独立审核，核对权利、定价和宿主兼容性；配置每家独立的连接与密钥、市场权益授权器和固定次数账本 |
| 用户AI宿主 | 在任务入口先查货，固定原发行和用途，必要时按原账单购买；调用后保存数据及许可，继续制作用户成果 |

商品上架沿用[API商品契约](api-product.md)、[批量准备](supplier-batch.md)与[增量同步](supplier-sync.md)。连接配置不放进商品正文或模型参数；卖方自定价不等于审核通过或市场已能付款。测试许可不能复用为商业许可证。

可信宿主配置示意（参数均须来自已有获准配置）：

```python
from ai_supermarket.api_provider import ApiProviderClient
from ai_supermarket.api_delivery import ApiDeliveryHost

client = ApiProviderClient(
    provider_id, approved_https_endpoint, provider_connection_key,
    provider_binding_id, operations=[approved_operation], timeout=10,
)
host = ApiDeliveryHost(
    market=buyer_market, provider=provider_id, task_id=task_id, purpose=purpose,
    ledger=existing_quota_ledger, output_root=approved_output_root,
    binding_id=provider_binding_id, task_check=check_task_and_data_transfer,
    execute=client.execute, lookup=client.lookup,
)
```

provider_id须与商品api.provider一致，操作在固定清单内；同一提供方全部宿主共用原次数账本。不要每任务新建账本，不要把此密钥与支付、市场用户凭据混用。上述host再按[远程入口](api-remote.md)配置到MarketHTTP；已有purchase组合模式无需新增模型工具或修改数据结构。

## 网站端的固定协议

一个固定端点接收POST JSON，由action区分execute和lookup。必须HTTPS；数字本机IP可用HTTP作本地演练。客户端不使用环境代理、不接受跳转、不重试，也不允许模型覆盖URL。请求头为Content-Type与Accept: application/json，以及X-Market-Signature（请求原始字节的HMAC-SHA256）。连接密钥至少32字节，由可信双方保管；正常运行不得记录密钥、完整敏感输入或签名请求包。

执行请求的字段为：format=`market-provider-api-v1`、provider、binding_id、action=`execute`、operation_id、operation、input、issued_at、expires_at。时间为UTC Unix整数秒，有效期最长60秒；输入最多16KiB，请求总量最多20KiB。lookup请求只有共同字段，不带operation或input。operation_id由原次数预留产生，恢复沿用原值。

网站端可复用验签与响应封装：

```python
from ai_supermarket.api_provider import verify_provider_request, provider_response

request, fingerprint = verify_provider_request(
    bounded_raw_body, signature_header, provider_connection_key,
    provider_id, provider_binding_id, authorize_original_operation,
)
# 以下两个函数由网站实现，不是本库隐含提供的能力。
if request["action"] == "execute":
    evidence = durably_execute_once(request, fingerprint)
else:
    evidence = lookup_original_without_dispatch(request["operation_id"])
body, signature = provider_response(bounded_raw_body, provider_connection_key, evidence)
# 返回HTTP 200、Content-Type: application/json，正文body，
# X-Market-Response-Signature: signature。
```

`authorize_original_operation(request)`在执行和查询时均须查供应商认可的身份/连接与原操作授权记录，核对操作和数据范围，仅明确通过才返回True；签名只能证明持有连接密钥，不能证明已付款或有资源权利。可使用[原操作远程授权回调](provider-authorization.md)核对市场当前授权，再结合供应商自己的商品发行白名单；真实双方信任约定、账户映射和凭据分发仍须由有权主体落实，不能将演练中的固定授权替代生产规则。需要可复用的durably_execute_once实现时使用ProviderOperations.handle，它同时处理execute和lookup，避免重新编写同一状态机。

execute必须在供应商持久记录中以operation_id防重，保存fingerprint与状态；时间变化不改变fingerprint，同编号输入变化须拒绝。多进程/重启后仍须只有一次执行；不能只用内存字典。供应商进程在接单后退出或执行结果丢失时，lookup只能查原操作，不得重新派发。原站自身已有幂等编号时，必须贯穿传入原编号。

## 返回与恢复

| evidence | 含义 |
|---|---|
| `{"state":"completed","result":...,"evidence_ref":"original-record"}` | 原操作已完成，result最多64KiB，引用供应商原记录 |
| `{"state":"unknown"}` | 未知、仍进行中或查不到，保持次数占用 |
| `{"state":"not_executed","evidence_ref":"fenced-record","fenced":true}` | 仅lookup可返回；供应商已确认没有执行且阻止在途/排队工作今后执行 |

“查不到”不等于not_executed；fenced字段只是供应商签名声明，运营必须能追溯真实终止证据。连接器没有取消远端任务的能力。execute收到unknown会保留原次数占用，后续由原API恢复入口显式lookup。

响应封装绑定format、provider、binding_id、operation_id及完整请求SHA256，并签名原始响应字节；客户端先验签和范围再交结果给次数账本。错误状态、跳转、类型/压缩/长度不符、超限、签名或原请求不符都不会当作成功，也不会自动再执行；不回显服务器错误原文。默认10秒、最大30秒网络等待，读取阶段共享总期限；操作系统DNS解析的耗时仍受系统行为影响，不能把该值视为包含DNS的绝对墙钟上限。

完成后的数据沿既有ApiDeliveryHost/RemoteApiResources保存为结果文件、原许可证和清单，小摘要才返回模型。一次调用完成不代表整包API履约、支付宝收货、用户成果完成或真实费用对账。

## 本地验证与正式接入的差距

执行 `python -X utf8 -B -m unittest ai_supermarket.tests.test_api_provider -v` 可复现原创虚构供应商的本机HTTP交接。测试验证签名、原操作绑定、重复调用、丢响应后查询、拒绝跳转/异常响应、超时保留、授权拒绝及市场权益到结果/许可文件。

该测试不证明第三方签约、真实独立权益核验、生产TLS、分布式防重、支付或外部用户完成任务。真实接入还需供应商持久工作记录、可撤销授权、固定域名/运维配置、数据使用许可和双方认可的原操作查询方式；这些缺口不能通过把测试标记改为成功解决。故障时停用该连接，保留原订单、次数占用和操作记录，依据供应商原记录恢复。
