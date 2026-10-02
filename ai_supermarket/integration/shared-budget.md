# 主任务与子任务共享预算

来源：本项目原创。预算是获准花费的上限，不是账户余额、冻结资金或支付渠道授权。当前实现服务于受信Python宿主；模型不能创建预算、分配任务、开始扣费、核销或释放未知费用。

网络服务可另启用[远程任务预算绑定](task-budget-gateway.md)，把买方凭据绑定到单个任务，在静态资料交付前检查并预留依赖费用。该入口不自动执行依赖、不自动核销，也不成为外部模型/支付网关；本地预算接口仍由受信宿主使用。

## 已实现与使用条件

`BudgetLedger`使用新的独立SQLite文件，主任务和所有子任务共用一份额度。SQLite是把结构化数据保存在文件中的数据库；同一台机器上的独立进程通过事务竞争额度，不给每个子任务复制一份总预算。预算账本不与市场、身份或旧钱包账本混用，不迁移既有文件。

支持resource、model、dependency、feedback四类费用记录。原reserve/settle金额为人民币分的非负整数，范围0～100000000分；新增reserve_micro/settle_micro以百万分之一元记录，范围0～1000000000000，同一根预算共用原授权额度。未知不能填0，不能混用单位。报价上限、币种与实际费用须来自受信适配器，不能直接采用模型估价或卖家正文作为付款依据。其他币种仍须确认核算依据，本模块不猜汇率。

可用额度＝授权上限－已记录实际费用－全部有效预留。reserved、in_flight和unknown都占额度；只在操作未开始而取消，或有依据确认未扣费，或核销实际费用后释放相应部分。当前不因退款自动恢复累计消费额度，退款与平台佣金规则仍单独核定。

主预算由有权方确认后，受信程序调用create并保存非秘密authorization_ref；这个引用本身不证明确认真实发生。grant_task固定任务、主体和可选父任务，scope核对绑定。任务层级最多64层；关闭父任务阻止后代新增预留或开始操作，但不抹掉已发生/未知费用。不同root仍有各自授权；可显式启用下文的同账本归属方与总额度约束，不提供支付钱包。

## 可选的归属方与账本总额度

`configure_pool`让同一账本中的所有根任务同时服从根预算、归属方额度和账本总额度。归属方使用根预算创建时固定的`owner`；由受信运营者映射到实际个人或组织，这个字符串不自动证明真实身份。模型没有管理此配置的工具。

- 未配置时保留原行为，不自动改变既有实例。首次启用必须覆盖所有已有根预算的owner，包括已关闭任务；已发生费用和全部有效预留立即计入，不能靠创建新任务、重启或关闭旧任务清零。
- 所有金额为非负整数微元，即百万分之一元人民币；1元=1000000微元。全局上限和各owner上限最多10的15次方微元，仅是输入类型边界，**不是建议授权额度**。最多256个归属方；这是小规模单账本约束，不是已验收的生产容量。
- 这是账本生命周期内的累计额度，不按日/月自动清零、不恢复退款额度。新建另一份账本不能沿用本约束宣称覆盖全平台；跨账本、跨机器、真实组织身份及全部供应商费用仍须统一受信入口和另行验收。
- 新预留在同一写事务中计算三层额度并入账；`begin`再次检查当前状态。在执行中的请求不会因暂停自动取消。晚到实际费用仍入账；实际费用导致全局超额时自动暂停总预算，归属方超额阻止其所有根任务开始新调用。已有其他归属方仍须受全局额度限制。
- `pool_summary()`仅供运营读取。`global`和`owners`各自的available是该层剩余额度，不代表某任务最终可花金额；实际可用还要受根预算、父子任务、恢复门和其他层约束。买方工具不暴露其他归属方的费用。
- 状态为active、paused或closed。paused可以在费用/授权核对后调整并启用；closed不可重开。每次修改须传当前`expected_revision`，首次为0；并发修改只有一份可成功。禁止删除配置、历史或重置能力版本来绕过限制。

