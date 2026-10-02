# AI超市HTTP服务与远程机器接入

来源：本项目原创。HTTP是程序通过网络交换请求的方式；ASGI是Python应用与网络服务器之间的接口。本服务沿用原市场工具契约，通过Uvicorn接收网络请求，每次核验服务端身份后才调用业务。

当前已验证本机HTTP、卖方→审核→买方交付、凭据撤销和跨进程MCP适配。默认只监听127.0.0.1；没有公开部署，没有真实付款。MCP适配器在客户端运行，通过本地标准输入输出向AI提供工具，再调用远端HTTP；它不是原生MCP Streamable HTTP/OAuth服务。

素材卖方可选开启`--asset-uploads`，须已有schema 2素材库；增加认证二进制草稿入口，不改变JSON工具和原权限边界。格式、限额与宿主命令见[素材网络提交](asset-market.md#供应商通过网络提交素材)。默认关闭，尚未在公网部署。

## 安装与受信运营初始化

服务端需要Python 3.12及[锁定依赖](../requirements-http.txt)；本地市场、HTTP客户端及远程MCP适配器只需标准库。以下命令从项目根目录执行。选择新的独立环境，不升级原环境：

```powershell
py -3.12 -m venv D:\AI-Supermarket\http-env
$marketPython = 'D:\AI-Supermarket\http-env\Scripts\python.exe'
& $marketPython -m pip install --index-url https://pypi.org/simple --require-hashes --only-binary=:all: -r ai_supermarket/requirements-http.txt
& $marketPython -m pip check
```

账号和凭据由有权运营者在受保护的本机目录管理。普通买方/卖方AI只获得相应工具，不获得下面的管理命令或身份账本访问权限：

```powershell
New-Item -ItemType Directory -Path work/http-local-example
& $marketPython -m ai_supermarket.identity_admin --identities work/http-local-example/identity.sqlite init
& $marketPython -m ai_supermarket.identity_admin --identities work/http-local-example/identity.sqlite create-account --principal buyer-example --role buyer
& $marketPython -m ai_supermarket.identity_admin --identities work/http-local-example/identity.sqlite issue-key --principal buyer-example --output work/http-local-example/buyer.credential.json
```

签发只向新文件写入凭据，控制台只输出非秘密编号及有效期；目标存在即拒绝，不覆盖，也不签发第二把钥匙。写入失败会尝试撤销本次签发，保留失败文件供运营者检查。签发失败不能直接复用该文件。默认30天、允许60秒～90天；轮换新旧凭据时不要把“签发新钥匙”当作“旧钥匙已撤销”。

卖家使用seller账号；审核者使用独立reviewer账号。新增可选[受控自助入驻](enrollment.md)：买卖方凭邀请领取限定角色，审核者仍由运营单独管理。**尚未完成无限制公众注册、主体核验和跨设备邀请交付**。Windows下文件继承目录访问权限，本工具未自动配置ACL（即哪些账号可以访问文件的权限表），因此不能据文件已生成便宣称生产密钥存储合格。密钥文件不进入模型上下文、聊天、URL或代码仓库；示例work目录已被Git忽略。

## 启动与连接

在服务端独立终端启动：

```powershell
& $marketPython -m ai_supermarket.http_server --database work/http-local-example/market.sqlite --identities work/http-local-example/identity.sqlite --port 8765
```

新业务账本在第一次有效业务调用时建立；身份账本必须预先初始化。旧v1账本仍拒绝隐式迁移，请先按[复制升级说明](feedback-evidence.md)处理。live只证明进程能响应；ready检查账本读取与恢复门，不能替代支付、磁盘或运营验收。可选`--budget`和`--measurement`分别加入预算与计量检查；四库恢复后的市场要求提供同批次计量账本，否则not_ready，详见[就绪与恢复说明](recovery.md)。仅传这两个参数不启用支出控制；额外启用`--enforce-task-budget`才约束静态资料read，具体绑定与失败处理见[远程任务预算](task-budget-gateway.md)。仍不执行或计费外部供应商调用。

接入方宿主保管相应凭据文件后，可以生成可审阅的配置：

```powershell
py -3.12 -m ai_supermarket.remote_cli --url http://127.0.0.1:8765 --credential-file work/http-local-example/buyer.credential.json config
py -3.12 -m ai_supermarket.remote_cli --url http://127.0.0.1:8765 --credential-file work/http-local-example/buyer.credential.json describe
py -3.12 -m ai_supermarket.remote_cli --url http://127.0.0.1:8765 --credential-file work/http-local-example/buyer.credential.json mcp
```

config只输出绝对路径配置，不读取或显示密钥，也不安装到AI客户端。describe从服务取回当前角色工具；mcp提供固定2025-11-25标准输入输出协议。主机地址、凭据路径和角色不是模型工具参数。凭据每次请求从文件读取，服务端每次认证；工具列表在MCP进程启动时固定，配置或接口版本改变后须重新接入。

接入技能见[买方](ai-supermarket-buyer/SKILL.md)和[卖方](ai-supermarket-seller/SKILL.md)。实际AI每任务触发及完整用量仍需宿主验证，MCP配置和协议演练不等于任意AI都已接入。

## HTTP契约

| 方法/路径 | 身份与输入 | 响应 |
|---|---|---|
| GET /health/live | 无凭据，仍受Host/来源/请求频率检查；无正文/查询参数 | alive及API版本；不是就绪检查 |
| GET /health/ready | 与live相同的网络边界；不初始化缺失账本 | 200 ready或503 not_ready；只返回状态，不泄露依赖详情 |
| GET /v1/tools | 单个Authorization: Bearer凭据；无正文/查询参数 | 当前角色、instructions、工具名称和JSON参数定义、工具版本0.3.0 |
| POST /v1/tools/call | 相同凭据，application/json UTF-8；正文仅tool和arguments | 原业务结构化结果，一份JSON，不重复附文本副本 |
| POST /v1/enroll（可选） | 仅配置已初始化的--enrollments时启用；无已有Bearer凭据，核验invitation与本地生成credential；正文最多4KB | 固定买方/卖方角色、账号及凭据编号和到期时间，不返回秘密；详见入驻说明 |

调用正文示例：

```json
{"tool":"market_search","arguments":{"q":"产品简报","purpose":"commercial_use"}}
```

角色工具数量仍为买方7、卖方8、审核4。先取工具定义，按当前必填参数执行；获取和读取分开，商品正文不进入搜索摘要。邀请领取走单独可选入口；邀请签发/撤销、通用账号管理及改角色不向网络或模型开放。

| 状态码 | 含义与处理 |
|---|---|
| 200 | HTTP调用取得业务结果；仍须判断draft/pending_review/approved/quoted/acquired/read等具体状态 |
| 400 / 413 / 415 / 431 | 请求格式、大小或编码不符合契约；正文未完整合法接收时不执行业务 |
| 401 | 凭据缺失、无效、过期或撤销；交接入方核对，不让模型自行改角色 |
| 403 / 404 / 405 | 不允许的来源/用途/操作、资源或工具不存在、方法不符；不绕过拒绝 |
| 408 | 正文接收超时；未执行业务 |
| 409 / 422 | 修订/幂等/报价冲突，或业务参数/兼容条件不满足；按原状态处理 |
| 429 | 频率超限；Retry-After给出等待秒数，适配器转成retry_after_seconds |
| 503 | busy表示当前请求未执行；unavailable须核原状态；recovery_required交受信运营核验，不绕过恢复门；snapshot_read_only表示误用快照目录，需正确恢复；not_ready为依赖探测未通过；payment_unavailable表示未接真实支付 |
| 500或连接中断 | 可能已写入但未送达响应；结果未知，不得换编号重做 |

响应带no-store、nosniff及服务端生成的X-Request-ID；客户端不跟随重定向、不使用环境代理、不关闭证书校验、不自动重试。HTTP响应体超过1MB或工具响应缺业务状态会拒绝。已知获取编号先用market_status查；不知道获取编号时，恢复连接后可用**完全相同参数及原request_id**重放一次取得幂等结果；再次未知保留待处理。卖方也先核seller_status，不另建商品绕开未知状态。

## 运行限制与发布边界

以下为当前可调起点，尚无生产负载依据：应用并发16、Uvicorn连接/任务上限32；正文128KB、整体接收期限10秒、请求头16KB；60秒窗口内进程总请求600、每连接来源IP120、每主体120；最多4096个限流键，空间满且无过期项时拒绝新键。凭据轮换不会绕过同主体限额；同一IP共享限额，适配容量需实测。

单进程内存限流在重启后重置，不能直接用于多进程/多实例全局配额；身份和业务记录持久保存。请求取消后已开始的数据库工作继续收尾，其并发名额保留到线程结束，调用方仍须按结果未知核对。[主子任务共享预算](shared-budget.md)已提供受信Python宿主接入，尚未成为远程付款服务；请求限流不等于货币预算控制。

非本机监听必须显式给TLS证书、私钥路径及允许Host；应用拒绝远程明文HTTP和所有浏览器Origin，不接受URL查询参数或转发头冒充HTTPS/来源IP。未测试正式证书、反向代理或公众部署；本轮未配置证书、域名、防火墙或任何生产环境。当前不支持把TLS卸载到未经验证的代理后直接使用。

默认关闭会打印请求URL的通用访问日志；应用仅累计状态码计数，异常响应不含原始异常、正文或凭据。已有本地就绪、联合快照和隔离恢复工具；持久监控、具名接警、服务守护、磁盘容量、异地灾备、真实恢复时间与凭据文件权限仍需发布前验收，不把现有限制当成完整抗攻击或可用性保证。

停止本机前台服务使用该终端Ctrl+C；Uvicorn等待在途请求收尾，最长30秒后可能终止，之后仍需核对未收到响应的原操作。不要在有未决写入时以删除账本代替停服。

## 验证与来源

在装有锁定网络依赖的环境执行：

```powershell
& $marketPython -m unittest discover -s ai_supermarket/tests -t . -v
& $marketPython -m compileall -q ai_supermarket
& $marketPython -m pip check
```

没有Uvicorn的基础环境会跳过真实网络测试，不能据此声称网络验收通过。本轮真实网络测试在D盘独立环境执行，未使用真实商户凭据、未连接外部模型或支付渠道；测试服务均停止，临时凭据不输出。

依赖版本/哈希于2026-09-14从官方PyPI核对，安装使用require-hashes且仅接受wheel。Uvicorn 0.53.0与Click 8.5.0为BSD-3-Clause，h11 0.16.0为MIT；发行包均含许可证文件，分发时保留其版权和许可声明。基础业务及适配代码为项目原创，公开分发许可仍待确定，无GPL/AGPL依赖。

依据：[ASGI HTTP规范](https://asgi.readthedocs.io/en/latest/specs/www.html)、[Uvicorn设置](https://www.uvicorn.org/settings/)、[Uvicorn固定版本配置](https://github.com/Kludex/uvicorn/blob/0.53.0/uvicorn/config.py)、[Uvicorn](https://pypi.org/project/uvicorn/0.53.0/)、[Click](https://pypi.org/project/click/8.5.0/)、[h11](https://pypi.org/project/h11/0.16.0/)。实际商业化门槛以[主计划](../../docs/ai-supermarket-master-plan.md)为准。
