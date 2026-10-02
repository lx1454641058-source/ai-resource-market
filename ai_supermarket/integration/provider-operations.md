# 供应商持久操作账本

已有只读JSON接口可将[ProviderJsonQuery](provider-json.md)的execute作为固定后端。无原操作查询能力时不配置lookup，未知保留，不重新GET；已完成结果仍由本账本复用。

2026-09-23，v0.106，本项目原创。`ProviderOperations`接收[供应商HTTP协议](api-provider.md)的原始请求和签名，保存原操作并最多派发一次，在重启和并发时复用原记录。它独立于市场订单和API次数，不收款、不生成权益，也不会因为超时重新执行。

## 网站如何接入

使用网站已有HTTP框架读取有界原始正文和签名，配置一次：

```python
from ai_supermarket.provider_operations import ProviderOperations

operations = ProviderOperations(
    private_ledger_path, provider_id, connection_binding_id,
    provider_connection_key,
    authorize=check_original_authorization,
    execute=execute_backend_once,
    lookup=lookup_original_backend,
    require_existing=True,
)

# 每个HTTP请求：限制正文最多20KiB；禁止记录密钥、签名和完整敏感正文。
body, signature = operations.handle(raw_body, signature_header)
# 正常返回HTTP 200，Content-Type: application/json，
# X-Market-Response-Signature: signature，正文为body。
```

执行器签名为 `execute(operation_id, operation, inputs)`，返回有界JSON结果。查询器签名为 `lookup(operation_id)`，只读取业务后端原记录，返回协议中的unknown/completed/not_executed证据。lookup可不配置，此时未知操作始终保持unknown，需运营核对。网站HTTP框架负责监听、TLS、正文/并发限制、异常状态码及运维；模块不启动网络服务，不隐藏重试，不接收模型代码。

所有处理该连接的进程使用同一个持久本机SQLite文件，数据库固定provider和binding_id。不能每次启动新建临时库、复制空库恢复、为每个worker另建库，也不支持网络共享文件系统上的分布式运行。它只创建独立空库，已有其他表或绑定不符则拒绝，不迁移市场或用户数据库。备份和恢复必须保留原操作；不能丢弃未知记录来恢复“可用”。

v0.125：仅首次可信准备使用默认 `require_existing=False` 创建新库；网站启动/重启应使用上例的 `True`。现有配置式供应商服务、双端及API演练入口已采用此模式。缺失、空文件或无表的库不能作为重启后的新库；运行事务用SQLite现有文件模式打开，避免检查后文件被移走又自动新建。后端已派发但账本无法写回时，停止请求并保留未知；恢复原账本后也不能再次派发原操作。此机制不检测合法格式的过时备份替换，恢复时仍须核对原记录和上游事实。

不同连接的业务后端也须按连接区分operation_id命名空间。库的请求记录包含业务输入和结果，须按供应商的数据存储授权保护与保留，不对模型暴露数据库路径、密钥或完整账本。

## 原请求、执行与恢复

| 供应商账本状态 | 网络结果 | 行为 |
|---|---|---|
| 无记录，首次execute | 先写reserved，再执行 | 保存原字段及不含时间的fingerprint，事务提交后仅创建者派发 |
| reserved | unknown | 重复execute不再派发；lookup只查原业务后端 |
| completed | completed及原结果 | 校验结果摘要后复用；重复lookup不访问业务后端 |
| not_executed | lookup返回not_executed和fenced | 原请求终态，不因重试execute再次执行 |
| 无记录，lookup | unknown | 不创建记录，不查询或派发后端工作 |

同编号更改操作、输入或范围会拒绝；刷新时间戳不会改变原输入绑定。数据库锁只覆盖短事务，不在执行后端期间持锁。进程在保存reserved前退出，没有本模块派发；保存后退出，即使尚未派发也不会自动再发，因为另一方无法据此证明业务未执行。该设计保证本模块对原操作最多派发一次，不保证所有崩溃场景最终完成，也不承诺业务副作用的分布式“恰好一次”。

执行器异常、返回无效/超限JSON或执行期间授权撤销时，原操作保留reserved。若后端已完成但本地提交/网络响应失败，lookup核对后端原记录后补齐原结果；若已提交完成记录则直接返回缓存。任何异常原文都不作为供应商网络结果返回。存储异常由网站按未知请求处理，保留原文件核对。

授权在进入、派发前、执行/查询后及返回前重新核验；缓存也不绕过撤销。授权器必须按实际供应商合同、认可的连接/原操作权利和数据范围判断，不接受请求中的paid声明，不能用`lambda request: True`作为真实供应商授权。请求签名只能证明连接凭据，不能证明市场付款；本模块不解决双方原权益同步和独立信任约定。

## 何时允许确认没有执行

lookup只有从真实业务后端确认该原操作未执行、且已经阻止所有在途/排队/未来同编号工作，才能返回：

```json
{"state":"not_executed","evidence_ref":"backend-fence-record","fenced":true}
```

本模块保存该终态并禁止自身后续重发，但不能终止已经交给外部后端的工作。fenced必须对应后端有效的终止/拒绝机制，不是调用方填一个true即可成立。缺字段、fenced=false、查不到、超时或非法结果都保持unknown。测试中的权威证据为原创夹具，不是生产证明。

若并发查询与执行给出不一致终态，保留先保存的记录并返回settlement_conflict，不覆盖原结果或自动放弃费用。运营必须查真实后端证据处理；代码不能从两个矛盾声明推断哪个业务事实为真。

## 交付边界与验证

供应商结果通过ApiProviderClient回到原次数账本，再由API宿主保存JSON、原许可及交付清单，用户AI继续完成成果。操作完成不等于API包全部履约、支付宝收货或任务验收；资金与次数仍分别对账。

`python -X utf8 -B -m unittest ai_supermarket.tests.test_provider_operations -v`覆盖两个真实进程共享记录、进程退出、并发请求、重启缓存、原请求冲突、授权撤销、错误恢复证据、结果损坏、响应丢失与本机HTTP交接。测试仅使用原创虚构数据。本模块与说明包含在内部源码候选包，未公开部署、未接入真实网站、未验证供应商生产集群及实际支付。