以下仅为受信运营者的配置形状示例：账本累计最多1元，两个归属方各最多0.6元；实际使用前金额与依据必须另获授权。配置不会创建根预算或代替付款批准。

```json
{"limit_micro_cny":1000000,"owners":{"org-a":600000,"org-b":600000},"state":"active","authorization_ref":"synthetic-example-not-approval","expected_revision":0}
```

把经过实际核准的配置保存在受保护的JSON文件，用已有预算账本执行：

```powershell
python -m ai_supermarket.budget --database work/approved-instance/budget.sqlite pool-inspect
python -m ai_supermarket.budget --database work/approved-instance/budget.sqlite pool-configure --input work/approved-policy.json
```

这两个路径是占位例子；命令拒绝缺失/空账本，不自动新建。配置输入限64KiB，拒绝重复和未知字段。检查结果仍返回`payment_authorized=false`。普通Python宿主可调用`ledger.configure_pool(limit_micro_cny, owners, state, authorization_ref, expected_revision)`，管理接口不能注册给模型。

启用时只在原meta表写入当前配置、递增审计及`budget_schema_version=2`能力标记，不增加表、改列或自动迁移原实例。**启用前必须停止旧程序的全部写入者**：旧版BudgetLedger新建实例会拒绝版本2，但已经打开的旧对象不会自动更新检查。需在经核验的新版本中启用，不允许新旧写入程序混用。

三库/四库快照验证配置与审计完整性。恢复副本保留额度、归属、已花费和未知预留，并追加恢复审计；active转paused，closed保持closed。先完成市场恢复放行，再由获准运营者核准总预算，最后逐根预算恢复。恢复放行和readiness均不意味着预算已经active；晚到费用仍可记录。

测试见`python -m unittest ai_supermarket.tests.test_budget_pool -v`：独立进程竞争、跨根归属方限额、混合金额、未知与关闭、迟到超额、修改冲突、恢复、真实本机HTTP与模型替身。预算金额均为合成数据，模型替身是代替真实模型的测试程序。总额统计当前扫描历史预留，未验证大账本吞吐；正式容量需负载测试，不能据小样本测试时间作承诺。

## 状态与恢复

预算恢复副本必须与同批次market.sqlite保留在同一目录。市场恢复未放行时，不能创建新根预算、恢复active状态或开始新支出；仍允许核销已发生费用及凭证确认未扣费。缺恢复标记的半成品副本拒绝使用。普通预算暂停仍返回budget_inactive；恢复批次或市场门未通过返回recovery_required。放行不自动解冻旧预算，预算需有权运营者另行核对。

| 状态 | 表示什么 | 允许的处理 |
|---|---|---|
| reserved | 已预留，尚未获准开始这一调用 | begin或cancel；暂停/关闭预算后不能begin |
| in_flight | 已有执行者领取，可能正在执行或执行者已退出 | 核对原操作；不可直接cancel，不因超时自动重做 |
| unknown | 操作或费用回执不确定 | 保留全部预留；settle记录实际费用，或confirm_no_charge记录未扣费依据 |
| settled | 已依据受信回执记录实际费用 | 原金额/回执可重复核对；不同金额不能覆盖 |
| cancelled | 未开始已取消，或已经核实未扣费 | 保留历史，不因重试自动重新预留或执行 |

同一任务request_id绑定费用类别、对象引用和上限；参数改变被拒绝。begin只有首次领取返回execute=true，多个进程不能因响应丢失而重复执行。run/run_reserved是受信宿主的调用包装：执行宿主提供的函数，再读取其费用回执；任何异常保留为unknown，不自动重试。商品脚本不会自动执行，函数不应来自卖家内容或模型代码。

执行者退出后，in_flight不会自动过期；重启或关闭任务仍保存风险敞口。先查询供应商原请求号/回执，再由受信程序处理，不能把“没收到响应”当成“没扣费”。供应商请求建议使用稳定reservation_id作为防重复编号；实际渠道是否支持这种编号需单独验证。相同root中一条费用回执不能核销多个预留；批量账单需使用可区分明细的引用。

