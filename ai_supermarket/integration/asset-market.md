# 本地图片与音频素材市场

来源：本项目原创。2026-10-01新增可选的已认证卖方网络提交入口，见下节；沿用受限PNG/WAV格式和原草稿/审核/权益逻辑，尚未公开部署或验证外部供应商。原v0.43仅本地导入的说明保留用于历史理解。没有启用真实支付或自动审核。

## 供应商通过网络提交素材

受信运营者在已有schema 2素材库上显式给HTTP启动器增加`--asset-uploads`。默认关闭；schema 1、缺失或不可用的库不能借此自动创建/升级。开启只允许已有seller凭据创建本人素材草稿，不向买方/审核者开放写入权限。已有TLS、Host、请求/并发限流继续适用；本轮只在独立本机实例启用，没有改动活动服务。

供应商宿主复用原候选JSON及同目录素材、许可文件，执行：

```powershell
python -B tools/market_assets.py upload --url https://YOUR-MARKET-HOST --credential-file C:/private/seller.json --seller YOUR-SELLER --input C:/approved/candidate.json --title "素材名称" --summary "适用范围" --keyword "任务关键词" --price-cents 125 --request-id asset-upload-001
```

以上均为占位；须对发送目标和素材已有授权。金额为供应商自定的人民币分，0为免费；不会调用支付。工具先在本地核对文件、摘要、元数据和许可格式，再以相同凭据读取`/v1/tools`的seller身份及asset_upload能力声明；不匹配就不上传文件。二进制由宿主传送，不作为模型参数或base64正文。接口返回`draft`仅代表草稿已保存，沿原seller_submit提交审核，真实审核后才可被买方找到。格式检查不能证明版权。

请求地址固定`POST /v1/seller/assets`，Content-Type为`application/vnd.ai-supermarket.asset-v1`。封装依次为10字节前缀`AISASSET1\n`（末尾为一个换行字节）、4字节大端无符号JSON长度、UTF-8 JSON、原素材字节；JSON恰含seller、slug、version、metadata、content、request_id。文件最多2MiB、JSON最多128KiB、整包最多2228238字节；HTTP请求头仍限16KiB，禁止压缩正文。服务端先验身份/角色再收包，写入前重新认证；字节哈希、实际格式、声明大小和卖方身份必须一致。原素材库64MiB上限保持。

请求编号沿用原draft_asset幂等规则：相同编号和内容返回原草稿，变化内容不能覆盖同版本；网络未知时保留文件/编号，通过seller_status查询或显式重发完全相同请求，不生成新编号试图绕过。工具没有自动重试、自动提交审核、自动批准、改价或删除动作；路径与文件名不被服务器执行。旧seller_draft文本工具和JSON正文大小限制不变。

## 创建或复制升级

普通`Exchange`新建库仍是开放市场schema 1。新程序可读取schema 1/2，打开旧库不会自动迁移；素材必须使用显式创建的schema 2库。

```powershell
python -B tools/market_assets.py create --output C:/path/new-asset-market
python -B tools/market_assets.py upgrade-copy --source C:/path/old/market.sqlite --output C:/path/upgraded-copy
```

两个命令择一执行；路径是占位值，目标必须不存在且父目录已存在。升级复制只支持运行可用的schema 1开放市场，拒绝快照、恢复目录、未知结构、待恢复库；不修改源库或切换客户端配置。开始前停止源库写入，保持停止直至自行决定后续是否切换；报告只证明该次快照的数据，不能收集复制完成后源库的新写入。

复制前核验结构、完整性和商品/权益关联；复制后逐表核历史记录、素材schema、数据库完整性。新目录保留`restore-started.json`，未放行的部分副本无法由市场打开；成功后`recovery_state=released`。失败保留现场和原库，换新目录处理，不覆盖或降级已有数据库。旧程序因表结构不同会拒绝新库。需要回退时继续使用原v0.41程序与原库，不用旧程序打开新库。

新表`asset_objects`保存按SHA-256去重的原始字节，`asset_links`绑定商品/版本；和草稿在同一事务写入。每对象2MiB，当前对象总量上限64MiB；这是受限试点参数，不是生产容量承诺。修改素材或许可需要新版本，当前不提供原版本覆盖和清理操作。

## 从候选进入草稿

准备`market_asset_prepare.py`所需的候选JSON、同目录素材和许可原文，再执行：

```powershell
python -B tools/market_assets.py import --database C:/path/new-asset-market/market.sqlite --seller vendor --input C:/path/candidate.json --title "素材名称" --summary "适用任务和限制" --keyword "任务关键词" --request-id import-001
```

候选id须为`vendor:商品slug`；导入严格验证格式、摘要、原始字节和许可字段，保存来源、许可原文与细分权利声明。商品许可正文当前市场上限8KiB，离线候选允许16KiB不表示一定可导入。首轮导入价格0，卖方后续仍可自主改价；未接支付时新买方不能取得收费资源。

