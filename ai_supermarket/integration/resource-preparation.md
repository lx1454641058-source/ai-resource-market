# 任务资源准备

v0.41 后续入口：[把已选资源交付到任务目录](resource-delivery.md)。支持无额外依赖/权限的免费或已有权益 Markdown；本页的发现和预检仍不进行取用。

来源：本项目原创。供运行AI的宿主调用：AI从目标提出需求，搜索候选并自行选择，规则统一核对。用户不需要手工挑商品或填写这些JSON。本工具不调用模型；不能仅凭它从自然语言自动理解领域需求。

## 发现与选择

输入例子（示例，非真实用户需求）：

```json
{"task_id":"new-domain-1","purpose":"personal_use","needs":[
  {"id":"method","query":"产品分析","kind":"skill","required":true},
  {"id":"facts","query":"事实清单","kind":"template","required":true},
  {"id":"cover","query":"封面插图","kind":"image","required":true}
]}
```

在可信本机环境、已有初始化的开放市场上执行：

```powershell
python -B tools/market_prepare.py discover --database work/market.sqlite --principal my-buyer --input work/needs.json
```

路径和买方编号换成实际值。本地角色入口不提供公网认证，不交给不可信调用方。Python调用入口为`ResourcePreparation(已有买方Exchange实例)`；现有市场工具和数据库结构不变，未自动添加到MCP客户端。

每次1～8项需求，每项最多3个候选，相同查询和类型只搜索一次；不读取正文。kind支持需求标记skill、template、image、audio、tool。v0.43支持在素材schema 2市场搜索受限PNG/WAV，见[素材接入](asset-market.md)；tool仍返回unsupported_delivery。旧库没有素材时返回no_match；不拿文字冒充图片。unavailable保留错误码，服务故障不等于没有需求。

AI将选中的真实id/version加入同一输入的selections对象；每项需求都有键，明确不选时为null。例如`"selections":{"method":null,"facts":{"id":"trial-seller:local-evidence-note","version":"1.0.0"},"cover":null}`。该样例并不满足两个必需项；正确结果应显示缺口。判断可以使用已有能力而不需要资源时，AI应依据任务调整需求，不能为通过检查擅自删掉用户的必要要求。

## 整组预检

```powershell
python -B tools/market_prepare.py check --database work/market.sqlite --principal my-buyer --input work/selection.json --budget-cents 0
```

预算单位为人民币分，仅是本次资源估算对照值，不是支付授权或资金预留。宿主策略可通过`--host-policy`传入已有可信配置，不从商品或模型输出取得权限。相同资源/版本只resolve一次，返回一次完整许可和使用条件；AI仍需判断语义适配，规则不证明专业知识正确或素材权利真实。

v0.63已合入发行绑定：资源预检返回`release_digest`。核对本次条件后，将原值保存到相应`selections.<需求编号>.expected_release_digest`，再交付；此字段与id/version一并保留。后续预检与交付不符时停止，不能更新成新值来绕过。旧选品文件未传该可选字段时仍兼容，但不能声称更早的选择条件已被绑定。

| 结果 | 含义 |
|---|---|
| prechecked | 本次选中项的声明检查通过，尚未获取、读取、安装、验证产出或完成任务 |
| needs_attention | 必需项未选、用途/类型不符、商品不可用、费用未知、付款不支持或估算超过预算等 |
| no_resources_selected | 没有选中资源且必需项没有缺口；可按任务条件使用已有能力 |

所有需要的资源都应在执行前检查。工具保留未满足项，不把部分准备当全部就绪；可选项可以明确放弃。每次check重新读取商品状态，不能将旧报告当成后续执行授权。退出码1表示错误或needs_attention；发现阶段成功枚举缺口时仍返回0，调用方应检查逐项状态。

已有同用途权益的商品新增资源价计0，仍计算依赖费用；不同资源中的重复依赖按各自声明分别保守计入，不宣称组合折扣。estimated_total_cents只在选品与费用信息完整时给出，缺口或未知保留null；known_estimate_cents保留已知商品价和依赖费用上限的估算小计，未知部分不能当0。已知小计超限时，即使总额未知也会标over_budget。估算不含模型、人工、渠道和运营成本，不是报价或实际费用。

## 衔接取用

通过后，文本由宿主按任务授权用market_acquire/market_read或TaskResources.use处理；素材用可信本地read_asset或market_deliver.py实际传输，并在执行时复核。需要依赖费用的原路径仍要求绑定共享任务预算；预检不会替代它。付费商品无适用权益时仍为payment_unavailable。安装、代码执行、更多格式和真实用途权利仍待实现或验收；PNG/WAV文件交付不等于任务成功。

准备CLI只读既有账本；缺失、空库、错误版本、快照或未完成恢复的账本被拒绝，不自动初始化。不保存任务正文或学习轨迹；输出由宿主按本次任务需要保存。输入文件上限32KiB，未知字段和重复需求编号拒绝。原文件、市场、许可和账号不被改写。
