# 本地运营只读入口

`tools/market_operations.py` 提供三个命令，供有账本文件读取权限的可信本地运营宿主使用。仅依赖项目代码和 Python 标准库，不安装新软件、不联网，不读取身份文件、Token 或密钥。

买方和卖方参数是原订单范围断言，**不是身份认证**。不要把该脚本或其数据库路径参数直接暴露为公共 HTTP/MCP 接口。能够读取整个数据库的本地操作者原本就具备该文件的读取权限；远程调用仍须另行使用现有认证和固定宿主配置。

## 可执行命令

在项目根目录运行；其他工作目录可使用脚本和账本的绝对路径。`<...>` 必须替换为实际已有值。金额单位为人民币分，退款编号应来自原退款流程，不能新造编号后把无记录当成退款未发生。

```powershell
python -B tools/market_operations.py order-inspect --database <已有市场账本.sqlite> --buyer <原买方> --seller <原市场卖方> --order-id <原订单编号>
python -B tools/market_operations.py refund-history --database <已有市场账本.sqlite> --buyer <原买方> --seller <原市场卖方> --order-id <原订单编号> --refund-id <原退款编号>
python -B tools/market_operations.py accounting-status --database <已有市场账本.sqlite> --buyer <原买方> --seller <原市场卖方> --order-id <原订单编号>
```

| 命令 | 结果 | 不包含的动作 |
| --- | --- | --- |
| `order-inspect` | 原订单状态、买卖双方、金额、环境及本地付款/权益记录是否存在 | 不确认新付款、不搜索商品、不授予权益 |
| `refund-history` | 指定退款原查询记录的状态、金额和次数；缺少记录返回 `not_queried`、金额 `null` | 不查询渠道、不提交退款、不创建记录 |
| `accounting-status` | 冻结成交费率、已记退款、剩余佣金建议和访问封锁记录 | 不记新账、不执行分账、不恢复预算 |

三条命令的 `--help` 列出完整参数。没有写入命令、通用 SQL 或原始请求逃生入口。

## JSON 契约

除 `--help` 外，stdout 始终输出一行 JSON。成功退出码 `0`，失败 `2`；错误不回显未知参数、文件路径、数据库正文或异常栈。

```json
{"schema_version":1,"status":"ok","command":"refund-history","read_only":true,"evidence_scope":"retained_local_history","channel_checked_this_call":false,"result":{"order_id":"example","refund_id":"original-refund","status":"not_queried","attempt_count":0,"refund_confirmed":false,"refund_cents":null}}
```

```json
{"schema_version":1,"status":"error","error_code":"order_not_found","message":"查询未完成；请核对范围、参数及留存记录。","read_only":true}
```

常见错误：`order_not_found`（不存在或买卖方不匹配）、`database_not_found`、`payment_not_confirmed`、`refund_record_corrupt`、`payment_history_corrupt`、`database_or_history_invalid`。缺记录与损坏不等价；损坏必须保留原账本核查，不能重建、清空或自动重试付款。

## 复用与只读边界

- 不使用 `purchase_connections.load/attach`：前者读取密钥来核验配置指纹，组件构造涉及可写存储，后者还启动任务资源搜索。此入口直接用 SQLite `mode=ro`，并设置 `query_only` 与 SQL 操作白名单，不执行建库、建目录、迁移或授权。
- 复用 `PaymentOrders._order` 校验原订单范围、绑定摘要和留存确认；复用 `refund_recovery.validate_refunds` 校验指定退款查询历史；复用 `payment_refunds._load/_view` 和 `AlipayRefundQueryClient._view` 校验原退款来源并生成既有佣金/退款视图，不复制金额或分账逻辑。
- 商户映射取自原订单并与显式卖方比对；不会核验当前在线商户资格，也不从模型输入建立可付款映射。不读取私钥重新验签，不把本地摘要校验当成独立渠道证明。
- 不输出付款凭证、商户账号、完整原单、资源正文或任意数据库字段；订单结果使用固定字段白名单。
- 当前支持项目默认的 SQLite rollback journal（普通回滚日志）账本；WAL 账本返回 `read_only_wal_unsupported`。SQLite 对 WAL 的只读打开可能创建共享内存伴随文件，因此这里拒绝该模式，也不使用可能漏读已提交数据的 `immutable=1`。不会自动切换日志模式或修改运行数据库。

## 含义和剩余工作

`paid_recorded`、`refund_confirmed` 只表示本地已留存的证据及状态。合成交易账本的这些值不代表真实资金。`commission_settled:false`、`funds_moved:false` 描述该模块未执行资金操作，不是对外部渠道当前状态的否认。`channel_refund_total_known:false` 和 `channel_fee_cents:null` 必须保留。

这次未加入退款提交、实际分账、退款渠道查询或退款记账命令。已有 `PaymentRefunds.reconcile` 属于可信宿主的写操作，需要匹配渠道退款成功观察及运营方权益决定；实际退款提交和分账仍需真实渠道契约和授权，不可从本地查询结果推断完成。

验证：`python -B -m unittest ai_supermarket.tests.test_market_operations -v`，使用隔离合成订单、签名和退款记录。测试覆盖三个结果与既有宿主视图相同、买卖方匹配、缺失路径不创建、原账本和目录内容不变、禁止写 SQL、腐坏来源拒绝、异地工作目录调用和敏感错误不回显。
