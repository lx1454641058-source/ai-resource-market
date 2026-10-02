# 整任务计量与对照证据

来源：本项目原创。计量记录实际观察值，不授权支出、不证明商品有效。当前为Python宿主接入，未接通真实外部模型账单。

## 口径与边界

一次run是一个完整根任务，包含子任务、检索、读取、模型、依赖、评价、失败重试与分摊成本。每次真实重试新建调用记录；同一费用回执不能计入两次。计量器不自动重试，不替代预算、权限或外部工具防重复执行机制。

Token是模型处理文本的计量单位，只接受供应商响应或受信宿主实际记录，不从字节推算。input_tokens包含cached_input_tokens，缓存部分不能再次相加；输出与推理用量是否已合并须由具体适配器核对。所有模型调用，包括模型评价，都标为model，避免遗漏。

cost_micro_cny是百万分之一元人民币的整数：1元为1000000，1分为10000。预算新增[微额接口](shared-budget.md)，可按相同精度预留及核销；原按分接口继续保留，单位不能混用。币种转换、费用分摊及渠道舍入由受信适配器核实，不能猜汇率。未知用null；0仅表示有依据的零直接费用，基础设施/审核等可另记other，不能重复分摊。

known_cost_micro_cny只是已知部分。只有任务结束、宿主确认覆盖完整且每个调用都有费用，total_cost_micro_cny才有数值；未知调用仍阻止收益比较。调用耗时之和与整任务耗时分开，并行调用不能相加当作墙上时钟经过的时间。

## 本地可运行示例

下面是普通程序选第一项并检查正文的合成演练，不是AI自主评测，也没有实际模型或财务用量。在项目根目录执行：

```python
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from ai_supermarket.common import compact
from ai_supermarket.host import TaskResources
from ai_supermarket.measurement import TaskMeter
from ai_supermarket.service import Market

with TemporaryDirectory() as folder:
    root = Path(folder)
    meter = TaskMeter(root / 'measurement.sqlite', 'demo-run', 'demo-host',
                      experiment='demo-experiment', case='brief', arm='market',
                      configuration_hash=sha256(b'demo-fixed-config-v1').hexdigest(),
                      evidence_mode='synthetic')
    market = Market(root / 'market.sqlite', 'demo-host')
    task = TaskResources(market, 'main', '产品简报', kind='template', meter=meter)
    item = task.candidates['items'][0]
    resource = task.use(item['id'], item['version'], 'read-template')
    task.checked(resource['usage_id'], 'demo-content-nonempty', bool(resource['content']))
    meter.finish('unknown', 'demo-only', 'local-delivery-check', coverage_complete=False)
    report = meter.report()
    assert report['calls'] == 3 and report['model_calls'] == 0
    assert report['total_cost_micro_cny'] is None and report['input_tokens'] is None
    print(compact(report))
```

TaskResources自动记录搜索、一次use流程及已有检查。use的预检、获取、读取合并为一个资源阶段，不声称逐次网络计量。资源引用是ID与版本的摘要；具体商品和usage由市场账本按主体、任务及版本核对。未传meter时保持原行为。

## 真实宿主适配

既有Codex纯文本调用可使用[预算与模型计量适配](codex-measurement.md)，在同一请求上连接准入、执行和用量；工程测试使用替身，真实账户试点与账单仍待核验。下述通用observe本身不提供防重复执行。

在原本已获准的模型调用处执行`result, call_id = meter.observe('model', model_version_ref, task_id, existing_call)`。existing_call必须是受信宿主已有函数，不能来自商品正文或模型生成脚本。预算与权限仍由原执行链路检查。

宿主从实际供应商响应提取用量，再调用`meter.receipt(call_id, receipt_ref, input_tokens=..., output_tokens=..., cached_input_tokens=..., cost_micro_cny=...)`。回执引用使用供应商/账号命名空间及请求或明细编号，不包含密钥、正文或账户秘密。可以先记token，再以同一回执补未知费用；已知数值不可覆盖，null不抹掉原值。模型自报数值不等于供应商数据，当前未实现供应商验签。

子任务在同一根meter下使用各自task_id；多个进程可用相同run和冻结配置打开账本。根宿主等所有子任务结束后再finish。崩溃留下in_flight，可用`call_records(offset, limit)`按页取得原调用，核实际状态后end或补回执。计量写入失败时原操作可能已执行，必须按原请求核查，不能重做；未知结果保持unknown，不宣称收益已证实。

