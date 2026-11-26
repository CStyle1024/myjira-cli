# myjira-cli

独立的 Jira CLI，不依赖 issue-resolver 或兄弟项目源码。Python >=3.8，Linux，YAML 输出由 PyYAML 提供。

## 安装

需要 Bash、Git、`uv` 和 GitHub 仓库访问权限。发布安装脚本后，可执行：

```bash
curl -fsSL https://raw.githubusercontent.com/CStyle1024/myjira-cli/main/install.sh | bash
```

已有源码时可直接运行 `bash install.sh`。脚本默认通过 HTTPS 浅克隆 GitHub 仓库的默认分支，使用 `MYJIRA_CLI_REPO` 可指定仓库地址，使用 `MYJIRA_CLI_REF` 可选择分支或标签。安装结束后清理临时源码目录。如果终端找不到命令，运行 `uv tool update-shell` 后重新打开终端。

也可以在源码目录中用 pip 安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install wheel
python -m pip install --no-build-isolation .
myjira-cli --help
myjira-cli --tool-info
```

也可从源码执行 `PYTHONPATH=src python3 -m myjira_cli --help`。分发包与命令同名，均为 myjira-cli。

## 配置

### 交互式初始化

在终端运行：

```bash
myjira-cli config init
myjira-cli doctor
```

向导询问 REST 地址和 Jira PAT。PAT 输入时关闭回显，按 Enter 提交。验证通过后，PAT 保存在独立配置目录下权限为 `600` 的凭据文件里。配置 `${XDG_CONFIG_HOME:-$HOME/.config}/myjira-cli/config.json` 只记录该文件路径。随后运行 `doctor` 时可直接读取凭据。

如果已有环境变量，或需要 Basic 认证，使用 `myjira-cli config init --auth-env`。向导会询问认证方式和环境变量名，后续命令继续从环境读取凭据。

向导通过当前账号和 `serverInfo` 验证连接并探测 Jira 部署类型。服务未报告可识别的类型时，提示手动选择或保留未知。部署类型作为配置信息记录，现有 REST 命令仍使用 Jira API v2。

普通命令的配置优先级为 `--config`、`ISSUE_RESOLVER_CONFIG`、独立配置、当前目录的 `project.json`。初始化始终只写独立配置。身份验证不检查写入权限。


使用 `--config /absolute/path/config.json`，或设置 `ISSUE_RESOLVER_CONFIG`；默认读取当前目录的 project.json。可使用完整工作流配置，也可只包含 jira 服务配置。认证使用环境变量或文件引用，不把凭据写进 JSON。

```bash
cp examples/jira.json jira.local.json
# 填写服务地址及认证引用后运行
myjira-cli --config jira.local.json doctor
```

## 查询与归档

全局选项可放在子命令之前或之后；默认输出 JSON。

```bash
myjira-cli --config /path/to/project.json doctor
myjira-cli --config /path/to/project.json me
myjira-cli --config /path/to/project.json issues list --assignee me
myjira-cli --config /path/to/project.json issues list --jql 'project = DEMO AND statusCategory != Done'
myjira-cli --config /path/to/project.json issue get DEMO-123
myjira-cli --config /path/to/project.json issue comments DEMO-123
myjira-cli --config /path/to/project.json issue transitions DEMO-123
myjira-cli --config /path/to/project.json issue changelog DEMO-123
myjira-cli --config /path/to/project.json issue fetch DEMO-123 --output-dir /path/to/bugs/DEMO-123
myjira-cli --config /path/to/project.json attachment download 12345 --output-dir /path/to/attachments
```

`doctor` 只检查配置、身份和可访问性，不宣称具有写权限。搜索和备注自动分页。Jira v2 expand 返回的历史若被截断或缺失，`changelog.complete=false`，归档产生明确警告。

`fetch` 保存原始 issue、完整备注和历史元数据到 `issue/snapshots/<hash>.json`，最新索引为 `issue/manifest.json`，人读摘要为 `issue.md`。附件保存为 `attachments/<id>/<sha256>.bin`，原始文件名/MIME 等原始元数据保留在快照中，文件名与摘要等下载记录位于每个 ID 的 `record.json`。同名附件不会覆盖；缓存复核摘要，下载大小不一致不会标记成功。部分附件失败时输出 FAILED 及逐项失败，重跑可补齐；先前快照保留。

## 状态转换、评论和指派

写命令缺省只预览；执行需显式 `--execute --operation-id ID`。本工具不决定 bug 的业务生命周期。

```bash
# 先查询允许的转换；ID 和必填字段由当前 issue 决定。
myjira-cli --config /path/to/project.json issue transitions DEMO-123
myjira-cli --config /path/to/project.json issue transition DEMO-123 --transition-id 21 --preview
myjira-cli --config /path/to/project.json issue transition DEMO-123 --transition-id 21 --fields-file /path/to/fields.json --execute --operation-id demo-123-start-001

