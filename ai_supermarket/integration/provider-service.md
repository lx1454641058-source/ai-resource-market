# 用配置启动资源站的JSON交付服务

v0.124，2026-09-23，本项目原创。已有合法API商品和JSON查询接口的网站，可用配置启动独立供应商服务，不再修改演练中的固定商品或编写HTTP启动程序。复用[原接口适配](provider-json.md)、在线原操作授权、持久防重和现有买方交付协议。

这是供应商内部的本机服务入口，默认只监听127.0.0.1。它不注册卖方、不上架或批准商品、不接钱包，也不自动创建市场中的买方连接。跨机器HTTPS入口及生产权限、运维由部署方在获准范围内另外配置；启动本机服务不能当作生产上线。

市场运营方可按[市场API连接配置](api-connections.md)将已约定端点接入通用HTTP启动器，双方使用一致的provider、binding_id及独立连接密钥。市场侧使用既有买方任务授权，供应商侧保留原商品发行白名单与在线核权，不共享彼此数据库。

## 先备好原站资料

沿[批量上架](supplier-batch.md)或[增量同步](supplier-sync.md)提交商品，由独立审核角色批准。价格和佣金在原上架/报价系统管理，不由本服务覆盖。下面的configure可以从已有卖方接口读取指定发行，无需手抄api和release_digest；也可继续使用已核验的完整配置。

## 从审核后的商品生成配置

供应商只需提供自己的部署部分deployment.json，字段是binding_id、listen_port、backend和result_checks。它们与[完整样例](provider-service.example.json)的对应字段相同，例如：

```json
{
  "binding_id": "supplier-connection",
  "listen_port": 8771,
  "backend": {
    "endpoint": "https://data.example.invalid/stock",
    "query_map": {"warehouse": "location"},
    "fixed_query": {"format": "json"},
    "timeout": 10
  },
  "result_checks": [
    {"path": ["count"], "type": "integer", "minimum": 0},
    {"path": ["warehouse"], "type": "string", "input": "warehouse"}
  ]
}
```

上例是占位部署说明，业务字段必须匹配自己的审核商品。端点、连接编号和业务规则由供应商可信配置提供，不能从商品正文或用户请求中执行指令，也不在此文件放认证值。

```powershell
python -B tools/market_provider_service.py configure --url https://YOUR-MARKET-HOST --credential-file work/site/seller-credential.json --seller YOUR-SELLER --id YOUR-SELLER:YOUR-RESOURCE --version YOUR-VERSION --deployment work/site/deployment.json --output work/site-release
```

configure最多发两次既有只读请求：核对已认证卖方与原操作授权路由，再读取指定id/version的seller_status。只接受自己的、已批准且当前在售的API商品，复用原version_hash核对包含元数据的API版本摘要，再沿市场原market-release-v1算法生成发行摘要。它不是凭名称推测摘要，也不是独立验证资源许可；信任来源仍是指定市场和已认证响应。

输出为全新目录中的provider.json、observation.json和receipt.json；标准输出仅是小型回执，不回传商品正文/许可或凭据。只有三份文件全部写完才算配置生成成功；磁盘失败留下部分目录，不覆盖。已存在的输出目录在联网前拒绝，不自动更新成最新发行。查询失败或状态不符停止，无自动重试，不调用后端、不上架、建库或启动服务。

observation.json保存该次卖方观察和当前offer，并保留原元数据字段顺序以复核既有API版本摘要；其中价格不是服务的报价承诺。配置只使用该商品的原api与不可变发行条件，当前价格和可用状态仍由市场检查。两次请求不构成事务快照，生成之后也可能停售或撤销；每次执行继续在线核权，不能把配置回执当持续批准。

核对生成内容后，把下面prepare命令的`--config`改为`work/site-release/provider.json`，再提供原供应商连接密钥及可选私有后端凭据。configure不读取或复制这些密钥，不创建市场侧连接，也不决定真实经营主体/许可、价格或额度。凭据轮换及在用服务迁移沿后文限制。

## 准备服务的私有材料

复制[配置样例](provider-service.example.json)到自己的私有工作目录，替换全部占位值：市场地址、原卖方、key_id、提供方与连接编号、原商品发行、原api字段、实际后端和结果检查。样例不是获准商品或有效身份，不能直接对外使用。当前每个实例固定一个商品发行及一个API操作；多个商品分别规划连接和原操作库，不混用编号来绕过防重。

另准备已有凭据文件，内容不放商品、模型或普通日志：

- seller-credential.json：市场已颁发的原卖方凭据，格式沿用RemoteMarket，key_id须与配置一致。
- provider-key.json：双方已有供应商连接密钥，格式为`{"key_hex":"既有密钥的十六进制"}`，至少32字节。不是支付宝凭据，也不是此工具生成的新身份。
- 可选backend-headers.json：原站已经批准的Authorization或X-API-Key头对象；无认证则省略此文件参数。不要把凭据写在backend端点或固定查询值里。
- 可选backend-query.json：仅在原站要求查询参数认证且该传输已获准时使用，内容为参数名到密钥文本的JSON对象；通过下面的文件参数传入，不能写入商品、模型输入或普通fixed_query。最多4项，值为8至2048个非空白可打印ASCII字符；其他格式使用专门适配。这里只新增文件入口，不创建或读取任何已有真实账号的密钥。

## 准备与启动命令

