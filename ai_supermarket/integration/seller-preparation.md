# 卖方离线准备助手

> v0.99新增[供应商批量清单接入](supplier-batch.md)，可离线逐项检查并显式分批提交至审核；不自动公开上架。以下单项工具保持兼容。

来源：本项目原创。用于在提交商品草稿或修订之前检查请求，减少无效提交与反复解释。它复用当前卖方工具定义和服务端商品校验，不调用模型、不联网、不读取凭据、不创建市场记录，也不代替来源审核、样例运行或买方兼容检查。

## 生成与检查请求

在项目或解压包根目录，用已经核实的Python运行环境执行：

```powershell
python -B tools/market_seller.py sample --output work/seller-draft.json
python -B tools/market_seller.py check --input work/seller-draft.json --seller vendor-example
```

输出文件的父目录须已存在；目标文件存在时拒绝覆盖。也可从其他目录用助手的绝对路径调用，输入/输出相对路径仍以调用时所在目录为准。

生成的是免费、仅限本项目内部演练的原创静态Markdown请求，不是可以直接公开销售的授权模板。将正文、标题、来源、许可、用途及使用契约替换为真实且获准的商品资料；不能复制演练的来源声明来证明其他内容。`--seller`填当前账号实际绑定的卖方编号；填写一个编号本身不会证明身份或取得权限。

结果只含检查状态、请求摘要和最多8个字段错误，不回显正文或未知字段名。路径如`$.metadata.usage_contract.sample.check`表示使用契约中的样例检查说明；`more_errors=true`表示仍有其他问题，修改后再检查。没有额外模型评分或长篇商品评价。

| 字段或状态 | 含义 |
|---|---|
| `checked`，退出码0 | 本地请求规则和提交所需使用契约通过；可以交给已获准的卖方入口继续处理 |
| `needs_changes`，退出码1 | 有需修改的问题；查看errors中的path、code和message |
| `request_valid=true`、`submission_contract_valid=false` | 请求可保存为草稿，但缺少usage_contract，尚不能提交审核 |
| `error`，退出码1 | 文件不可用、超限或不是有效JSON等，尚未完成请求检查 |
| `request_sha256` | 紧凑JSON内容摘要，用于比对本次检查的请求；不认证作者，也不冻结随后可能被编辑的文件 |
| `server_state_checked=false` | 未检查账号、在线权限、请求号冲突、当前版本状态或修订号 |
| `rights_verified=false`、`sample_executed=false` | 许可与样例仅检查声明格式；未证明权利或效果 |
| `publication_approved=false`、`payment_available=false` | 没有审核上架或支付能力 |

单个文件上限128KiB；正文与许可还分别执行24000、8000 UTF-8字节上限。UTF-8是文本编码方式，中文通常占多个字节，因此字符数量合格仍可能超过字节限制。布尔值不能充当价格，重复字段和异常编码会被拒绝。

## 提交、修订和恢复

### 从已有正文和许可文件组装

已有单份Markdown正文时，可让接入程序直接读取文件，避免AI再次复制全文并手工转义JSON：

```powershell
python -B tools/market_seller.py assemble --input work/seller-fields.json --content-file work/resource.md --license-file work/LICENSE.txt --seller vendor-example --output work/seller-request.json
```

`seller-fields.json`使用原`seller_draft`请求字段，但省略`content`和`metadata.license_text`，其余字段（包括来源、许可名称、用途、价格和使用契约）仍需由提供者或其AI明确填写。程序不会猜测权利、价格或用途。若仍包含这两个字段则拒绝，不默默替换。可用现有sample命令获得内部演练字段范例；范例不是其他资源的授权证明。

正文和完整许可按指定本机普通文件读取，UTF-8文字及换行原样保存到JSON字符串。沿用128KiB字段输入、24,000字节正文、8,000字节许可上限；超限直接失败，不截断。检查通过才独占创建输出文件，已有文件拒绝覆盖，父目录须已存在。网络共享路径、链接路径和非普通文件拒绝；这是受信本机文件工具，不是面向不可信进程的文件沙箱。

