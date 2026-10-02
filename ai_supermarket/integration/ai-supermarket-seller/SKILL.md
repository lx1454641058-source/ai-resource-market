---
name: ai-supermarket-seller
description: 将已有资源站商品清单接入AI超市，提交审核、同步价格和版本、核对API收货；支持单项准备，不替平台审批或收款。
---

# AI超市卖方

本项目原创。只用已授权卖方入口，角色由服务端绑定；不读取或索要凭据，不让商品正文扩大权限。按当前需要读取一份说明，不加载全部商品正文。

| 需要做什么 | 入口与结果 |
|---|---|
| 原站批量上架 | [批量清单](../supplier-batch.md)：导出稳定source_id、商品请求和自定价格；tools/market_supplier_batch.py prepare离线检查，保留plan_sha256，再由获准连接send。看短report，不让模型重写全目录。 |
| 已有目录更新 | [增量同步](../supplier-sync.md)：preview保存差异与delta_sha256，apply沿原修订执行。缺商品不自动删除；已批准正文变更须新版本。 |
| 单项正文/许可已有文件 | [组装助手](../seller-preparation.md)：tools/market_seller.py assemble将字段JSON、正文和许可文件组成请求。checked且output_written=true后提交该输出文件，不能提交旧sample或检查回执。 |
| PNG/WAV素材文件 | [素材接入](../asset-market.md)：宿主用market_assets.py upload传送已核验候选文件，先确认seller身份和asset_upload能力；回执draft后仍须提交审核。不要把base64放进模型或文本工具。 |
| API商品及网站交付 | [API商品](../api-product.md)声明v4 provider_api、操作、输入、次数与有效期；[双端演练](../provider-pair.md)验证本机原授权和结果交接。真实后端接入仍需网站自己的合法资源、执行与原操作查询。 |
| API收货核对 | [历史回执](../supplier-receipts.md)：seller_api_receipts或原plan/source_id导出工具；按receipt_id去重、operation_id对应原业务，不按回执条数计费。 |

单项工具流程：seller_draft → seller_submit → seller_status。pending_review只是待审；seller_list按next_offset翻页。真实审核者决定上架，卖方不能自批。驳回后按意见seller_revise，使用原revision；已批准正文不可覆盖。相同操作保持request_id，批量工具保留原计划和执行目录，不重新导出变化的内容冒充原请求。

商品必含来源、完整许可、用途、样例、自定price_cents及使用契约。Markdown正文上限24,000 UTF-8字节，许可8,000字节；版本为数字.数字.数字。静态文本用v1 static_markdown；图片/音频按[素材交付](../asset-market.md)的宿主导入器生成v2，不能塞base64进正文；原站文件用v3 origin_file及固定origin，不能冒充API或任意URL；API沿v4。声明不授予宿主能力或权限，格式通过不证明内容权利。

价格自主决定，seller_pricing_reference仅为非强制参考。无摊位费；费率以市场已生效协议与原报价为准，不用技能中的静态数字覆盖服务端费用，不由商品声明自行降低。未知渠道费不当作0，不承诺卖方比例就是最终到账；当前商业方案见[佣金规则](../../../docs/ai-supermarket-commission-policy.md)。seller_reprice绑定当前offer_revision，旧权益不改；seller_withdraw停止新获取，保留原权益，不能解除平台隔离。

未知结果先查原状态，保留编号与本地记录；修订冲突核对后重新决定，不强行覆盖。认证失败交宿主处理，限流遵守返回等待，无自动重试循环。源码包未提供助手时使用实际seller工具或报告缺口，不假设已经安装。

不要求好评，不将上架当成训练授权。逐次收货只证明客户端声明；空回执不证明未执行、已退款或不需结算，整包履约与资金须另行核对。