实际费用高于预留时如实入账，并暂停根预算；超过总额会报告over_budget_cents，不通过拒绝记账掩盖损失。当前已在执行中的其他调用仍须核对，暂停不等于撤销外部扣费。已关闭预算不会因此重新打开。费用依据只代表宿主提交的证据引用，未接入真实支付验签或独立财务确认。

## 宿主接入方式

下面仅是临时软件演练，没有付款或真实预算批准，退出后清理临时账本：

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from ai_supermarket.budget import BudgetLedger

with TemporaryDirectory() as directory:
    ledger = BudgetLedger(Path(directory) / 'budget.sqlite')
    ledger.create('demo-root', 'demo-owner', 100, 'demo-only-not-payment-approval')
    ledger.grant_task('demo-root', 'main', 'alice')
    ledger.grant_task('demo-root', 'child', 'bob', parent_id='main')
    main = ledger.scope('demo-root', 'main', 'alice')
    child = ledger.scope('demo-root', 'child', 'bob')
    first = main.reserve('step-1', 'model', 'demo:quote-1', 60)
    second = child.reserve('step-2', 'dependency', 'demo:quote-2', 40)
    assert main.summary()['available_cents'] == 0
    # 以下25分是演练回执，不是调用外部模型的实际价格。
    result = main.run_reserved(first['reservation_id'], lambda rid: 'local-demo-result',
                               lambda output: (25, 'demo:receipt-1'))
    assert result['status'] == 'completed'
    child.cancel(second['reservation_id'])
    assert main.summary()['available_cents'] == 75
```

实际获准任务中，将scope传给`TaskResources(..., budget=scope)`；scope的task_id/principal必须与宿主任务、市场主体一致。商品声明每任务额外依赖费用大于0时，没有绑定共享预算会返回shared_budget_required。

当前market_acquire只能发放免费或已有静态资料权益，付费商品仍返回payment_unavailable。宿主确认资料可获取后、返回正文供使用前预留依赖费用；不足时停止使用，可能已留下免费获取记录，但没有收费或依赖执行。暂不可获取的商品不产生依赖预留，之后变为免费可重新尝试，不留下永远阻止该商品的取消记录。

同一任务对同一商品版本/用途的重复读取只共用一次依赖预留，避免把读取次数当成依赖调用次数；不同子任务仍分别记录并共享root上限。不同商品声明相同依赖时暂按各自上限保守预留，不仅凭相同名称就假定只收费一次。返回正文只表示资料交付和预算准备；宿主按实际权限决定调用，并通过run_reserved及受信回执核销，不能据预算字段执行商品脚本。

model/resource/feedback等其他费用可由受信宿主用run包装实际供应商调用，但本次未连接任何模型或支付渠道。卖家真实资源收款、跨机器远程预算服务、供应商账单自动核销、全部费用覆盖和生产恢复仍待实现/验收；HTTP和MCP买方工具保持7个，不允许模型通过预算管理工具绕过授权。

## 微额费用与整任务计量

1元＝1000000微元，1分＝10000微元；此处微元指百万分之一元人民币，仅为内部精度，不是渠道结算单位。根授权仍按分建立，内部按微元累计全部分/微元预留和已发生费用。例：10次各999微元，已用9990、可用10微元；不能各次截为0，也不能各次收取1分。卖方报价、原订单佣金及支付渠道舍入规则不由该模块决定。

| 接口/字段 | 含义 |
|---|---|
| reserve_micro(request_id, kind, reference, max_micro_cny) | 按精确上限预留；同编号绑定精确金额和单位，不能用相同向上取整分值掩盖金额变更 |
| settle_micro(reservation_id, actual_micro_cny, evidence_ref) | 核销真实精度费用，原金额/回执重复提交无副作用；已知金额冲突拒绝 |
| run_micro(..., operation, receipt) / run_reserved_micro(...) | 与原调用包装相同，只执行受信回调一次；receipt返回微元金额与依据引用。错误单位在实际回调前拒绝；异常不自动重试 |
| amount_unit | 预留使用cents或micro_cny；核销和调用包装须同单位 |
| limit/spent/held/available/over_budget_micro_cny | 预算精确上限、已用、预留、可用及超额字段，分别带相同后缀 |
| 原*_cents字段 | 纯分记录时保持原数值；存在微额时已用/预留/超额各自向上取整、可用向下取整，作为保守显示。各项单独取整后不保证相加恒等，不作为实际账单或佣金基数 |

内部准入始终比较精确金额，不能用保守显示值反推精确余额。即使实际费用与预留向上取整后都是1分，只超出1微元也会记录并暂停根预算。未知仍保留原精确上限；证实失败但收费的调用同样计入费用。执行中请求不能因暂停被假定已取消。

以下合成示例只运行本地函数，没有模型、支付或真实批准：

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from ai_supermarket.budget import BudgetLedger

with TemporaryDirectory() as directory:
    ledger = BudgetLedger(Path(directory) / 'budget.sqlite')
    ledger.create('micro-demo', 'demo-owner', 1, 'synthetic:not-payment-authorization')
    ledger.grant_task('micro-demo', 'main', 'demo-agent')
    scope = ledger.scope('micro-demo', 'main', 'demo-agent')
    for index in range(10):
        scope.run_micro(str(index), 'model', 'synthetic:quote', 999,
                        lambda reservation_id: reservation_id,
                        lambda reservation_id: (999, 'synthetic:receipt:' + reservation_id))
    assert scope.summary()['spent_micro_cny'] == 9990
    assert scope.summary()['available_micro_cny'] == 10
    assert scope.summary()['available_cents'] == 0
```

