# 独立供应商的原操作授权查询

v0.111，2026-09-23，本项目原创。供应商可通过认证HTTP查询市场的原调用是否仍获准，不必读取市场、买方身份或次数数据库。它是可复用的工程接口，不代替真实供应商协议、业务/数据授权或支付渠道核验。

## 两方分别配置

市场继续配置固定ApiDeliveryHost和TaskBudgetGateway。市场HTTP的新调用在API次数预留后、供应商执行前保存不可变的原操作授权绑定，包括原买方凭据编号、预算根、任务、提供方/连接及原请求摘要；不保存Token。接口默认随API宿主关闭，只有配置api_hosts的MarketHTTP才开放 `/v1/provider/authorize`。本轮未改生产配置或发放真实凭据。

供应商配置自己的市场卖方凭据、固定市场HTTPS地址、已同意的商品/version/release_digest及业务后端。HTTP仅允许数字本机地址用于演练，不跟随跳转或自动使用环境代理。密钥和凭据由受信宿主保存，不进入模型工具参数。

```python
from ai_supermarket.http_client import RemoteMarket
from ai_supermarket.provider_authorization import RemoteProviderAuthorization
from ai_supermarket.provider_operations import ProviderOperations

market = RemoteMarket(fixed_market_url, seller_credential_file)
authorization = RemoteProviderAuthorization(
    market, "your-provider", "your-connection",
    {("your-seller:your-resource", "1.0.0"): approved_release_digest},
)
operations = ProviderOperations(
    provider_operations_database, "your-provider", "your-connection",
    provider_connection_key, authorization, execute_original_operation,
    lookup=lookup_original_operation,
)
# 在供应商固定入口使用 operations.handle(raw_body, request_signature)。
```

这里的卖方身份必须拥有原商品，供应商本地白名单必须匹配市场已核准发行摘要。新版本不会自动加入白名单。供应商若有额外地区、账号或数据用途规则，应在其authorize回调组合检查，只有双方都通过才返回True；不能将callback固定True替代这些约束。

## 核对范围和网络成本

已验签的供应商请求触发在线核对。查询包含provider、binding_id、operation_id、action及随机nonce；execute另含operation和input_sha256，不重复传用户原输入或结果正文。响应绑定本次完整查询摘要，并返回原商品/version/release_digest供供应商白名单检查；不发送买方主体、凭据、任务或原订单信息。

市场从原派发记录取出原买方凭据编号，重新核对当前有效性、任务/预算、宿主策略、原权益/付费来源、发行和API有效期、原次数记录、操作/输入和卖方商品归属。不能用另一把仍有效的买方凭据替代被撤销的原绑定。签名证明持有连接密钥；授权查询证明市场当时允许原操作；供应商白名单表示提供方同意该发行。三个条件不能相互替代，也不是新的付款证明。

RemoteProviderAuthorization不缓存决定，不自动重试。复用ProviderOperations时正常新执行会查询4次（进入、执行前、执行后、回复前），已有结果lookup通常2次；均为小型机器请求，无新增模型调用或模型工具。每次请求使用既有HTTP等待限制；多次查询与业务执行累计耗时仍需供应商及外层宿主控制，不能把单次超时当整个任务上限。

市场和供应商必须为回调预留并发容量。若所有市场槽位都被等待供应商的请求占满，授权回调可能收到busy；当前实现拒绝放行、保留原未知状态，不绕过限制或自动重试。正式容量、限流和生产TLS未验收。线上检查也不能撤回已发生的外部副作用；执行期间撤销会拒绝后续结果交接，不能声称远端工作已取消。

## 失败、恢复和历史兼容

凭据/任务撤销、预算暂停、商品隔离、API过期、范围不符、服务器不可用、响应不匹配或供应商不认可该发行均不能开始/继续交付。查询本身不预留新的API次数、不派发执行、不购买、不查询支付渠道、不结算。失败不等于not_executed，不自动释放占用。业务结果丢失时仍从原operation_id显式lookup，供应商持久记录负责防重。

原操作授权记录只由新HTTP派发路径产生，不能通过查询凭空创建。已存在但没有授权绑定的历史操作、直接使用ApiDeliveryHost的本地操作，或在预留与写授权记录之间崩溃的操作，不自动补造原凭据来源；保持未知并由受信宿主核对。旧供应商自己的授权回调仍可按其原协议使用，升级不自动更换。

本机试用包原先的共享库样例保持原行为；可使用[分目录双端启动工具](provider-pair.md)直接演练独立网络授权，无需自行编写组装代码。未擅自替换既有运行配置。实际资源站接入须由双方批准固定身份、域名、商品、许可和业务执行范围，再完成自己的联调。授权记录与逐次收货是不同证据；收货查询见[供应商回执](supplier-receipts.md)，包履约、退款分账和真实商业对账仍待完成。