# fields.json 为字段值对象，例如 {"resolution": {"id": "1"}}；不要猜测字段和 ID。
# 评论来自 UTF-8 文件；--body-file - 则从 stdin 读取。
myjira-cli --config /path/to/project.json issue comment DEMO-123 --body-file /path/to/comment.txt --preview
myjira-cli --config /path/to/project.json issue comment DEMO-123 --body-file /path/to/comment.txt --execute --operation-id demo-123-comment-001

# --user 是 Jira Server 的 username，不是显示名或 Cloud accountId。
myjira-cli --config /path/to/project.json issue assign DEMO-123 --user tester --preview
myjira-cli --config /path/to/project.json issue assign DEMO-123 --user tester --execute --operation-id demo-123-assign-001
```

评论执行时添加 `[jira-cli-operation:…]` 关联标记，预览会说明这一点。成功写入后回读远端。状态转换检查允许转换及必填字段；指派先检查目标用户可分配。

回执默认保存在 `${XDG_STATE_HOME:-~/.local/state}/issue-resolver/actions/jira`，也可通过 `--state-dir`、jira.state_dir 或顶层 state_dir 指定。一个自动化流程应使用固定的绝对目录并保留它，不能把回执当可随意删除的缓存。文件权限为 0600。

同 ID/同输入重复执行会复用回执，同 ID/不同输入报冲突。超时或中断返回 UNKNOWN，不自动重发写请求；使用**原命令、原 ID、原输入、原回执目录**再次调用只做远端对账。评论可凭精确正文、标记和作者确认；状态/指派仅当前值符合期望不足以证明是本次操作成功，因此可能一直保持 UNKNOWN。此时人工检查 Jira 历史并决定下一步，勿换新 ID 盲目重试。

这是一份保守的动作台账，不保证跨系统 exactly-once。读取检查与远端写入之间仍可能有其他人修改 issue，工具会核对写入后的状态，不提供 Jira 侧的原子业务锁。

## 机器调用契约

`--format json`（默认）stdout 只含一个结果对象；`--format text` 以缩进 JSON 便于人工查看。`--format yaml` 将同一个结果对象完整序列化为 YAML，例如 `myjira-cli doctor --format yaml`。帮助和版本信息为常规文本。错误不会回显原始异常、凭据或服务端登录页。

```json
{"schema_version": 1, "status": "SUCCESS", "data": {}, "error": null}
```

写入结果另含 operation_id、receipt_path。status 为 SUCCESS / FAILED / UNKNOWN；不要仅根据退出码或 stderr 判断某次未知写入是否发生。

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功，包括只读预览 |
| 2 | 参数/配置错误 |
| 3 | 认证或权限错误 |
| 4 | 冲突或前置条件不满足 |
| 5 | 已知远端失败/归档部分失败 |
| 6 | 写入结果未知，需对账 |
| 7 | 本地错误 |


`--tool-info` 不读取配置或连接服务，输出 distribution、command、version、result_schema，供调用程序识别本项目及兼容契约。此接口不是程序签名或安全认证。

## 验证与构建

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
```

测试使用本地 HTTP 服务和临时 Git 仓库；真实服务的写操作不属于拆分验收。默认操作回执目录继续使用 ~/.local/state/issue-resolver/actions/jira（遵守 XDG_STATE_HOME），升级时不要删除。UNKNOWN 必须使用原 operation ID 与输入对账。
