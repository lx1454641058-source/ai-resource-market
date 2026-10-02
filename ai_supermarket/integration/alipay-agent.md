# 支付宝官方 Agent 支付交接

2026-09-24。本项目原创适配说明。采用支付宝官方买方支付工具，不自行实现资金钱包。已实现首次账单准备、可信宿主显式执行和原订单资源接回；当前验证使用隔离替身及本机市场，没有执行官方工具的实际付款、钱包开通或商户签约。

## 已准备的官方能力

官方源码：[alipay/payment-skills](https://github.com/alipay/payment-skills)，固定提交 `abdd97a527e8dae4ba8d661f524359f6440433a0`。`alipay-payment-skill`与`alipay-authenticate-wallet`已安装到本机Codex技能目录，原Apache-2.0许可证保留，16项文件摘要与随附清单匹配；未宣称已核验RSA签名。新技能在后续回合可被发现；安装本身不启用付款或账户。

官方helper `@alipay/agent-payment@1.0.12`的包SHA512与官方技能预期相同；未执行其安装器。安装成功分支存在自动`apply-wallet`和用户PATH修改，且该helper的包格式与本轮观察的Windows EXE ZIP不同，不能把helper安装成功作为事实。

本机已将官方Windows运行包0.4.5独立解包至`D:/CodexRuntime/alipay-agent-20260924/runtime-review/`，`alipay-bot.exe`的Windows代码签名有效，签名方为Alipay.com Co.,Ltd；`--version`和`402-buyer-pay --help`实际通过。没有修改全局PATH。运行包另属官方分发软件，不把技能Apache许可套用到EXE/DLL；不随项目候选包分发。

## 最小接法

`PurchaseDownloader`只为首次`/v1/purchases/resource`请求返回的402创建`request_snapshot`；其内容从真实Request复制，冻结原URL、POST正文、头、任务、用途、商品发行与账单。原认证头只在可信宿主内存中持有，不进入模型工具返回值或普通报告。

```python
from ai_supermarket.alipay_agent import prepare_alipay_agent

# task是已有RemoteTaskResources，request_id是本次原采购编号。
# 用户目标和session_id由可信宿主提供，不从商品正文或付款响应生成。
reply = task.wallet_handoffs[request_id]
prepared = prepare_alipay_agent(
    reply.request_snapshot,
    user_goal=original_user_goal,
    session_id=current_session_id,
    directory=new_directory_under_existing_private_parent,
)
# prepared.status == 'prepared_not_executed'
# prepared.argv是不含可执行文件的参数数组，只供可信宿主使用，不打印/序列化。
# 此示例不运行CLI、不产生付款授权。
```

目录中只保存本次原`Payment-Needed`文件和不含认证头/目标正文的绑定摘要；旧目录拒绝覆盖。目录应由已有权限保护，Windows的目录mode不能代替访问控制。`repr=False`只是减少误打印，不是阻止Python调用者读取秘密的安全边界。准备器不验证真实商户资格或费用单位，也不授予购买权限。

官方CLI会执行付款并重放原URL/POST/正文/头，不应把它当仅返回proof的函数。执行器固定0.4.5的EXE摘要，按参数数组调用且不经shell；参数含认证头，不能写日志或进入模型。`use()`和模型工具不会自动付款，不调用`check-wallet`或`apply-wallet`。

## 可信宿主的显式执行与接回

已有原始快照的宿主可显式调用`task.execute_agent_payment(request_id, session_id=..., directory=..., execution_root=..., executable=..., authorization=...)`。目标来自当前任务的原始目标，模型不能提供其他目标、可执行文件或授权。`authorization`不是模型自动填写的表格：可信宿主必须事先核验实际购买许可、精确账单、报价和收款方，并提供下列完整绑定。

| 授权字段 | 来源及含义 |
|---|---|
| `url/task_id/purpose/order_id/request_id/key_id/resource_id/version/release_digest` | 原请求及原市场订单，一项不匹配即拒绝 |
| `bill_sha256/user_goal_sha256` | 原始账单字节及真实任务目标的摘要 |
| `amount_cents/currency/payee/expires_at` | 获准报价的人民币整数分、CNY、收款方及授权到期秒时间戳 |
| `authorization_ref/quote_ref` | 真实授权与报价的证据引用，不能填写“待验证” |
| `executable_path` | 宿主批准的绝对EXE路径，执行器仍自行核固定摘要 |
| `compatibility` | 固定`cli_version/cli_sha256`及`redirect_policy_evidence_ref/authorization_forwarding_evidence_ref/zip_delivery_evidence_ref` |

证据引用是可信宿主已完成核验的声明，函数不会读取引用文件或证明其真实性，也不会从账单猜金额。当前项目没有真实兼容证据与交易授权，因此未提供可直接启用付款的默认配置。付款准备命令不是取得用户授权的方法。

`execution_root`须由宿主固定、预先配置私有权限并持久保留；不能每次换目录。执行器以规范化市场地址和原订单生成唯一意图，启动前原子占位并落盘。同一root内并发、换会话、换准备目录、超时或重启均不能再pay；不同root或人为删除记录不在此去重保证内。EXE及其依赖目录也须由可信宿主保护，摘要检查不能防同机有写权限的恶意替换。

子进程隐藏窗口、禁用stdin、最多等待60秒，stdout与stderr合计默认上限256KiB。仅传入明确指定的`AIPAY_MODEL/AIPAY_OUTPUT_CHANNEL`，不继承全部环境。实际Windows钱包对这种环境和工作目录的兼容尚待联调。超时控制直接启动的进程，未验证官方工具是否派生后代进程；不保证整棵进程树结束。停止本地进程也不意味着远端交易撤销。

结果仅反映进程退出、超时、输出限制等事实，`payment_status='unknown'`始终不根据退出码或文字变成已付。原始stdout/stderr只在受信内存且不进入repr或磁盘结果；不自动转发混合日志，不提取交易号，项目代码不额外调用query/ack。实际用户友好输出须由可信宿主按官方技能处理。本轮未取得固定版本完整JSON契约；`--json`选项存在不等于字段和日志边界已验证。

**固定0.4.5的实际帮助揭示了另一个集成条件：**`402-query-payment-status --help`说明查询会获取资源，并在资源正文非空后自动确认履约。它不是纯只读查询，与技能文档的独立买方ack步骤需要进一步核对。当前项目没有调用该命令，不能因为代码未显式执行ack就宣称将来查询没有收货副作用。也不能在CLI可能已自动ack后再无条件补发一次。帮助没有列出关闭自动ack的选项，不猜测不存在的开关。先确认能在本地文件/许可验收后进行收货，或明确官方工具支持的验收方式，再启用待确认查询接续。[本次实际帮助](../../work/alipay-agent-contract-20260924/query-help.txt)

待确认时返回调用方，不能立即用循环重付。经实际状态核查，宿主可用原执行意图中的订单号调用`task.receive_after_agent_payment(request_id, expected_order_id=...)`：仅查询原市场订单，原单已验签后才由原下载器重新取资源、校验摘要及许可。原CLI输出不作为本地交付证据；未付款、错误或未知时保持未完成，新的402也不产生付款快照。查询和下载各计入已有任务调用额度。该方法不自动发送买方履约回执，资源取得仍不等于用户任务完成。

## 恢复和结果边界

- `/handoff`恢复返回的账单不含可付款快照：该URL拒绝Payment-Proof，不能将它偷偷换成`/resource`交CLI。首次请求中断或原快照丢失时停止自动付款，核对原订单；不能删记录或换目录制造“首次”。
- 原`Accept: application/zip`保持不变。CLI的ZIP字节/响应头保真、重定向和认证头处理仍未验证，本地参数检查不能证明这些能力。
- CLI的成功文字不代替市场验签或最终资源核验。付款后用原request_id查询市场，并由原下载器重取/核验包、许可与回执；仍是402/202时保留状态，不能再调用pay。
- 付款意图及进程结果已持久化，但官方待确认查询凭据的可靠解析、查询及履约接续仍未实现。`tradeNo`与`outShakeNo`必须来自实际本次CLI；不要从演示或日志补造，不把outShakeNo用于履约。
- 真实退款提交、平台佣金分账与对账继续单独接入；官方买方支付工具不自动解决这几项。

当前`wallet_execution_available`继续为false。资格、金额单位、HTTPS实际端点、具体交易授权及CLI业务兼容均落实后，才能执行获准沙箱或真实单验收。准备和只读帮助命令通过，不是支付完成。

2026-09-24增补：商家侧已用官方匿名沙箱实际完成一笔0.01元虚拟账单、原客户端HTTPS验付、订单绑定以及正文/许可交付，详见[证据](../../work/alipay-sandbox-cent-20260924/report.md)。该实验使用官方商家沙箱脚本约定，未执行Windows买方CLI，故不改变上述false标记或买方兼容限制；真实钱包、履约确认、退款和分账仍未验收。
