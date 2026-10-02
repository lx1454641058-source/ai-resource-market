# 分源反馈与账本复制升级

来源：本项目原创。适用新建或已复制升级的v2本地超市账本。当前不是公网认证或独立质量审核服务。

## 行为与边界

同一次使用可以同时记录：AI认为“没有帮助”、格式检查通过、来源检查失败。它们检查的对象不同，不互相覆盖，也不自动推导为商品整体合格。

模型仍通过原有`market_feedback`提交极简判断，参数和accepted/duplicate返回形式不变；程序检查走受信Python宿主。没有新增日常模型调用，也不要求模型读取本页。买/卖/审核工具数量不变。

账本按使用编号、证据来源、检查器、修订号分别去重。每次更正保留旧记录，`supersedes`指向上一条；同一修订重放不会回退当前结果。`duplicate`说明这条报告以前已保存，不证明它仍是最新判断。

`resolve.quality.observations`增加可空的`validator`字段，分别展示各检查器最新结果的使用次数。每个检查器计数不能相加当独立任务数；`independently_verified`继续为false。报告只针对当前主体，不是全市场评分，也不自动改排名。

## 宿主使用示例

`usage_id`必须来自该主体真实的`read`返回，下方`market`为已有Market或Exchange对象。两类报告无先后要求：

```python
# 来源：本项目原创接口示例。格式检查与使用价值是不同判断。
market.record_check(usage_id, 'format-v1', True)
market.feedback(usage_id, useful='no', check='not_run')
history = market.feedback_history(usage_id)

# 只有重新检查后才更正；预期修订号取自history，不由模型猜测。
market.correct_check(usage_id, 'format-v1', False, 1, 'check_repeated')
market.correct_feedback(usage_id, 1, 'report_corrected', useful='unknown')
```

更正原因仅允许`check_repeated`（复检）、`report_corrected`（报告更正）、`appeal_resolved`（已有申诉裁定）。字段本身不是申诉裁定证据；实际运营仍需相应记录及责任人。

`feedback_history`、`correct_feedback`、`correct_check`仅供受信宿主调用，不是模型可自行发现的工具；主体只能查看或更正自己的使用记录。正式远程接入需绑定真实身份和检查器权限，不能将本地调用者自报的validator视为认证机构。

## v1升级流程

新代码遇到v1账本返回`upgrade_required`，不会静默改表。旧账本文件不变，但必须复制升级后才能用新代码继续调用。目录账本和开放市场账本仍严格区分。

1. 停止所有向源账本写入的调用者；保持停止直至切换完成。包括后台宿主和所有角色入口。
2. 指定一个尚不存在的新目标文件。示例路径应替换为实际选定文件：

```powershell
py -3.12 -m ai_supermarket.migrate --source work/ai-supermarket/exchange.sqlite --output work/ai-supermarket/exchange-v2.sqlite
```

3. 必须得到`status=copied`、`verified=true`。工具只读源账本，校验结构、归属、关联、逐表内容以及反馈转换结果；发现复制期间新增写入则返回`source_changed`并回滚本次复制的数据。失败时不要使用该目标，应排除问题后复制到另一个新文件。
4. 在写入仍停止的前提下，核查新账本的已购内容和各角色记录，再把调用者的`--database`路径明确切换到目标。工具不会自行修改客户端、默认路径或配置。

```powershell
py -3.12 -m ai_supermarket.exchange_cli --database work/ai-supermarket/exchange-v2.sqlite --principal buyer-example diagnose
```

5. 验证后恢复调用。旧文件保留；切换后的新写入不会同步回旧账本。发生问题先停止写入、核对两边差异，不得直接切回旧文件而丢掉新记录。

升级工具是当前小型本地账本的离线复制方案，使用内存快照；未做大规模账本容量认证，也不是在线迁移或备份轮换系统。返回成功之后若源账本再被写入，工具无法追溯阻止；暂停写入和切换窗口仍是操作者必须落实的条件。

现有历史验收账本未在本轮批量升级；新数据库及测试数据在临时目录生成。正式生产备份、灾难恢复、身份系统、外部模型检查器和独立审核仍有各自验收门槛。
