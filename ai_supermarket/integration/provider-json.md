# 把网站已有JSON查询接口接到超市

已有审核商品的网站可使用[配置式服务入口](provider-service.md)，无需修改固定演练商品或编写HTTP启动代码。复杂业务规则仍可沿用本页的Python检查器方式。

v0.120，2026-09-23，本项目原创。`ProviderJsonQuery`供资源站自己的可信接入程序使用，把已批准的API输入映射到固定GET接口，接在[供应商持久操作账本](provider-operations.md)后面。网站可复用请求编码、网络等待、响应大小和JSON检查；仍负责商品许可、授权、参数范围与业务结果判断。

适用于网站已有、获准转交给买方的只读JSON查询接口，包括固定服务端认证头。它不是任意网页抓取器，不接POST写操作、HTML、文件下载、分页聚合或任意URL。接口返回HTTP 200不代表业务成功，因此必须配置业务检查器。

v0.123补充：可信供应商宿主可传入`private_query`字典，支持原站要求的私有查询参数认证。该字典来自私有凭据文件，不能从模型输入、商品内容或固定业务配置生成；配置式入口使用[单独文件参数](provider-service.md)。最多4个参数、每值8至2048个非空白可打印ASCII字符，不得与固定参数或业务映射重名。适配器复制配置，编码时合并，查询总长度仍限制16KiB。

私有查询密钥可识别的原文、URL编码或JSON转义回显会在业务检查和成功缓存前拒绝；不声称检测任意变换。原站和其访问日志仍可能接触完整URL，须由供应商控制。原固定认证头方式保持兼容，新增回显检查仅针对private_query，不自动分析或拆分认证头。没有真实凭据轮换或生产权限变更。

## 接入现有供应商服务

```python
from ai_supermarket.provider_json import ProviderJsonQuery
from ai_supermarket.provider_operations import ProviderOperations

def valid_stock(result, inputs):
    # 示例字段；供应商须按自己真实的成功/错误响应调整。
    return (
        isinstance(result, dict)
        and set(result) == {"count", "warehouse"}
        and type(result["count"]) is int
        and result["count"] >= 0
        and result["warehouse"] == inputs["warehouse"]
    )

backend = ProviderJsonQuery(
    approved_api_descriptor,             # 已审核商品中的api字段
    approved_existing_json_endpoint,     # 固定HTTPS地址，不含查询串
    {"warehouse": "location"},           # 商品输入→原站参数名
    fixed_query={"format": "json"},
    result_check=valid_stock,
    headers=provider_held_headers,        # 无认证时为{}；不是买方请求头
    timeout=10,
    enabled=True,
)
operations = ProviderOperations(
    original_operation_ledger,
    provider_id, connection_binding_id, market_connection_key,
    authorize=check_original_authorization,
    execute=backend.execute,
    # 未配置lookup：后端结果未知时保留未知，不重新GET。
)
```

网站原HTTP入口继续把有界正文和签名交给`operations.handle`。`check_original_authorization`可沿用[原操作在线授权](provider-authorization.md)，同时保留网站自己批准的商品发行白名单；不要使用无条件通过的授权函数。模型只见原API调用工具，不得到后端地址、认证头或数据库。

商品的operation、输入字段和结果上限来自固定descriptor；query_map必须逐一覆盖全部输入字段，不得重复映射或覆盖fixed_query。字符串、整数和布尔值作为查询值编码，布尔值为true/false；输入不能改变域名、路径、请求方法或固定参数。构造时复制配置，不能通过修改原字典改变在用适配器。

## 交付和失败行为

- 默认为enabled=false；打开前由网站确认数据转交范围及后端费用，GET也可能计费。
- 固定HTTPS；HTTP只允许数字本机IP。地址中不接受用户信息、查询串或片段。认证支持供应商固定的Authorization、X-API-Key头及上述独立private_query，不接受Host等任意请求头；凭据不要放普通fixed_query、商品、模型或普通日志。
- 不读取环境代理，不跟随跳转，不自动重试或翻页。编码后请求路径和查询最多16KiB；结果受商品result_max_bytes约束，最多64KiB。
- 只接收200、JSON、UTF-8或未声明字符集、未压缩响应；重复关键头、冲突长度、损坏/重复JSON字段、超长或业务检查失败都作为未知。异常不回显原站错误正文或认证值。
- 默认等待10秒，允许0至30秒内的正值；读取阶段使用剩余期限。DNS仍受操作系统行为影响，不能宣称这是包含DNS的绝对墙钟上限。
- 业务检查器取得结果和输入的副本，必须明确返回True；修改副本不会改写交付数据。需验证错误码、实际数据范围和空值含义，不能只检查JSON可解析。

持久防重由ProviderOperations负责，直接重复调用backend.execute会再次GET，不能把裸适配器直接作为买方工具。供应商重启必须沿用原操作库。已完成操作复用原结果；响应丢失、进程中断或不明结果保留reserved，未配置真实后端lookup时只能保持unknown。不能因为查询“只读”就重发一个可能产生费用的请求；也不能把查不到当作已取消。

若原站支持按原操作编号核查，可另行提供真实lookup函数。当前GET适配器不会向原站虚构幂等字段，也不能从一个没有操作查询接口的网站恢复丢失的响应。特定认证方式、复杂POST查询、分页、流式大结果或业务错误恢复须使用专用后端，不能删掉检查强行兼容。

## 本轮证据

本机测试覆盖真实HTTP请求、输入编码/固定配置、异常响应、超时，以及市场在线授权→供应商操作库→原JSON接口→买方结果/许可证/收货回执。供应商重启后相同原操作没有第二次GET；缺授权及未知恢复也不派发新查询。夹具为原创虚构数据，没有真实供应商或付款。

此外，2026-09-23使用本适配器对[世界银行公开人口接口](https://api.worldbank.org/v2/country/CHN/indicator/SP.POP.TOTL?format=json&date=2024&per_page=1)执行一次固定HTTPS读取，成功保存2024年中国人口记录。接口参数依据[官方API说明](https://datahelpdesk.worldbank.org/knowledgebase/articles/898581)；该指标[元数据](https://databank.worldbank.org/metadataglossary/world-development-indicators/series/SP.POP.TOTL)标注CC BY 4.0并列出原始统计来源，另须遵守[世界银行数据附加条款](https://datacatalog.worldbank.org/public-licenses)。保留来源和署名，数据仅重新格式化为JSON；没有据此声称签约、背书、收费资格或将商品自动上架。

真实公共接口读取只验证后端访问与结果检查；不能代替真实供应商准入、生产TLS部署、合同/数据范围核对、付费采购与财务对账。接入失败时停用新派发并保留原操作库，沿原记录核查，不删除未知占用。