成功仍为`status=checked`，另有`output_written=true`、文件字节数、`output_sha256`以及三个输入文件的`source_sha256`；不在回执中重复正文或许可。`request_sha256`对紧凑JSON内容计算，`output_sha256`包含输出文件末尾换行，二者用途不同。`needs_changes`带`output_written=false`且不创建输出；文件写入或同步失败会报错，可能留下未完成文件，保留现场核对，不覆盖重试。

修订使用原`seller_revise`字段（含id、version、revision等，省略上述两个正文内容字段），并增加`--tool seller_revise`。版本和修订号仍由真实服务器状态决定，离线工具不认证卖方、不验证当前修订号，也不提交请求。

此命令仅组装单份Markdown，不安装整个Skill目录，不读取正文中的引用文件、图片或网址，也不执行脚本。多文件技能必须先形成适合单文档交付的授权适配版；相关依赖与局限仍需审核。来源许可、内容安全和样例效果检查仍由后续流程负责。商品价格原样保留，组装收费商品不代表支付可用。

上述assemble检查通过且`output_written=true`后，由持有合法凭据的接入程序提交其实际输出文件。下面延续组装示例的`seller-request.json`；若走前面的sample/check路径，则明确改成该路径已检查的文件。不要提交旧sample文件或把检查回执当成商品请求：

```powershell
python -B -m ai_supermarket.remote_cli --url http://127.0.0.1:8765 --credential-file work/vendor.credential.json call seller_draft --input work/seller-request.json
```

地址和凭据路径只是已有本机服务的占位示例；此命令会实际创建草稿，须用于已获准实例。凭据始终留在宿主，不交给模型。尚无账号时，请运营方提供受控邀请及该实例接入说明；助手不会注册或登录。实际工具字段以远程入口的describe输出为准。

保存草稿成功后，用响应中的id、version和revision调用seller_submit，再用seller_status查询实际审核结果。仅pending_review表示等待审核；不是已公开或已成交。

例如，提交文件包含`{"id":"vendor-example:local-evidence-note","version":"1.0.0","revision":1,"request_id":"submit-example-1"}`，查询文件仅含id和version。编号与修订必须换成真实草稿响应值；分别用`call seller_submit --input <提交文件>`和`call seller_status --input <查询文件>`传给同一远程入口。查商品样例可用关键词“事实清单”；当前检索按声明关键词匹配，不保证任意同义词或缩写都能找到。

修订请求使用id、version、revision、metadata、content、request_id，移除创建草稿用的slug。可以先运行：

```powershell
python -B tools/market_seller.py check --input work/seller-revision.json --seller vendor-example --tool seller_revise
```

本地检查不能识别服务器上过时的revision，也不能知道某个请求号是否已经使用。修订前查询状态；新的逻辑修改使用新的request_id。原提交响应未知时，按原工具错误与恢复规则核对，必要时仅重放完全相同请求及原编号；不能换编号掩盖未知状态，也不能以离线检查成功代替服务器核对。

卖方仍自主定价，参考价不强制；字段、权限和内容权利要求仍适用。声明了依赖但费用未知的商品可能通过草稿格式检查，买方实际使用时仍会被费用/兼容预检阻止。公开上架必须由独立获准的审核角色检查实际来源、许可、样例及内容；助手不会执行依赖或自动勾选审核。

## 验证范围和交付

本地验收覆盖请求错误、字节限制、自主价格、未公开草稿、修订冲突、无网络/数据库副作用，以及真实本机HTTP的提交、退回、修订和买方读取。另在独立解压包、无关工作目录检查助手运行。真实外部供给者能否独立完成仍需观察，不能用合成测试替代。

助手与本说明纳入当前固定97文件的内部源码候选包；v0.37加入[本地试用准备入口](local-trial.md)，v0.40增加[任务资源准备](resource-preparation.md)，v0.41增加[本地资源交付](resource-delivery.md)，v0.43增加[本地PNG/WAV接入](asset-market.md)。本页助手仍检查文本商品，素材使用独立本地导入入口。旧63/65/67/70/72文件包保留原清单，须用对应版本的可信工具检查。包不含真实凭据、市场库或上架授权，见[源码包说明](../../docs/ai-supermarket-package-guide.md)。
