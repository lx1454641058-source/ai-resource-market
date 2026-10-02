# 受控自助入驻

来源：本项目原创。买方或卖方收到运营签发的邀请后，在本机准备凭据、自助领取账号，并使用原有市场工具。邀请不是商品，也不收摊位费；商品仍由卖方自主定价，提交后需通过准入审核，成交佣金按原报价及已核验协议确定，真实收费尚未开放。

当前交付为有限邀请的机器入驻入口，支持本机网络演练和已有HTTPS服务配置。尚非无限制公众注册，也没有完成商户主体核验、跨设备邀请交付、账号归属争议或生产安全验收。受控试点不改变长期开放供给方向。

## 责任与数据

运营程序签发绑定buyer或seller的邀请，不能签发reviewer邀请；审核者仍单独由有权运营管理。领取请求只包含邀请与本机生成的凭据，不能提交角色、主体、权限、额度或凭据期限。新账号默认只声明read_markdown能力，没有额外权限、依赖或费用预算。

客户端先生成随机凭据，写入**不存在的新文件**并同步到磁盘，再发入驻请求。服务器只存摘要，响应只含账号编号、角色、凭据编号和到期时间；秘密不放在命令参数、URL、日志或模型上下文中。邀请是可被持有者领取的秘密，持有邀请不构成真实姓名或商户身份核验；不要公开分享。Windows文件访问权限继承所在目录，本工具不自动更改权限，目录保护和跨设备交付须另行验收。

新增独立enrollment账本保存邀请摘要、限定角色、到期时间、领取关联与审计；身份账号及凭据仍在原身份账本。本次没有改变原身份或业务表结构，不自动迁移旧库。

## 本地演练步骤

前置：已按[HTTP服务说明](http-service.md)选择装有锁定网络依赖的Python 3.12环境。每个终端先设置`$marketPython`为该解释器的绝对路径。所有命令从项目根目录执行；使用一个未占用的演练目录，不填真实商户凭据。

运营终端初始化并准备一个卖方邀请：

```powershell
New-Item -ItemType Directory -Path work/enrollment-local-example
& $marketPython -m ai_supermarket.identity_admin --identities work/enrollment-local-example/identity.sqlite init
& $marketPython -m ai_supermarket.exchange_cli --database work/enrollment-local-example/market.sqlite --principal local-setup --role buyer diagnose
& $marketPython -m ai_supermarket.enrollment_admin --identities work/enrollment-local-example/identity.sqlite --enrollments work/enrollment-local-example/enrollment.sqlite init
& $marketPython -m ai_supermarket.enrollment_admin --identities work/enrollment-local-example/identity.sqlite --enrollments work/enrollment-local-example/enrollment.sqlite issue --role seller --output work/enrollment-local-example/seller.invitation.json
& $marketPython -m ai_supermarket.http_server --database work/enrollment-local-example/market.sqlite --identities work/enrollment-local-example/identity.sqlite --enrollments work/enrollment-local-example/enrollment.sqlite --port 8765
```

`diagnose`在受信本机建立空业务账本，不创建市场商品或正式账号。开启入驻前业务账本也必须就绪。服务仅本机监听；省略`--enrollments`不开放入驻路径。启动命令占用当前终端，结束用Ctrl+C等待请求收尾。

另一个终端模拟收到邀请的卖方。演练中直接访问上述邀请文件；实际跨设备交付需由有权运营通过已批准的途径完成，本工具不发送消息：

```powershell
& $marketPython -m ai_supermarket.enrollment_client prepare --credential-file work/enrollment-local-example/seller.credential.json
& $marketPython -m ai_supermarket.enrollment_client enroll --url http://127.0.0.1:8765 --invitation-file work/enrollment-local-example/seller.invitation.json --credential-file work/enrollment-local-example/seller.credential.json
& $marketPython -m ai_supermarket.remote_cli --url http://127.0.0.1:8765 --credential-file work/enrollment-local-example/seller.credential.json describe
```

成功返回`enrolled`后，卖方继续按[机器接入包](README.md)调用seller_draft、seller_submit等工具。买方使用运营签发的buyer邀请，以不同的新凭据文件执行同一流程。审核角色不能通过此入口获得，批准商品需真实准入证据，演练通过不等于生产审核。

## 入驻契约与恢复

启用后的`POST /v1/enroll`接受application/json，最大4KB，禁止额外字段。它不要求已有Bearer账号凭据，而是核验正文中的邀请及新凭据；参数名为`invitation`和`credential`。数据由可信客户端程序传输，不能让模型代填或读取秘密。已有Host、HTTPS、Origin、查询参数、接收超时和并发限制继续生效。