导入只生成草稿，不自动提交或审核。沿用原`seller_submit`、`review_inspect`、`review_decide`流程；实际审核者必须核对来源、许可、素材、样例和使用契约，不得用卖方声明代替独立依据。可信本地审核宿主可以调用`Exchange.inspect_asset(id, version)`取得真实字节；其他卖方不能读。该方法不暴露为模型工具，也不会返回base64。需要图像显示/音频播放的审核宿主仍应自行组织，测试身份不代表真实运营签核。

## AI发现与取用

开放市场`market_search`增加image/audio种类，`market_resolve`返回`format=asset`、`resource_protocol_version=2`、尺寸或时长、字节数、摘要及细分权利。原skill/template和文本参数保留；MCP市场服务版本标识更新为0.4.0。公开卖方文本draft/revise仍仅接受Markdown，二进制不进入模型参数；素材由本地导入或上述已授权宿主网络提交。

素材契约`version=2, delivery=static_asset`需要宿主能力`receive_asset`，表示宿主能安全保存原文件，不代表模型已理解图像/音频。默认宿主仍仅有read_markdown；本地交付时明确通过`--host-policy`提供如下可信配置：

```json
{"capabilities":["read_markdown","receive_asset"],"permissions":[],"dependencies":[],"extra_budget_cents":0}
```

AI按原多资源需求流程选择，无需用户挑商品。素材同样经过准备预检、用途/兼容/价格检查、获取权益；`market_read`对素材只返回`host_transfer_required`及摘要，不记录实际读取、不携带二进制。可信宿主使用`Exchange.read_asset`或`market_deliver.py`完成真实字节读取。前者含Python bytes，绝不能直接发送给模型或JSON序列化；后者只返回任务文件夹与清单。

```powershell
python -B tools/market_prepare.py check --database C:/path/market.sqlite --principal buyer --input C:/path/selection.json --host-policy C:/path/policy.json
python -B tools/market_deliver.py --database C:/path/market.sqlite --principal buyer --input C:/path/selection.json --output C:/path/new-task-bundle --host-policy C:/path/policy.json
```

含素材的交付清单为`local-resource-bundle-v2`；纯Markdown仍v1。`content_hash`绑定素材版本的完整元数据、许可与说明，`asset.sha256`绑定原文件字节，不能混用。文件用固定`.png`/`.wav`名称，保存原始字节不转码、不提取指令。原免费取用、已购复用、用途隔离、幂等、失败证据及不覆盖规则保持。暂停/隔离商品阻止已购读取，停售商品保留已有权益；新选品预检仍只查在售。

使用前调用`market_bundle_check.py`，带原回执`--expected-sha256`、账号、任务、用途和相同宿主策略。它同时核对本地文件、版本许可、素材字节、原获取/使用记录与当前隔离状态；不会新增读取/评价。任何结果只代表该时刻可读取，不是长期有效授权、任务质量或独立权利证明。

## 当前范围

PNG支持8位RGB/RGBA及v0.47新增的8位索引色，均非交错、最多400万像素。v0.43接收IHDR/IDAT/IEND最小子集；v0.45额外接收至多16个、数据合计至多8KiB的普通tEXt块，校验关键字、分隔符、CRC和位置，保持IDAT连续。不解读其指令或许可、不返回文本到模型、不剥离或改写原文件。纯最小PNG及带tEXt的既有profile保持原值。

v0.51在RGB/RGBA8非交错范围内接收一个sRGB色彩标记，必须在IHDR之后、IDAT之前，长度1且渲染意图0～3；重复、错误位置、长度、值或CRC均拒绝。对应profile为png-rgb-rgba8-noninterlaced-srgb，另有普通文本时为png-rgb-rgba8-noninterlaced-srgb-text；旧商品profile不变。标记与原字节保留，由实际图像渲染器处理显示，不执行色彩转换。此扩展未加入iCCP、真彩gAMA或索引色sRGB组合支持，其他未支持块仍拒绝；不是完整PNG解码器。

v0.47索引色profile为png-indexed8-noninterlaced，支持必需PLTE（1～256项）、可选tRNS透明度及gAMA，检查长度、顺序和重复；逆向还原五种滤波后检查每个像素索引是否在调色板内。channels表示展开后的RGB/RGBA通道数。压缩文本、其他辅助块、1/2/4位索引色及交错图仍拒绝；旧解析器不支持新素材，跨版本使用应先检查。WAV仍仅最小RIFF PCM16、单/双声道、8000～48000Hz。OGG不直接入库，若来源许可允许且在宿主预先转成WAV，须另存原件和转换记录，并明确派生版本；交付环节始终保留已登记的字节。依据为[W3C PNG规范](https://www.w3.org/TR/png-3/)和[Microsoft RIFF说明](https://learn.microsoft.com/en-us/windows/win32/xaudio2/resource-interchange-file-format--riff-)；解析代码原创，仅标准库，无新增运行依赖或表结构。

已有联合快照/恢复校验识别schema 2，并核素材对象与版本绑定；仍须执行既有运营放行，不把单库升级替代多库恢复。当前没有实际素材卖家、专业权利核验或真实任务效果证据。开发/测试批准不包含改现有实例、上线、外发、交易、训练或新模型进程，免费邀请试点方向保持，现行费率见[佣金规则](../../docs/ai-supermarket-commission-policy.md)，历史订单沿用原费率。
