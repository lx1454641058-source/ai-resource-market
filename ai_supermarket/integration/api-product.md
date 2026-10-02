# API商品、原权益与本地交付

> v0.104已将采购与API调用组合到同一purchase任务，保留独立api模式，见[远程接入](api-remote.md)。下文描述商品与本地宿主契约。

2026-09-23，v0.102，本项目原创。正式卖方草稿、审核、搜索和详情现已支持 `kind=api`，使用v4契约。供应商的[次数账本](api-quota.md)可通过 `MarketApiAuthority` 核对真实市场获取记录，`ApiDeliveryHost` 将结果和原许可证保存给任务。旧文本、素材及原站文件格式保持兼容，无数据库迁移。

当前完整验证范围是本机工程链路，包括真实本机API网络，以及使用合成渠道证据获得的付费权益。没有真实钱包、支付或签约供应商。HTTP/MCP调用通过显式API宿主映射开放，默认关闭；v0.104已在同一任务中核验购买、取得权益、调用API和接收结果，仍不代表真实商业闭环验收。

## 上架声明

沿用[批量清单](supplier-batch.md)与[增量更新](supplier-sync.md)。商品正文仍是Markdown说明，许可原文约束结果的允许用途；正文不再作为API调用成果。新增字段如下：

```json
{
  "kind": "api",
  "usage_contract": {
    "version": 4,
    "delivery": "provider_api",
    "capabilities": ["invoke_api"],
    "permissions": ["network", "file_write"],
    "dependencies": [],
    "sample": {
      "input": "虚构仓库编号",
      "expected": "包含整数count的JSON",
      "check": "供应商样例与输出说明一致"
    }
  },
  "api": {
    "provider": "provider-example",
    "operation": "stock",
    "total_units": 100,
    "validity_seconds": 2592000,
    "result_max_bytes": 8192,
    "input_fields": [
      {
        "name": "warehouse",
        "type": "string",
        "required": true,
        "limit": 32,
        "description": "已获准传给该供应商的仓库编号"
      }
    ]
  }
}
```

这只是元数据增量示例，须连同已有必填的价格、用途、来源、许可和商品说明提交。示例次数与期限不是经营建议或授权。仍需独立审核，不允许供应商自行批准。

本版为单操作的预付次数套餐，售价是整包价格；每次新调用占1次。次数1至1,000,000，期限1秒至365天，从原权益生成时间起算；最长结果64KiB JSON。输入最多16字段，仅字符串、整数、布尔值。`limit` 对字符串表示UTF-8字节上限（最多8192），对整数表示绝对值上限（最多100,000,000），布尔值必须为1。拒绝额外字段、重复字段名、错类型、越界值及额外依赖费用。没有调用URL或凭据字段，供应商不能通过商品声明选择任意网络目标。

API商品不能混用origin、asset或rights字段。操作、输入条件、次数、期限、输出上限和许可都进入发行摘要；已批准条件变化必须提交新版本。改价仅影响后续新权益，不改原套餐与期限。

## 发现与获取

AI仍先search获取少量摘要，再resolve所选商品。API详情返回 `format=api`、`billing=prepaid_call_bundle`、完整输入声明及原发行摘要。宿主须具备 `invoke_api` 能力和 `network/file_write` 权限，商品声明不授予权限。

获取API必须传入resolve得到的 `expected_release_digest`。同主体、版本、用途的重复取用返回原acquisition_id，免费套餐也不会因为换请求编号生成新额度。原权益的期限不会续期；详情显示已拥有权益的到期时间，过期后提示停止，不自动续购。当前不支持加购次数或续费，不能把重复acquire当成充值。

收费新权益仍须经过既有可信宿主付款核验；普通acquire返回payment_unavailable，不会跳过付款。已有付费权益在每次调用时继续核对原支付记录。普通read返回 `host_transfer_required` 和 `trusted_host_invoke_api`，不写使用记录，不把说明正文或取得套餐说成已经调用成功。

## 本地可信宿主接入

```python
from ai_supermarket.api_delivery import ApiDeliveryHost

host = ApiDeliveryHost(
    market=authenticated_buyer_market,
    provider="provider-example",
    task_id=approved_task_id,
    purpose="personal_use",
    ledger=provider_private_ledger_path,
    output_root=task_output_directory,
    binding_id="approved-provider-connection-v1",
    task_check=check_active_task_and_provider_transfer,
    execute=provider_execute_with_bounded_io,
    lookup=provider_read_original_operation,
)
result = host.use(acquisition_id, {"warehouse": "fictional"}, "original-call-1")
```

以上对象和回调由可信宿主配置，不是模型参数。市场身份须先经过认证；`task_check(principal, task_id, provider)` 必须核对活跃任务、预算/执行范围及向该提供方传输输入的许可，返回严格True才通过。输入结构校验不能代替用户数据传输授权。网络端点、凭据、响应限制和超时由固定供应商适配器配置；此组件不提供任意URL代理或自动网络重试。

`MarketApiAuthority`读取原权益、发行绑定、付费来源、当前商品状态和宿主权限；只有API商品才能产生调用声明。提供方、操作、用途、输入和原权益必须一致。停售允许原权益继续使用，平台隔离停止使用；任务撤销、过期、许可/版本变化或付费记录损坏也会拒绝缓存读取。提供方是否有权销售和信任该市场账本，仍需真实供应商接入与审核，不由字段字符串自动证明。

宿主固定数据库、供应商连接编号、提供方次数账本、任务、用途和输出目录。每次先保存原调用绑定，调用后返回小型信息：状态、剩余/已用/占用次数、result.json路径、LICENSE.txt路径、清单路径和摘要。完整API结果不进入这份回复。文件以原子创建保存，原内容一致可复用，用户已有冲突文件保留、不覆盖。

文件保存并再次核验后才写入市场usage_events及原结果清单；它证明这次API结果已交给本机宿主，不证明业务内容正确或用户任务完成。此使用记录不会自动把整包次数视作已用完，也不自动发送支付宝履约或客户端收货声明。

## 未知状态与恢复

如果返回reserved，保留原acquisition_id、输入和request_id，通过同一宿主显式调用：

```python
result = host.use(acquisition_id, {"warehouse": "fictional"}, "original-call-1", recover=True)
```

它仅查询原供应商操作，不重新调用。原站成功但响应丢失，可取回原结果；本地写文件失败后重跑原use，复用供应商账本缓存，补齐文件。相同请求改输入或改权益将被拒绝。确定未执行及释放次数的条件见[次数账本](api-quota.md)，超时本身不是证明。

额度共享要求服务实例使用同一持久账本，不能新建数据库来重置次数。HTTP调用/查询和客户端结果接收已在v0.103接入；提供方独立部署、远程权益信任、钱包联调、续费与资金对账仍未完成。旧固定文件的ZIP通道不会将API说明误打包成动态结果。

任务初查与远程机器接口见[API远程接入](api-remote.md)。继续保持模型只选资源/传获准输入、宿主管身份/预算/网络、供应商独立核权益并返回结果的职责划分。
