# 远程资料交付的任务预算绑定

来源：本项目原创。此入口把一个买方凭据绑定到一个获准任务，在返回商品正文前检查任务状态及共享预算。它交付静态Markdown资料，并预留商品声明的依赖费用；实际模型、工具调用、支付和供应商核账仍由受信宿主负责。

## 启用条件与范围

先准备既有[身份账本](identity-gateway.md)和[任务预算](shared-budget.md)：运营批准根预算、任务与主体，另行签发买方凭据。模型不能通过工具参数创建预算、绑定凭据或改变任务归属。

服务启动时同时传`--budget`和`--enforce-task-budget`。仅传`--budget`仍只加入就绪检查，保持原有服务兼容；默认未开启此约束。以下是可审阅的启动方式，使用实际已获准的本机路径；本轮没有替用户修改运行服务或部署配置：

```powershell
python -m ai_supermarket.http_server --database work/market/market.sqlite --identities work/market/identities.sqlite --budget work/market/budget.sqlite --enforce-task-budget
```

缺预算文件时拒绝启动，不自动建空预算账本。已有服务若要采用此边界，运营必须核对所有入口及启用配置；旧服务实例、受信本地CLI及可直接访问数据库的程序不因此自动受到约束。不能向普通AI提供这些管理入口或底层文件权限。

## 受信运营绑定、查询与撤销

以下`ak_凭据编号`是占位，须替换成身份管理命令返回的非秘密`key_id`，不是token；root、task、principal必须对应已有授权记录。`authorization-ref`是实际批准记录的编号，程序不会验证其业务内容，也不会因此批准真实付款。

```powershell
python -m ai_supermarket.task_budget_admin --database work/market/market.sqlite --identities work/market/identities.sqlite --budget work/market/budget.sqlite bind --key-id ak_凭据编号 --root-id root --task-id main --principal alice --authorization-ref 实际批准记录编号
python -m ai_supermarket.task_budget_admin --database work/market/market.sqlite --identities work/market/identities.sqlite --budget work/market/budget.sqlite inspect --key-id ak_凭据编号
python -m ai_supermarket.task_budget_admin --database work/market/market.sqlite --identities work/market/identities.sqlite --budget work/market/budget.sqlite revoke --key-id ak_凭据编号
```

绑定要求有效、未禁用的买方身份及可用任务。相同绑定可以重复提交；不能通过重复提交改绑或恢复已撤销绑定。新任务应使用获准的新凭据与绑定，避免一把凭据被不同任务混用。

`inspect`返回任务归属、绑定状态、根预算余额及该任务的预留记录，不打印token或商品正文。它是受信本地管理操作，不能将结果不加筛选地提供给其他主体。预算状态查询不证明凭据当前有效，也不证明实际费用已经核验。

每页最多100条。若`next_cursor`非空，下一次inspect附加`--after-reservation-id`及该值；返回空游标才读完本次遍历。分页期间的新写入不构成固定快照，完整事故核账需先协调暂停相应写入并按恢复手册保存一致快照。

`revoke`只撤销任务绑定；不撤销API凭据、不取消预留、不退款。需同时关闭账号或凭据时使用身份管理入口。撤销后仍可进行原角色允许的搜索和状态查询，但不能通过该绑定读取正文。

## 买方流程和成本语义

买方工具仍为7个。`tools/list`附带要求及绑定的任务编号；未绑定时可发现工具、搜索、核兼容与取得免费权益，`market_read`会拒绝返回正文。已绑定时不得自行更换`task_id`。

1. 先按原流程search、resolve、acquire。获取权益不等于运行外部依赖。
2. read检查主体、任务、当前使用条件，再按商品/版本/用途在该任务中预留声明费用。同任务重复读取复用预留；不同任务分别占用同一根预算。
3. 最终交付前重新核验凭据、绑定、任务及预留状态。暂停、任务或父任务关闭时，即使零依赖费和已有权益也拒绝新读取。
4. 在最终准入与写入使用记录期间持有预算写锁；并发暂停/撤销绑定会等待已准入的交付结束，随后读取被拒绝。已经返回的资料不能远程收回；身份认证时已准入的请求也不承诺可瞬间取消。
5. 返回`task_budget`及可选`dependency_reservation`；`execution_authorized=false`、`payment_authorized=false`。只交付资料不会将预留自动核销为实际费用。

预留单位沿用商品契约的整数分；共享根预算仍能与已有微额模型费用一起精确累计。商品免费不代表依赖免费；未知依赖费按原兼容检查拒绝，不填零。

## 异常与人工核对

| 错误 | 行为与处理 |
|---|---|
| task_budget_required / task_binding_denied | 无绑定或绑定失效；运营核原身份和授权，不让AI改身份绕过 |
| budget_scope_mismatch | 请求任务与绑定不同；使用工具发现返回的获准任务，不能挪用其他额度 |
| budget_exceeded / budget_inactive | 剩余额度不足或预算/任务暂停关闭；不交付，不自动加额度 |
| budget_operation_in_flight / unknown / cancelled | 原依赖在执行、结果未知或已取消；先核原操作，不能换请求编号重新预留 |
| unavailable / 连接中断 | 可能已预留或已写入使用记录；保留原获取、任务及请求编号核验 |
| invalid_database / recovery_required | 账本不可信或恢复尚未放行；停相关入口，由运营检查，不能清标记绕过 |

预留和正文交付分两次事务：交付失败、绑定撤销或正文被隔离后，已经提交的预留仍保留。管理员用`inspect`取得记录编号，结合真实执行及账单核查：尚未开始且确认不再需要的预留，可用原受信预算接口取消；在执行或未知记录必须依原规则取得无费用证据或核销真实费用，不能为释放额度伪造0元。

重复相同read请求可复用使用记录；相同请求改用另一获取编号会冲突。不存在“失败自动退款”或“撤销绑定自动取消供应商请求”。查询与人工处置均不应重新调用供应商。

## 恢复与验收

联合快照保存并校验绑定元信息。恢复到新目录后旧凭据仍撤销、预算暂停、市场隔离；绑定历史保留，但身份路径摘要也会阻止错误位置复用。按[恢复手册](recovery.md)核验并放行市场，批准后恢复预算、签发新凭据并重新绑定；不复活旧绑定，不把复制账本当作可直接运行。

本机演练命令：`python -m unittest ai_supermarket.tests.test_task_budget_gateway -v`。用例覆盖主体/任务、并发额度、凭据过期、账号停用、损坏绑定、失败保留、暂停、恢复、管理命令和真实本机HTTP。运行网络用例使用已安装的锁定HTTP依赖；未安装会标跳过，不能声称完整网络验收。

真实外部模型执行、实际账单签验、组织/全局额度、公众生产环境和支付仍需独立验收。任务绑定代码、测试和说明均为项目原创，没有新增第三方依赖或变更数据库表结构；对外分发许可按项目主计划处理。