在源码根目录，用已有项目HTTP依赖的Python执行。所有路径指向自己已批准的材料，prepare输出必须是全新目录。

```powershell
python -B tools/market_provider_service.py prepare --config work/site/provider.json --seller-credential work/site/seller-credential.json --connection-key work/site/provider-key.json --directory work/site-service
python -B tools/market_provider_service.py check --directory work/site-service
python -B tools/market_provider_service.py probe --directory work/site-service
python -B tools/market_provider_service.py serve --directory work/site-service
```

有后端认证头时，prepare另加`--backend-headers work/site/backend-headers.json`。命令只接受文件路径，不把Token或密钥放命令行。

有查询参数认证时，prepare另加`--backend-query-credentials work/site/backend-query.json`。值只在供应商进程向固定后端发请求时合入；参数名不能与业务映射或固定参数重名，业务输入无法覆盖。原后端及其代理仍能看到请求URL，供应商须配置该链路的日志隐藏规则；本工具不能管理外部日志。

| 命令 | 实际行为 |
|---|---|
| prepare | 先检查完整配置和材料，再复制到新私有目录，创建一次原操作库，固定文件摘要。不联网、不检查真实卖方资格或原接口、不启动服务。 |
| check | 只读检查配置、凭据编号与原操作库。不联网，不创建缺失库，不代表市场身份或接口已连通。 |
| probe | 请求市场工具目录，核对当前卖方身份和原操作授权接口。不会访问业务后端，operation_authorized=false，也不代表具体商品/调用获准。 |
| serve | 前台启动本机/api入口，接受原签名协议；每次执行/查询都经原市场授权及固定发行白名单。收到合法请求可能访问实际后端，后端GET也可能计费。Ctrl+C停止，保留原库。 |

无查询认证时目录仍包含原六个文件；启用时另存backend-query.json，binding.json使用v2绑定格式并固定七文件目录中五份输入文件的摘要。provider.json格式仍为v1，市场HTTP/MCP协议和SQL表未改变。不要上传整个目录。只有prepare允许创建操作库；后续文件缺失、空库、配置或凭据变化都会停止，不创建新身份或空记录来继续。

新程序可读取原六文件目录；不能往在用旧目录直接加backend-query.json作为升级，启动和运行中均会拒绝。新绑定不能交给旧程序运行；若已有未决操作，须由有权运维规划保留原账本的迁移，本工具不提供自动迁移/轮换。新私有目录的准备不等于替已有操作重建目录的许可。

结果进入业务检查和操作缓存前，会拒绝解码JSON键值中的私有查询密钥原文及常规URL编码回显；数字回显也检查。拒绝后保留未知，不交付、不存为成功结果，不自动重发。这不是任意编码、拆分或恶意加密泄露的检测器，不能替代可信后端与业务检查。密钥恰好出现在正常数据中也会停止，需人工核查，不能临时关闭检查重新GET。

市场侧继续使用ApiProviderClient连接本服务的固定`http://127.0.0.1:<listen_port>/api`，共用原provider、binding_id和连接密钥，再接既有ApiDeliveryHost。该本机地址只适合同机联调；原站/市场在不同机器时须通过已经获准的HTTPS部署连接。不能把配置检查的成功当作买方交付已完成。

## 配置中的结果检查

result_checks最多32项，全部必须通过。每条path是字段路径数组，字符串访问对象字段，整数访问数组下标；空数组指根节点，最多8层，数组下标0至1000。不执行表达式、Python模块或任意代码。

| 字段 | 含义 |
|---|---|
| path、type | 必填；路径必须存在，type为object/array/string/integer/number/boolean/null。boolean不能冒充整数。 |
| equals | 可选；要求与固定JSON值一致，且类型符合声明。适合明确成功码、固定指标或国家。 |
| input | 可选；与api中某个原输入值严格一致，不能和equals同时使用。输入缺失则不通过。 |
| minimum、maximum | 可选；仅数值类型可用，边界包含，最小值不能大于最大值。 |

样例要求count为非负整数，warehouse与原输入一致。实际网站应把错误码、数据范围、空结果和其他关键业务条件写清；规则通过仅说明满足所写条件，不能代替数据真实性或质量验收。条件超出此表达能力时，使用已存在的Python宿主适配和专门业务检查器，不删除规则强行通过。

## 重启、变更和验收

配置、原连接凭据、后端认证头和可选查询凭据在准备时固定摘要；运行时授权也核对原文件，缓存不会绕过变更。当前没有热更新、密钥轮换或跨库迁移命令。需要变更时先停新调用并核查原未决操作，保留原库，由有权运维规划迁移；不能prepare一个空目录、换编号来重做未知请求。

已完成操作重启后复用原结果；未知操作仍未知，当前配置入口不提供后端lookup，不能自动GET重试。规则失败、响应丢失、原身份撤销或网络不可判定都保留原记录。服务端拒绝浏览器Origin、错误Host及非127.0.0.1入口，请求/并发限制沿用现有有界HTTP处理。

验收应实际完成：买方原权益→在线操作授权→原站GET→结果与许可证文件→逐次收货回执；再重启供应商并重放同一操作，核对原站未被再次请求。测试已用真实CLI进程、HTTP与隔离夹具完成这些步骤，未使用真实供应商、模型或付款。本说明不表示真实资源权利、渠道资格、生产部署及财务结算已完成。
