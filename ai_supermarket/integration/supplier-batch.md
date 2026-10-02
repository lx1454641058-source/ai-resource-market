# 供应商商品清单批量接入

> v0.100补充[增量预览和执行](supplier-sync.md)：既有商品可先比较当前状态，再明确改价、草稿修订或新版本需求；下文send仍保留仅创建/提交行为，不自动变成同步操作。

2026-09-23，v0.99。本项目原创。资源网站可从自有目录导出JSON清单，一次检查至多100项商品，再通过获准卖方账号分批创建草稿并提交审核。免费与收费商品使用同一流程，价格沿用供应商清单；不自动批准发布、付款或下载原站文件。

## 准备清单

```powershell
python -B tools/market_supplier_batch.py sample --output work/supplier-manifest.json
python -B tools/market_supplier_batch.py prepare --input work/supplier-manifest.json --output work/prepared-supplier-batch
```

sample生成完整原创内部演练样例，不授予供应商其他内容任何权利。真实清单结构为：

```json
{
  "format": "market-supplier-batch-v1",
  "seller": "your-seller-principal",
  "batch_id": "export-20260923",
  "items": [
    {
      "source_id": "stable-source-item-1",
      "request": {
        "slug": "your-resource",
        "version": "1.0.0",
        "metadata": { "...": "完整商品字段，见单项卖方契约" },
        "content": "# 商品正文或原站交付说明"
      }
    }
  ]
}
```

上面的metadata只是结构示意，不能直接提交。真实字段包括标题、摘要、关键词、输入/输出、检查样例、来源、完整许可、允许用途、自定price_cents和usage_contract；见[单项卖方准备](seller-preparation.md)。许可不能从示例商品照搬，字段检查不证明权利真实。

source_id为稳定来源编号，使用字母、数字、点、横线、冒号和下划线；不作为URL或文件路径执行。原站文件沿用metadata.origin中的provider、resource_key、摘要、大小、媒体类型，文件继续由已配置的原站交付链路提供。工具不爬网页、不追随正文链接、不执行商品代码；固定文件协议不等于动态API调用或计费。

整批上限4MiB、1至100项，单项保持正文24KB、许可8KB、请求128KiB限制。大目录可分页导出多批，无需让AI读完整目录。相同source_id或slug/version重复时，所有冲突项停止；无效项不妨碍检查其他项，根结构无效则整批停止。

输出目录必须不存在。plan.json保留有效项完整请求和逐项错误，report.json提供小型摘要；输出plan_sha256由接入者保存为发送预期值。准备不联网、不建市场记录、不审核版权。不要重新计算被修改计划的摘要来绕过拒绝；原内容仅留在获准本地目录，不把整份清单交模型或日志。

请求编号由卖方、稳定来源编号和规范化商品内容生成；仅改变JSON字段顺序或导出批次不会改编号。内容/价格变化会改编号，但不会覆盖已有同版本商品：草稿走显式修订，价格走原改价接口，已批准正文要发新版本。当前不自动镜像删除、撤回或改价。

## 显式发送

```powershell
python -B tools/market_supplier_batch.py send --directory work/prepared-supplier-batch --expected-plan-sha256 YOUR_SAVED_SHA256 --url https://YOUR-MARKET-HOST --credential-file /absolute/path/seller-credential.json --state-directory work/supplier-send-state --max-items 10
```

地址、摘要、身份路径须换成接入者已有配置。send会把有效项正文、许可、价格及原站声明发送到指定市场，只在对象与内容已获准时执行；不会自动使用本项目账号对外发送。凭据只由可信程序读取，不交模型。公网须HTTPS，仅数字本机地址允许HTTP，不跟重定向或环境代理。

写入商品前先通过认证发现检查role=seller及seller_principal与清单一致；旧服务缺少该字段时停止，不降级跳过。新版发现只为已认证卖方补充此字段，原工具参数兼容。

发送目录固定原计划摘要、端点与凭据编号。每项先保存固定请求，seller_draft成功后保留原id/version/revision，再用固定编号调用seller_submit。**成功仅表示提交审核，不能当作已上架。**后续seller_status用于核对当前审核状态；批次submitted_for_review是历史提交回执，current_review_state_checked=false。

默认每次最多处理10项未完成商品，可设1至25；60秒后停止启动新步骤，在途请求仍受15秒等待限制，实际总时长可能略长。网络未知、限流或业务错误停止这次发送，无自动重试，也不继续写后续商品；限流返回retry_after_seconds供接入者判断。离线准备已判无效的项跳过并保留错误。

显式重跑同一命令会跳过成功步骤，未确认步骤沿原编号恢复。草稿或提交响应丢失时，服务端幂等记录阻止重复创建，客户端识别replayed标记。每步骤最多4次持久化尝试；达上限须核查，不删记录重置。发送目录包含待发送正文，应按供应商内容权限保存。

| 返回状态 | 含义 |
|---|---|
| batch_partial | 尚有有效项未完成，核对interruption或继续原批次 |
| batch_needs_changes | 有效项已有提交回执，但清单仍有无效项待修改 |
| batch_submitted | 所有项已有审核提交回执，不证明当前上架状态 |

停止工具即可停止后续发送，保留服务端草稿及本地记录，不自动撤回、删库或覆盖已有成果。

## 证据与限制

本机HTTP测试覆盖免费/收费商品、自定价保留、卖方核验、逐项错误、分段发送、两种响应丢失、限流及原请求恢复；单独审核后才能上架。现也可准备含v4契约的[API商品](api-product.md)，实际执行与交付由独立API宿主负责。没有上传真实供应商内容、真实付款或公开部署；真实网站、商户/钱包和实际接入成本收益仍待验收。
