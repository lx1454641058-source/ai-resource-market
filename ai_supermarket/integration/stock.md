# 项目内免费货架

v0.46按用户“缺少货物，与游戏无关”的纠正扩充。已有20份产品工作资料，加12份跨领域技能/模板，共32份原创Markdown。新的六类为表格处理、程序排错、网站内容、视频前期、陌生领域入门、客服交接，每类一份方法、一份可填模板。

| 任务 | 方法商品 | 模板商品 |
|---|---|---|
| 表格处理 | csv-cleaning | csv-field-dictionary |
| 程序排错 | bug-triage | bug-reproduction |
| 网站内容 | web-content-structure | page-content-spec |
| 视频前期 | video-preproduction | storyboard-sheet |
| 陌生领域入门 | domain-onboarding | concept-map-card |
| 客服交接 | customer-support-triage | support-handoff |

方法商品提供做事步骤，不包含可执行工具、行业事实库或图像/声音素材。AI按具体缺口选择，不要求用户在表中挑选。下一批货源应补这些尚缺的商品类型，不能只重复生成通用提示词。

```powershell
python -B tools/market_stock.py --output work/my-stock
```

父目录须存在，目标必须是新目录。命令只把随程序分发的固定原创目录准备到新本地账本，无外部输入导入、网络、模型调用、客户端安装或付款。固定目录的项目内准入自检不等于独立质量审核；外部卖家仍需原卖方、审核与真实身份流程。

输出包含inventory.json、仅买方工具的MCP配置、START-HERE.md及最后生成的stock-ready.json。这里“买方”允许既有免费获取/读取/反馈，不是数据库只读权限；配置不自动安装，不向不可信用户暴露本地主体/角色选择。AI通过market_search少量取摘要，按任务决定是否加载正文。方法资料不会自动安装、执行代码或替代专业领域知识。

也可从项目原目录直接用`python -m ai_supermarket search "数据清洗" --kind skill`搜索，原目录用途为project_local；准备后的开放市场内部试用用途为personal_use，两者不要混用。

许可延续项目现有Project-Local范围，仅限项目内使用和修改，原许可不扩大到公开出售、分发或训练。价格0表示内部试用资源免费，不代表模型费为0。商品有明确输入、输出、示例与检查方法；专业质量、用户帮助和节省token仍需真实任务验证。原有货架和账本不迁移、不合并、不覆盖。
