# 严格JSON检查（附本地工具）

用于检查已有JSON文件的语法和结构，避免默认解析器悄悄接受重复键或NaN。需要宿主已核验market-data-inspect-v1依赖，具备data_inspector_v1能力，并在当前任务范围内可读输入、运行固定检查器。

运行受信包根目录下`tools/market_data_inspect.py json --input <获准文件绝对路径>`。返回valid、结构类型计数、最大深度和文件摘要，不把原始字段值回传模型。拒绝重复键、NaN/Infinity和无效Unicode；大指数使用十进制解析，不转换为浮点无限值。

样例：`{"id":1,"id":2}`得到valid=false、duplicate_json_key；`[1,true,null]`得到数组以及数字/布尔/null各1个。非法语法返回行列定位，不输出包含隐私值的原错误段。

valid=true只表示通过本工具的语法/结构检查，不证明字段齐全、符合JSON Schema或业务正确。2MiB/10万节点/128层限制和UTF-8约定需核对；格式化、修复、删除重复键不是该工具功能。保持原件，依据实际授权另行修复。

CLI和限制详见随包integration/data-inspector/SKILL.md，商品只交付说明，宿主另行运行程序。

来源：本项目原创；Project-Local，仅限项目内部使用修改，未授予公开分发、转售或训练许可。
