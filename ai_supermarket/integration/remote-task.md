# 远程 AI 任务入口

> v0.104将购买与API调用接入同一`purchase`任务：首次发现匹配的API宿主后增加调用/查询/恢复/收取工具，按原账单核验取得权益后继续调用，无需切换模式。只有采购宿主时仍保留原6个工具；旧任务发现结果不自动增加工具。独立`api`模式仍只取免费或已有权益，`free_text`保持不变；详见[API远程接入](api-remote.md)。验证使用合成付款证据，尚无真实钱包。

> v0.98增加显式`free_text`模式与本机免费试用包，无须商户或采购宿主。默认`purchase`模式保持不变，详见下方“免费接入”。

2026-09-23，v0.97。本项目原创。用于接入者已经配置身份、任务预算及采购 HTTP 宿主的场景。该入口在启动时查一次超市，把少量候选交给 AI，由 AI 核对和选择；取用后继续制作用户成果。没有启用现有 Codex 会话、通用 AI 平台或真实钱包的全局设置。

## 接入条件与启动

服务端须已有 `TaskBudgetGateway` 的凭据到任务绑定，以及同一凭据对应的 `PurchasingTaskResources`。接入者准备固定 `RemoteMarket` 端点、凭据文件、UTF-8 目标文件和已有本地交付目录。目标最多8000字且文件不超过24000字节；支持换行。任务和用途必须与 `/v1/tools` 描述一致。公网使用 HTTPS，仅显式本机数字地址允许 HTTP。

```text
python /absolute/path/ai_supermarket/remote_cli.py --url https://YOUR-MARKET-HOST --credential-file /absolute/path/credential.json task-mcp --task-id YOUR-BOUND-TASK --purpose personal_use --goal-file /absolute/path/goal.txt --output-root /absolute/path/downloads
```

上面的地址、任务和路径是占位符，需由接入者填写自己的已有配置。`task-mcp` 使用标准输入输出传送 MCP 消息（AI 宿主调用工具的协议），不是浏览器页面；需要宿主为每项任务传入实际目标和已绑定任务。不要把长期固定目标配置成所有工作的通用入口。原 `mcp`、`stdio`、`config` 命令保持原行为；`config` 不会自动生成任务入口配置。

启动先核对服务身份和任务，再发送一次规范化目标前512字的查询，最多返回3项摘要；超过512字会在初始化说明中标记截断。AI 可以根据需求提炼关键词补查。初次查询的完成或失败结果保存在本机，同任务重启复用；仅有请求意图却没有结果时保留未知状态，不偷偷再查。结果缓存不能授权取用，后续网络调用仍由原服务重新认证。

## 模型能调用的工具

| 工具 | 输入 | 返回与后续 |
|---|---|---|
| `market_search` | `q`，可选`kind` | 补查最多3项摘要；没有新信息不要循环查 |
| `market_resolve` | `id`、`version` | 条件兼容后保存原发行摘要 |
| `market_use` | `id`、`version`、`request_id` | 读取文本、保存文件或提示宿主处理原付款需求 |
| `market_purchase_status` | `request_id` | 查询已开始的收费采购状态；免费取用无需此步骤 |
| `market_receipt` | `request_id` | 重验原下载文件后记录收货声明；仅适用于文件 |
| `market_feedback` | `usage_id`及已有评价字段 | 仅报告本任务实际使用事件，不另开模型写长评 |

端点、身份、用途、任务、输出目录和付款凭证不在模型工具参数里。AI 先根据目标选择，再 `resolve → use`，文件可 `receipt`；文本直接继续成果制作。收货声明不等于独立质量验证或任务完成。

同一原请求不能改选商品；同一发行版本在任务中变化时拒绝更新原摘要。新客户端对文本也要求服务端返回匹配发行摘要，旧服务没有该字段会拒绝交付；新版服务仍兼容未发送可选摘要的旧请求。升级配套服务后使用本入口，不能删摘要检查绕过错误。

## 可信宿主处理钱包

程序宿主可直接使用以下接口，结合自身模型工具循环；不用另开模型完成查货：

```python
from ai_supermarket.remote_task import RemoteTaskResources, RemoteTaskInterface

task = RemoteTaskResources(remote, task_id, goal, purpose, existing_output_root)
model_tools = RemoteTaskInterface(task)
# 把 model_tools.instructions 和 tool_list() 交给现有模型，按模型选择调用 call()。
# 模型收到 payment_needed 后，由可信宿主读取 task.wallet_handoffs[request_id]。
# 重启后可显式 task.recover_wallet_handoff(request_id) 恢复原账单。
# 只有已获准的钱包完成原付款、返回凭证后，才调用：
# task.resume_payment(request_id, payment_proof_header)
```