返回成功对象为`status=enrolled`、`principal`、`role`、`key_id`和`expires_at`；同邀请、同凭据重放返回同一账号及原到期时间，不产生新钥匙。邀请默认24小时、可选60秒至7天；新凭据默认30天、可选60秒至90天，均由运营签发时固定。这些是可调试点起点，非商业服务承诺。

| 结果 | 调用方处理 |
|---|---|
| enrolled | 保存原凭据，查询角色工具后使用；准备凭据文件本身不表示账号已建立 |
| enrollment_denied / 403 | 邀请无效、被撤销或未领取即过期；不更换角色或猜测邀请 |
| enrollment_conflict / 409 | 此邀请已绑定别的凭据；保留原文件交运营核对，不能重开同一账号 |
| enrollment_inactive / 403 | 原凭据已撤销、账号禁用或凭据过期；重放不恢复权限，不延长有效期 |
| not_ready / 503 | 身份、业务或入驻配置未就绪；不绕过恢复门；由运营核配置 |
| enrollment_recovery_required | 关联身份路径或恢复批次变化；旧入驻账本不能直接沿用 |
| rate_limited / 429 | 保留原邀请与凭据，按retry_after_seconds等待；不自动循环 |
| unavailable、500、连接中断或响应不可核验 | 账号可能已经创建；先用原凭据查询角色工具，必要时同邀请、同凭据重放一次；仍未知则交运营核验 |

客户端没有自动重试。响应丢失后不要再次运行prepare来替换凭据，原文件存在也会拒绝覆盖。邀请过期后仅允许确认已创建且仍有效的原账号；不能新开户。邀请被撤销则拒绝领取和领取重放，但不会自动禁用已经创建的账号，后者由运营使用原有identity_admin的revoke-key或disable命令单独处理。凭据丢失后的身份核验和恢复仍需运营流程，当前没有公众“找回密码”接口。

身份账号、凭据和入驻来源标记在同一身份库事务中写入。若身份写入已成功、邀请库确认写入失败，下次用原凭据可补齐记录；固定账号编号由邀请编号派生，不同凭据不能趁第二账本尚未确认再次开户。跨库记录可能短暂不一致，因此不宣称两个独立数据库具备同时提交保证；身份库记录是防重复授予的依据。

服务增加60秒窗口的入驻总请求30、每来源IP5的默认限制，叠加原全局/来源/并发限制。它们是进程内计数，重启会重置，共享IP会相互影响，不等于完整防滥用或跨实例配额。请求接收完毕后再次核邀请撤销、到期及恢复批次，实际身份写入时仍检查到期和恢复批次。

## 备份、恢复与停用

入驻账本绑定身份库的路径与恢复批次。现有[联合恢复](recovery.md)支持三业务库及可选第四计量库，不自动备份或迁移邀请库；这是保守失效策略，不能宣称邀请无损恢复。身份恢复到新目录或恢复批次改变后，旧入驻配置拒绝启用；市场尚待恢复放行时不允许新开户。运营完成恢复核验后，使用新入驻账本和新邀请重新开放，旧邀请默认失效。已有账号用原身份恢复流程核验后另行签发新凭据，不用新邀请冒充找回旧账号。

只回滚邀请库时，身份库中已创建账号的固定绑定仍阻止重复授予；如果身份库也恢复，必须走带新恢复批次的正式恢复流程。不得用手工替换身份文件并删除恢复标记来绕过此约束。入驻审计的留存、归档、删除与业务关联还需生产策略验收。

停用可选入驻配置后，原已有凭据及商品链路继续按原规则处理；禁用新入驻不自动撤销既有账号。配置变更及生产切换须按正式发布流程进行，本轮未执行。

## 验证范围

专项为`python -m unittest ai_supermarket.tests.test_enrollment ai_supermarket.tests.test_http_app ai_supermarket.tests.test_http_live -v`。覆盖角色绑定、过期/撤销、并发重放、两个账本之间失败、恢复批次、文件不覆盖、响应不泄漏、网络边界，以及真实本机网络中新卖方提交→独立角色审核→新买方获取资料。真实恢复演练验证旧邀请和旧凭据不能复活，新入驻在业务放行前被阻止。

工程测试使用原创临时数据和软件调用，不是独立商家试点或真实AI效果评估。新代码仅用标准库，沿用已有网络依赖，无新增第三方素材、模型或GPL/AGPL依赖。公开分发许可、主体核验、邀请交付、生产访问权限及实际运营仍按[主计划](../../docs/ai-supermarket-master-plan.md)验收。
