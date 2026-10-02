# 极简反馈

仅在资源已获取并读取后，用返回的usage_id提交。

- useful：yes / no / unknown。无法判断就用unknown。
- check：pass / fail / not_run。未实际检查使用not_run。
- failure_code：incompatible / bad_output / delivery_failed / timeout / license_mismatch / unknown；无失败时省略。

日常不额外调用模型写长评，不上传任务全文、秘密或用户文件。
调用成功只证明运行；资料已读取只证明交付；任务结果要另行检查。
同一usage_id只提交一次，重放相同反馈可去重，不更换编号刷量。
模型反馈是agent_reported；只有受信宿主执行检查才可记录host_check，二者都不是独立质量保证。

---
来源：本项目原创；Project-Local，仅供本项目内使用与修改，对外分发许可尚未设定。