`wallet_handoffs` 只在内存保存带账单的 `PurchaseResponse`，工具仅返回其摘要；后续恢复出现待核查、已付或失败时清除旧账单。上述 SDK 不执行钱包付款，不签发授权。命令行 MCP 入口本身没有钱包执行器，当前主要用于免费资源交付和采购需求发现；收费流程需程序宿主接入获准的钱包能力。凭证不落盘，不把恢复账单当作再次付款指令。

支付宝买方支付现采用[官方 Agent 支付交接](alipay-agent.md)。首次资源402的内存`request_snapshot`可由可信宿主交给`prepare_alipay_agent`准备原账单与命令参数；不会自动执行。已有明确授权及固定版本兼容证据的宿主可显式调用`execute_agent_payment`，不在模型工具内。`recover_wallet_handoff`返回的恢复账单不具备该快照，不能直接交官方CLI付款，也不能改换请求地址补造快照。官方CLI会付款并重放原请求；`receive_after_agent_payment`独立按原订单状态接回并核验资源，不以CLI文字判定成功。源码已实现，实际官方支付和待确认查询恢复仍待联调，默认发现状态仍为不可执行钱包。

## 成本、恢复与验收

默认每任务最多24次超市网络尝试，包括入口核对、首次查询、详情、取用、恢复、声明和反馈；失败也占一次，重启不重置。SDK 可由宿主在首次创建时明确设2至100次，模型不能改。它不是模型 token 上限或购买预算，购买金额仍受服务器原授权和共享预算约束。并发启动只允许首次查询的一个持久化意图发出；中断后保留未知记录。不要删记录来重置上限或重购。

本机状态记录目标摘要、原选品、请求、使用事件和调用计数。下载器另存原资源、完整许可及收货记录，要求文件系统支持硬链接原子创建；现有文件冲突时保留，不能覆盖。资源正文按外部不可信资料处理。断线、过期身份、无匹配或未知付款都不能解释成任务已完成。

工程测试使用真实本机市场和供应商 HTTP，跑通免费 WAV 经 MCP 取用/声明，以及收费 CSV 的原账单恢复、合成支付证据核验、接收、声明和观察报告检查。观察报告由固定测试步骤制作，不冒称真实用户任务、独立 AI 试点或质量收益证据。真实授权供应商、钱包支付及实测 token/成本收益仍待验收。恢复旧入口可停用 `task-mcp`；保留原任务目录、账单、权益与费用记录。

文件接收协议及证据含义详见[采购接收与恢复](purchase-download.md)。

## 免费接入（v0.98）

接入者只有普通市场HTTP时，可显式使用`task-mcp --mode free_text`，或SDK传入`mode='free_text'`。服务仍必须强制任务预算且凭据已绑定当前任务；不能用未绑定的公共身份代替。不自动从采购失败降级到此模式。

免费模式只暴露search、resolve、use、feedback四个工具。详情须为零元静态Markdown、无外部依赖和执行权限；付费、素材和原站文件交给相应采购宿主。本轮不替代付费资源主流程。AI不挑端点或凭据，用户不挑商品。

取用将原发行摘要传入acquire，再按同任务读取。服务端复核原权益与身份，客户端比对正文SHA256后保存正文、许可原文和本地收货记录，返回文本及路径。一次use包含两次网络调用，分别计入原24次尝试上限；同请求恢复复用原权益和使用事件，不绕过认证。目录采用输出根目录下单层摘要，避免普通Windows项目路径因双层摘要超长。根目录本身仍须是操作系统支持的有效路径。

本地文本记录不向服务器发送ZIP收货声明，不是渠道确认或任务完成。目标、用途、任务或模式改变时拒绝复用原状态；不能删除记录重置预算。免费模式也不会调用模型、支付或执行资源中的代码。

### 准备一个可运行的本机试用包

```powershell
python -B tools/market_remote_try.py prepare --directory work/my-remote-trial --port 8769
python -B tools/market_remote_try.py check --directory work/my-remote-trial
python -B tools/market_remote_try.py serve --directory work/my-remote-trial
```

prepare仅创建全新目录，不覆盖旧成果。生成独立市场、一份固定原创免费模板、买方凭据、已绑定任务、零元预算、目标/任务文件、MCP配置及`START-REMOTE.md`。样例准入只适用于该固定原创模板，没有自动审批任意供应商资料。没有商户配置，也没有伪造付款。

check只检查本地账本、原凭据/任务绑定与零预算，不代表网络连通或任务完成。serve要求现有HTTP依赖，只绑定127.0.0.1，不改现有服务；端口占用时停止核对。配置文件交可信宿主审阅，不上传凭据；没有自动安装到当前客户端。按生成的任务让AI自行选资源并完成handoff.md，再检查事实来源和未知项。

此包用于本项目内自选演练。数据、源码和许可证未因此获得公开分发或销售许可；实际接入方应使用其获准的原服务、身份与资源。移动目录后原配置拒绝启动，应在新位置按接入流程准备并保留旧记录。停止服务可按Ctrl+C；未知状态、权益和本地成果保留。
