confirmed/unknown.json 是由拆分前 common/actions 摘要规则生成的固定样本。jira-*.json 或 gerrit-*.json 由旧 local-issue-resolver 安装版本实际执行模拟操作生成，均使用虚构服务、用户与临时目录，无真实凭据。测试通过独立 CLI 入口回读，并断言无重复 POST/push。