finish的pass/fail/unknown指最终任务验收，validator与evidence_ref指向检查器版本和产物依据。资料非空、HTTP成功或模型声称完成不等于最终pass。coverage_complete默认false，是宿主声明而非自动证明；遗漏调用时不得设为true。

## 比较和汇总

每次run固定experiment、case、arm、configuration_hash、evidence_mode。共同配置摘要需来自冻结的模型、任务条件、检查规则与已有资源；实验差异由arm和资源记录表达。不同配置或真实/合成数据不能混合，不为方便比较伪造相同摘要。

- `compare_runs`比较同一任务direct/local对照与market组。缺用量、未知调用或质量不合格返回insufficient_evidence；差值为实验减对照，正值代表超市更多。单对诊断不等于统计或商业结论。
- `summarize_runs`汇总同实验/组别/配置/证据类型。失败任务费用仍进分子，合格任务数只计pass；全失败时每个合格任务成本为空。返回精确分子与分母，避免浮点舍入当账单。
- 报告始终保留savings_proven=false与independent_quality_verified=false。真实价值须用主计划的冻结样本、独立评审和对照试点验证。

## 数据与未完成项

新建独立计量账本，不修改已有业务表。仅保存标识、状态、用量、时间和证据引用，不保存任务目标、模型输入输出、提示词、商品正文或异常文本。标识仍可能关联业务，应落实访问及保存/删除期限，不能声称匿名化。

这是受信宿主API，不是模型工具或多租户身份认证。已有Codex适配的本地工程验证；真实调用试点、账单核验、已知数值争议更正及独立效果实验仍待完成。已知值错误应保留争议记录并排除相应实验，不能静默覆盖。计量账本可通过[四库联合恢复](recovery.md)纳入快照，原三库格式仍兼容；没有计量的旧快照不能据恢复成功声称用量完整。

## 恢复后的任务核对

恢复副本保留原始记录，但历史任务冻结执行、撤销旧的完整覆盖资格。`TaskMeter.existing(database, run_id, principal)`只打开存在且归属正确的任务，缺库不初始化。以下都是受信本地管理动作，不能向买方模型开放：

| 动作 | 使用条件与效果 |
|---|---|
| recover_call(call_id, kind, reference, task_id, created_at, evidence_ref) | 原始调用轨迹证明遗漏时补录；使用原编号，先记处理中，不执行模型或重试原请求；重复同记录无副作用，不同记录拒绝 |
| resolve_recovered_call(call_id, state, elapsed_ms, evidence_ref, reply_bytes=None) | 查询原请求后，将处理中/未知改为实际成功或失败；已知结果不可静默改写 |
| receipt(...) | 使用原接口补尚未知的费用/token；回执不能重复归属，已知值冲突拒绝 |
| finish(...) | 原任务未结束时据验收证据结束；已有最终结果不覆盖 |
| review_recovery(coverage_ref, receipts_ref, outcome_ref, coverage_complete=True) | 显式核对整个任务覆盖、账单及最终验收；所有调用须有终态及费用，模型调用须有输入输出token；参数是三个证据引用和明确的完整声明 |
| invalidate_recovery(evidence_ref) | 发现新争议时撤回复核，保留原数值和审计；停止把该任务用于费用比较 |

`coverage_complete=True`必须作为关键字参数传入，不能把三个引用字符串当成已完成专业核验。API只记录受信宿主/运营声明，返回`independent_verification=false`。

历史任务只有四库共同放行且逐任务复核仍有效时才恢复完整报告。`source_coverage_attested_by_host`保留原覆盖声明；`coverage_attested_by_host`表示当前有效覆盖，原声明不足时可在补齐外部证据后复核，但不改原声明。`recovery_review_required=true`时，已知部分仍显示，总费用和完整token为空。补入新调用、费用或缓存用量会使已有复核失效；重复提交相同数据不会。再次恢复会重新隔离全部历史任务。

四库恢复后的measurement.sqlite须与同批次market.sqlite保留在同一目录；两个门均放行才可开始新的run。历史run即使复核完成也不能begin/observe新调用，新工作创建新run。服务ready与任务可比较是两件分别验收的事，恢复工具不会声称省钱、自动重做外部操作或核验供应商账单真实性。

专项命令：`python -m unittest ai_supermarket.tests.test_measurement -v`，使用可用项目环境。本模块不增加日常模型调用；代码、示例及测试为原创，无新依赖。后续验收和公开分发权利以[主计划](../../docs/ai-supermarket-master-plan.md)为准。