与[计量器](measurement.md)接入时，受信适配器把同一供应商回执的精确费用交给计量cost_micro_cny和预算settle_micro，不能再次按分舍入。二者是不同账本，没有跨库事务：计量已写而预算未写时，保留预算unknown/in_flight，用原请求核账并补原回执，不重复调用供应商；预算已记而计量缺记录时，费用不会重新变可用，实验保持不完整。汇率、批量账单分摊、验签和完整费用覆盖仍待真实渠道验证。

新增字段通过原spec与meta保存，费用、状态和审计在同一预算事务内提交；未新增表或隐式迁移旧文件。快照/恢复校验精确费用是否缺失、与分显示是否一致、是否存在无关联费用；三库和四库快照均包含这部分预算元信息。启用微额的账本必须使用支持该功能的程序版本，不能让旧程序继续写入或手工清除精度元信息。恢复后仍保留原精度与未知敞口。

新增专项运行`python -m unittest ai_supermarket.tests.test_budget_micro -v`，覆盖精确累计、混合单位、同回执预算/计量、线程/独立进程竞争、晚到/超额/无扣费、事务回退及恢复门。所有测试费用为合成数据。

## 核验记录与剩余限制

预算专项测试覆盖并发子任务和独立进程竞争、一次执行领取、跨主体隔离、未知/重启/关闭、晚到回执、超额入账冻结、回执复用拒绝，以及真实本地市场资料准备链路。本次测试中的金额均是软件测试值，不是实际模型/支付用量。

在项目根目录运行`py -3.12 -m unittest ai_supermarket.tests.test_budget -v`。数据文件应位于受保护的本机磁盘；未验证网络共享盘、复制副本多点写入或生产灾难恢复。备份恢复必须保留所有未决预留、任务关闭状态与费用依据，不能仅重建一个空额度。

本模块和测试为项目原创，只用Python标准库；没有新增依赖或模型。正式业务责任、费用渠道与上线门槛继续以[主计划](../../docs/ai-supermarket-master-plan.md)为准。
