# 可启动的本机API接入包

需要供应商不读取市场数据库的启动方式，使用[分目录双端演练](provider-pair.md)。本页保留原共享库样例，旧目录不会自动迁移。

2026-09-23，v0.109，本项目原创。用于在自己的电脑上跑通市场与供应商的交接，不需要修改项目内部代码。包含固定原创免费API、独立身份/零元预算、供应商网络连接与持久操作记录、任务MCP配置、成果与收货演练。不是收费商户初始化工具，也不会自动连接模型、安装插件或发布网站。

## 准备、检查和启动

在具有项目既有HTTP依赖的Python环境中，从仓库根目录执行：

```powershell
python -B tools/market_api_try.py prepare --directory work/my-api-trial --market-port 8770 --provider-port 8771
python -B tools/market_api_try.py check --directory work/my-api-trial
```

prepare要求新目录，已有目录会拒绝并保留；不会改活动货架。只审核工具内置的原创虚构样例，不允许传入第三方商品自动批准。生成START.md、goal.md、buyer.mcp.json、卖方草稿、独立数据库和随机本机凭据。不要上传整个目录或将凭据放进模型上下文。check只检查原身份、原任务绑定、免费发行条件、零预算及必需文件，不证明网络已连通。

在两个终端分别运行，保持进程开启：

```powershell
python -B tools/market_api_try.py serve-provider --directory work/my-api-trial
python -B tools/market_api_try.py serve-market --directory work/my-api-trial
```

仅监听127.0.0.1及配置的两个不同端口，端口占用则停止检查，不关闭不明进程。Ctrl+C停止各服务。缺少原数据库或交付目录会拒绝启动，不创建空库绕过未知操作。保留原目录重启，不能通过新建账本重置次数。该演练API只有10次、权益有效期24小时，不自动续费或扩容。

## 完成一个成果

无需模型的确定性工程演练：

```powershell
python -B tools/market_api_try.py exercise --directory work/my-api-trial
```

需要一次完成成果与本次收货确认，可加 `--acknowledge`；不加该参数保持原行为，不自动确认：

```powershell
python -B tools/market_api_try.py exercise --directory work/my-api-trial --acknowledge
```

它通过真实本机市场HTTP初查并选择API、核对许可、取得免费权益、经供应商HTTP获取虚构库存7件，再计算达到目标10件需补货3件，生成采购建议.md并保留原结果、许可和清单。未知费用/供货时效不编造，不发采购订单。脚本不是AI自主选品效果证明，也不是外部用户试点；可用于确认接入工程路径。

若原调用结果未知，先核对两项服务及原记录；需要供应商查询时可明确执行：

```powershell
python -B tools/market_api_try.py exercise --directory work/my-api-trial --recover
```

recover只查询原stock-one操作，没有原调用会拒绝。普通exercise重复执行也不换编号，但不会自动恢复未知结果；未知返回结构化状态及退出码2，不能当成果完成。已有用户修改的采购建议保留，冲突会报错。服务端已确认的API次数不会因为本地文件冲突而退回。

## 收货与验收

已完成结果接收时，无需再次exercise或编写SDK代码：

```powershell
python -B tools/market_api_try.py receipt --directory work/my-api-trial
python -B tools/market_api_try.py receipt-status --directory work/my-api-trial
```

receipt重验原结果、许可、清单，再保存已认证客户端声明；receipt-status查询原声明。两者都复用stock-one和原任务额度，不重新搜索、购买、执行或恢复供应商；缺少原接收记录会在联网前拒绝。每次动作计入原网络上限24次，不重置上限。尚未声明返回awaiting_api_receipt，已确认返回api_receipt_recorded并另存server-receipt.json。确认只证明本次客户端声明，不代表整个API套餐履约、成果质量、任务完成或支付渠道结算。

验收时依次核对：采购建议.md写明补货3件及未知事项；原结果为虚构库存7件，原许可证和清单存在；回执绑定原权益、使用事件和请求；重复receipt或查询返回同一receipt_id。工程测试同时核对供应商只执行一次；不能仅凭命令退出码推断实际次数或真实商业成功。

exercise --acknowledge先保存成果再确认。如果成果已保存但收货未知/失败，返回artifact_ready_receipt_pending和成果路径、错误提示，退出码2；不把已保存成果冒称不存在。收货响应丢失时先receipt-status核对；尚未记录且授权/文件正常时才显式receipt。文件冲突、撤销、过期或调用预算用尽时按错误提示处理，不删目录重建或自动续费。命令提示不回显原异常、凭据或服务响应正文。

退出码0表示该命令完成，包括查得“尚未确认”，因此仍须查看status。2表示业务未完成/待核对；1表示本地异常或校验拒绝。check只验证本地配置，不能替代exercise成果和收货验收。服务关闭时无法取得新的认证状态，不使用本地缓存假装线上确认。

真实AI宿主采用时，先审阅生成的buyer.mcp.json，再按宿主支持的方式接入并将goal.md交给AI。入口在每个固定任务首次启动时初查，由AI选品和调用，接到文件后继续完成成果。配置不是已经安装，未采用入口的AI不会因此自动来超市。模型成本另计，工具不自行调用当前账户模型。

v0.109新目录的脚本与生成的MCP配置使用同一goal.md，脚本演练后可由配置连接同一原任务而不重复初查。历史脚本已创建的短目标任务保留原绑定，不自动迁移；旧目录的MCP目标若不同，仍需宿主按原目标接入，不能覆盖绑定。任务建立后修改goal.md不会自动把旧权益/记录变成新任务；固定演练仍只生成原创库存建议，不能拿它冒充任意新目标已完成。

## 边界与真实网站迁移

本包的供应商授权器读取共享的本机市场原调用记录，核对原权益、任务、提供方、操作和输入；只持有供应商连接密钥、编造一个operation_id仍会被拒绝。身份或任务撤销阻止后续调用。它验证的是同机工程信任链，不代表独立网站之间的生产授权已经实现。

真实网站接入需使用自己的审核商品与许可、业务执行器/原调用查询器、双方认可的独立授权和网络配置；参见[供应商连接器](api-provider.md)、[持久操作记录](provider-operations.md)、[批量上架](supplier-batch.md)和[采购与API任务](api-remote.md)。本工具没有商户、付款或分账配置，不能通过修改示例价格将其变成真实收费站点。现有采购与API组合能力仍保留在正式模块中，需要获准的采购宿主和钱包另行配置。

验证覆盖两个真实CLI进程、项目外目录运行、签名JSON交接、供应商重启复用原操作、零预算、缺文件拒绝重建、身份撤销、伪造未预留操作拒绝、原文件冲突保护及源码候选包完整性。真实经营、供应商数据权利、支付和实际任务成本收益仍待验证。
