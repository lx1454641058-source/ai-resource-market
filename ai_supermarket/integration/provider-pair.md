# 分目录启动市场与独立供应商

v0.112，2026-09-23，本项目原创。该接入演练把市场和供应商放在不同目录、不同进程。供应商只读取自己的配置、卖方凭据和操作库，经HTTP核对原操作授权，不读取市场的商品、身份、预算或次数数据库。使用固定原创免费库存API，不接入模型、真实钱包或外部供应商。

## 准备与启动

在已有本项目HTTP依赖的Python环境中执行，不另行安装依赖：

```powershell
python -B tools/market_provider_pair.py prepare --directory work/my-provider-pair --market-port 8770 --provider-port 8771
python -B tools/market_provider_pair.py check-market --directory work/my-provider-pair/market
python -B tools/market_provider_pair.py check-provider --directory work/my-provider-pair/provider
```

prepare要求全新目录，生成market与provider两个子目录及根START.md；已有目录拒绝覆盖。两个check只核本地配置。供应商目录只有provider.json、binding.json、provider-key.json、seller-credential.json和operations.sqlite，不包含市场数据库。配置与原连接密钥的文件摘要固定，端口、原商品发行或身份不符时停止。缺失/空操作库不会初始化新库。

v0.116同时生成根目录steward-connection.json，供[包内个人总管](steward-trial.md)直接导入。文件只含路径及原任务范围，复用原身份、预算和downloads目录，不创建第二个远程任务；导入检查不联网、不运行模型。总管试用与下方exercise脚本是两种接入方法，本次请选择一种使用全新演练目录，不用不同宿主轮流重置任务次数。

先在一个终端启动市场：

```powershell
python -B tools/market_provider_pair.py serve-market --directory work/my-provider-pair/market
```

在另一个终端核对卖方身份和在线授权接口，再启动供应商：

```powershell
python -B tools/market_provider_pair.py probe-provider --directory work/my-provider-pair/provider
python -B tools/market_provider_pair.py serve-provider --directory work/my-provider-pair/provider
```

供应商可以先启动，但具体调用必须等市场可核验。probe-provider只确认卖方身份和接口存在，operation_authorized=false，不声称任何具体调用已经获准。身份撤销或连接失败会返回错误，不使用本地配置冒称网络成功。两个服务只监听127.0.0.1，使用明确且不同的端口；不会关闭其他进程。Ctrl+C分别停止，不删除原记录。

## 生成成果与收货

沿用原买方工具，目录指定market子目录：

```powershell
python -B tools/market_api_try.py exercise --directory work/my-provider-pair/market --acknowledge
python -B tools/market_api_try.py receipt-status --directory work/my-provider-pair/market
```

生成market/采购建议.md，保存API结果、许可证、清单和逐次收货回执。原结果库存7件，演练目标10件，建议补货3件；真实单价/时效未知，没有发送采购订单。脚本不是实际用户任务或模型自主选品效果证明。授权失败或结果未知时不换请求编号；确需查原供应商记录时显式使用exercise --recover，可同时加--acknowledge。

重启供应商仍使用provider/operations.sqlite，重建ProviderOperations不重新执行已完成操作。市场次数与供应商业务操作各自记录，正常演练只占用一次API额度。市场子目录为兼容旧准备工具保留一个空operations.sqlite，本双端流程不使用它；市场检查、启动和买方成果流程都不依赖该副本。不要删除它来“重置”状态，也不要混用market/START.md中的旧共享库供应商启动命令。完整新命令以本次根START.md为准。

## 验证范围与真实接入差距

测试启动了真实市场/供应商CLI进程，并在供应商进程安装测试专用访问检查，拒绝打开market目录中的文件和SQLite连接；在此条件下仍完成结果/许可/成果/回执及供应商重启恢复。这证明本实现的调用路径不依赖市场目录，不表示两个普通进程被操作系统沙箱隔离。生产文件权限和跨机器TLS由部署方另外配置。

原始凭据仅保存在私有演练目录，不能上传整个目录或把内容交给模型。源码候选包只包含工具代码和说明，不包含准备目录。该工具只认可内置原创商品和本机连接，不接受任意第三方内容自动批准，也不通过修改价格变成收费生产服务。

真实资源站可复用[远程授权回调](provider-authorization.md)、[持久操作记录](provider-operations.md)和[原站商品收货核对](supplier-receipts.md)，但须提供自己的合法商品/许可、固定域名、卖方身份、发行白名单、业务执行及原操作查询。线上核验无法取消已发生的业务副作用，回调须留并发容量；真实支付、整包履约、退款分账和商业联调仍未完成。
