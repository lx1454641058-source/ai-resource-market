# 查询原退款状态

v0.118，2026-09-23，本项目原创。`AlipayRefundQueryClient`供可信商户宿主核对已由其他获准渠道提交的退款，不发起退款、分账或转账，不进入模型工具表。

## 渠道依据与适用范围

使用`alipay.trade.fastpay.refund.query`。官方说明：`10000`只说明查询成功，须检查`refund_status=REFUND_SUCCESS`；应沿用原退款编号，金额字段单位为元，建议发起退款后等至少10秒再查。[官方退款查询接口](https://aipay.alipay.com/docs/vibe-pay/ai-web-app-payment-qianyi/api-list/alipay-trade-fastpay-refund-query.html)

该页面属于AI网页应用收款，不能由此认定Machine Pay商户自动有资格。真实商户须确认本退款产品、应用/商户关系和权限；`capability_reference`只是宿主保存的依据编号，不是程序签发的资格。当前仅支持既有普通RSA2公钥配置，不支持代商户app_auth_token或证书模式。

## 可信宿主接续

以下变量来自原商户宿主，不能用模型或商品内容提供的连接替换：

```python
from ai_supermarket.alipay_refund_query import AlipayRefundQueryClient

queries = AlipayRefundQueryClient(
    original_payment_fulfillment,
    original_alipay_verification_transport,
    capability_reference=approved_refund_query_contract_reference,
    network_enabled=False,
)
record = queries.history(original_order_id, original_refund_id, refund_cents=30)
```

history只读本地状态。真实查询须宿主在已有具体授权及资格范围内显式启用network_enabled，再调用`query(original_order_id, original_refund_id, refund_cents=30)`。这里30是人民币分；请求金额从原退款审批/提交记录取得，不允许AI推测。原付款必须已确认，查询身份必须是原买方订单宿主，商户、环境、签名/验签密钥与原确认策略一致。退款金额限1分至原实付金额。

固定请求只包含原trade_no、out_trade_no和out_request_no，签名方法固定为退款查询；不能传任意接口或账户。成功响应须通过原始字节RSA2验签，三个编号、总金额、退款金额必须全部匹配；金额按元字符串精确转换为分，不接受浮点或科学计数法。额外返回主体或币种时也核对，错误文本及私人明细不写普通记录。

## 状态与恢复

| 状态 | 含义与下一步 |
|---|---|
| not_queried | 尚无原编号查询，不能推断退款未发生。 |
| reserved / unknown | 已保存查询意图或未收到可判定结果；保留原记录，宿主可再次查询原编号。 |
| unconfirmed | 已验签但未得到匹配的退款成功状态，包括查询成功但缺退款状态；不能宣告资金失败或自动重退。 |
| invalid_response | 验签或订单/金额不符；核查原渠道，不能据此记成功。 |
| test_response | 本机回环返回匹配样例，refund_confirmed仍为false。 |
| confirmed | 固定HTTPS渠道返回了匹配成功状态；随后由宿主核对权益及结算。重复query直接返回原观察，不重新联网。 |

同一订单和退款编号的查询意图在联网前持久化；金额、渠道配置或依据编号不能换绑。最多三次显式查询、两次请求间隔至少10秒，没有自动重试。进程中断后reserved不推断失败；重新实例化仍保留次数，允许间隔后的只读原单查询。达到上限由运营沿原记录核账，不删记录绕过上限。此上限不是支付宝限额，也不是模型或人民币预算。

报告仅覆盖这一笔退款编号，`cumulative_refund_cents`保持未知；没有查到的退款不当作零。返回`refund_executed_by_this_client=false`、`entitlements_changed=false`、`commission_settled=false`。这些标志描述本查询器的行为，不代表其他宿主组件没有处理权益。它不会释放消费额度、撤销已发资源、重新付款或宣称整单结清。宿主可接续[退款记账与访问处理](refund-reconciliation.md)，消费已确认观察、记录明确的权利处理决定并按原费率累计计算；实际资金分账和渠道整单对账尚未完成，不应据此启用公众收费。

本机测试采用原创合成订单与RSA密钥，覆盖实际HTTP签名传输、合成固定HTTPS响应、超时/中断、重复/并发、金额/身份冲突及原账本不变；没有查询真实商户或真实退款。新增记录复用原meta表，不迁移数据库。停用查询开关即可停止新网络动作，保留原查询、订单、权益和费用账本。
