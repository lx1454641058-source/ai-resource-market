# 已授权采购的配置接入

本项目原创，v0.134。把已有采购授权、支付宝适配器和原站文件连接接到通用HTTP服务，沿用原采购接口，不再要求接入者手写组件注入代码。钱包执行仍由买方获准的宿主负责；此工具不扣款、不配置钱包、不授予新权限。

## 必须已有的条件

- 三个原账本：市场、身份和预算。买方凭据编号已绑定原预算任务，身份、策略及任务均有效。
- 已由有权运营者记录的购买委托：同一买方/任务/账本路径/市场环境，包含该卖家、用途、单笔上限和有效期。预算额度本身不等于购买授权，配置文件不能替代委托。当前版本不提供新授权或恢复目录重绑命令。
- 已核准商户映射与渠道策略、商户RSA2签名私钥文件、渠道验签公钥文件。需要现有[支付依赖锁定文件](../requirements-payment.txt)对应环境。真实商户资格、金额单位和渠道接入依据不能用示例值代替。
- 需要原站文件时，已有供应商端点、连接凭据和交付许可；供应商须独立核原权益，连接签名不证明付款。

## 准备、检查和启动

复制[占位配置](purchase-connections.example.json)到本地私有工作目录并填写实际配置。示例的编号、摘要和路径不可直接运行；两个启用开关默认false。相对路径以配置文件目录为基准，准备后固定为原绝对路径。

```powershell
python -m ai_supermarket.purchase_connections prepare --config work/service-purchase.json --directory work/service-purchase-connections
python -m ai_supermarket.purchase_connections check --directory work/service-purchase-connections
python -m ai_supermarket.http_server --database work/service/market.sqlite --identities work/service/identities.sqlite --budget work/service/budget.sqlite --enforce-task-budget --purchase-connections work/service-purchase-connections
```

prepare只创建新连接目录、绑定摘要和每个任务的结果目录，密钥保留原路径引用，不复制。prepare/check不创建账户、任务授权、购买委托、订单或账单，不联网，不改变三个原账本。输出checked只证明本地配置核验通过，不证明实际支付或供应商服务可用。

启动器验证全部配置后接入原采购HTTP。启动会执行已有宿主的任务初查并保存本地查货记录，不调用模型。单卖家v1配置继续可用；[多卖家v2示例](purchase-connections.multi.example.json)允许同一凭据在tasks中列出多个获准卖家。最多32个渠道、128条任务与卖家的绑定记录；所有商户必须与市场账本既定环境一致。

## 同一任务跨卖家采购

v2沿用原字段：为每家卖家设置渠道策略，增加一条相同key_id的任务记录，引用已有的mandate_id与channel_id。一个凭据对同一卖家只能设置一个渠道；多条记录的买方、预算根、任务、用途、目标、市场和环境必须相同。配置不会创建卖家权限，各自的原授权仍须有效。免费资源或已有权益不需要新购买；无获准渠道的收费卖家会被拒绝。不同卖家共用原任务总预算，增加渠道不会增加可用金额。

AI仍只传id、version、request_id，不传商户、密钥或卖家路由。服务器根据市场原商品的卖家选用已配置渠道；同一任务的request_id须跨卖家唯一。生成采购记录后，付款核验、状态查询、账单恢复与交付都固定到原请求的渠道。用原编号换商品、换卖家或移除原渠道后重买会被拒绝，并发请求也共用原记录。未确认付款保留原预算占用，不自动换渠道或再次扣款。

文件收货回执也沿原请求选择卖家；免费文件通过已有交付清单查回商品。原站连接按各条任务配置提供，未配置的文件连接不会自动外连或安装。当前每家采购宿主仍执行本地任务初查，未为多卖家额外调用模型；尚未测定真实任务的成本收益。

`billing_enabled=true`启用既有账单与权益组件，仍逐单检查委托、预算和原报价。`verification_enabled=true`允许收到付款凭证后向适配器固定的官方渠道核验；prepare/check及启动本身不请求渠道。配置不接受自定义支付端点或可替换测试网关。真实启用应使用已获准账户、商户与网络范围。

运行中反复核对最初加载的配置及密钥摘要，文件替换会停止请求。即使磁盘上的配置与绑定文件同时重写，也不能扩大已运行进程的授权；须由有权运营者核对新配置后另行启动。缺失结果目录、原账本或密钥不会自动重建。运行错误不输出原始异常、密钥内容或支付凭证。

## 原站文件与API

原站连接在顶层origins中设置provider、endpoint、binding_id、key_file；任务的origins只列获准使用的provider编号。例如：

```json
{"provider":"approved-provider","endpoint":"https://supplier.example/market/file","binding_id":"agreed-binding","key_file":"../private/origin-key.json"}
```

该独立密钥文件格式为`{"key_hex":"双方获准连接密钥的十六进制文本"}`。禁止把真实密钥写进商品正文、模型提示或本示例。端点只能HTTPS，本机隔离演练允许HTTP环回IP。结果写入连接目录的`results/<key_id>/`，复用原请求目录和内容/许可校验，重启不换采购编号。

HTTP使用[既有采购下载入口](purchase-download.md)：已认证买方从`/v1/tools`发现purchase_api；请求资源后402返回原账单，经获准钱包处理后再提供Payment-Proof。服务器核验付款、记原费用并交付；普通重试只返回原状态，不自动重发账单或钱包请求。显式handoff才取回原账单，仍需原授权有效。文件包包含资源、LICENSE.txt和manifest.json，买方可声明原包已收到。

可与[API连接参数](api-connections.md)同时配置。共用同一凭据时用途必须一致，权益、API次数和任务预算仍分别核验。当前组合检查不代表任意真实API付费套餐已联调。

## 边界与核验

本轮验证了同一任务的两家卖家账单、共用预算上限、并发冲突、错卖家响应拒绝、本机真实启动/重启、两份合成付款正文交付，以及第二家卖家的付费/免费文件、许可证和客户端回执。既有原站本机交付、配置变化和撤权检查仍适用。没有真实付款、钱包、支付宝或供应商商业请求。

恢复副本核准新身份、原任务和新委托后，可以同一请求查询原订单并复用已授予正文权益；付款未决保持原占用，handoff不重发旧账单，详见恢复范围。

已知未完成：真实钱包/商户联调、生产部署、恢复后的采购授权重接及外部API账本联合恢复。原恢复副本仍撤销凭据、暂停预算；本配置不能通过改路径把历史委托复活，见[恢复范围](recovery.md)。不把本地测试视为可以真实收费的证明。
