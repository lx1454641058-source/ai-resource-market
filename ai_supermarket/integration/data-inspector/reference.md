# 本地数据检查工具 v1

本项目原创，仅限项目内部使用与修改；未授予公开分发、转售或训练许可。入口`tools/market_data_inspect.py`仅依赖Python标准库，无自动执行第三方脚本或联网。使用已有Python运行时，不随包附带Python。参考官方[CSV](https://docs.python.org/3.12/library/csv.html)、[JSON](https://docs.python.org/3.12/library/json.html)、[SQLite](https://docs.python.org/3.12/library/sqlite3.html)接口文档，以原创代码实现，没有复制文档正文或库代码。

## 调用与状态

将路径替换为实际已核内部包入口和获准数据文件，参数作为独立参数传递，避免拼接shell字符串。

```text
python /package/tools/market_data_inspect.py doctor
python /package/tools/market_data_inspect.py csv --input /task/input.csv
python /package/tools/market_data_inspect.py json --input /task/input.json
python /package/tools/market_data_inspect.py sqlite --input /task/input.sqlite --table orders --limit 5
```

`checked`表示检查执行完成，不表示数据无业务问题；CSV异常数量非零仍是checked。JSON语法/重复键/非标准常量问题返回invalid和原因；读取、运行时、结构/大小限制返回error。返回字段source.bytes/sha256绑定已读取内容。JSON节点统计不含对象键，max_depth根节点为0。字符串中的孤立代理码拒绝；大指数有限十进制用Decimal解析，不先变成浮点Infinity。无修改/格式化/修复入口。

CSV必须明确分隔符及首行表头约定；不靠Sniffer猜。格式异常用csv解析器报错，定位中的record含表头、end_line是物理结束行，二者在多行字段下不同。最多返回10个列数不齐位置和10个疑似公式位置；公式候选包括负数字符串，不等于恶意公式。空值计数只计现有的空字符串，不替代缺失列统计；空表头、重复表头单独列出。

SQLite需要支持deserialize且SQLite至少3.37的Python构建。读入最多16MiB独立数据库文件，在内存执行固定查询；查询只读、trusted_schema关闭，并设置约2秒/100万虚拟机步上限。拒绝WAL文件头、相关日志/共享内存文件、未知表、视图及虚拟表。不会为缺失路径创建数据库。表名从实际普通表清单选择并正确引用，不允许SQL参数。只读原文件不等于能读取活跃WAL中的最新事务，必须先取得一致快照。

默认列出最多20张普通表，每表前8列；指定表时最多16列，样本最多20行，默认5行。生成列只在结构中显示，样本不执行其表达式；BLOB只给长度，文本超过256字符返回前缀和总字符数。过大的JSON输出返回output_limit_reduce_sample，调用方减小limit；不声称完整导出。SQLite无穷值作为nonfinite_number标记，不输出非法JSON常量。

## 商品与执行的区别

本轮三个商品是`kind=skill`的静态Markdown，声明`read_markdown`、`data_inspector_v1`能力，`file_read`/`code_execution`及`market-data-inspect-v1`依赖。它们是附有本地程序的使用说明，不是新增的远程工具协议；原市场tool种类的任意脚本执行仍未实现。宿主确认内部包和输入范围后，用固定入口完成实际检查，不能把商品里的命令当无条件授权。

示例货架配置只服务本轮隔离原创样例验证，未安装到现有客户端。若实际宿主没有程序或访问权限，预检应返回不兼容，不通过修改默认权限伪装已接入。工程样例通过不等于用户任务收益、独立审核或专业认证。
