# Skill 核心操作手册

## 配置与启动

Python 3.12；安装 backend/requirements.lock 和 editable backend。新机器配置 `.env` 的 `SCA_DATABASE_URL`，参见 `.env.example`。`SCA_ARTIFACT_ROOT` 可覆盖默认 `data/skill-v2`。`SCA_FIXTURE_MODE=true` 仅用于合成演示，禁止正式导出。无需 SESSION_SECRET、服务 token、账号或登录文件。

安装器将 Skill、核心源码、迁移和依赖锁冻结到 `.runtime/skill-releases/<release_id>`，不复制 `.env` 或业务数据。已安装 Skill 必须通过自身 `scripts/control.py` 执行；该入口校验发布及 Skill 文件后加载冻结代码。修改 checkout 不会改变当前安装版本。执行 `scripts/install-b2b-leads.ps1` 激活新发布；使用 `-Release <已保留的 release_id>` 重新激活旧发布。开发时直接执行 `scripts/b2b_leads.py` 使用当前 checkout，其发布标识为 development。

安装与回退只选择代码，不降级数据库。升级前备份，已知 `skill_v2_001` 基线可由 prepare 增量升级；不覆盖未知数据库。旧代码若不支持当前 schema，使用对应独立恢复库及文件目录，不能只回退 Skill 文件。

Windows 本地部署可将 PostgreSQL 二进制放在 `.runtime/pgsql/bin`、数据目录放在 `.runtime/pgdata`。prepare 连接失败时只尝试启动这个既有实例；不下载软件或终止占用端口的进程。外部 PostgreSQL 由操作者启动。新业务库创建需要权限。

```powershell
.\.venv\Scripts\python.exe scripts/b2b_leads.py prepare
.\.venv\Scripts\python.exe scripts/b2b_leads.py brief --spec-json .runtime/spec.json
.\.venv\Scripts\python.exe scripts/b2b_leads.py create --run us-forwarders-01 --spec-json .runtime/spec.json --confirm-hash <confirm_hash>
.\.venv\Scripts\python.exe scripts/b2b_leads.py append --run us-forwarders-01 --chunk round-01 --input-json .runtime/round-01.json
.\.venv\Scripts\python.exe scripts/b2b_leads.py process --run us-forwarders-01 --max-jobs 20 --max-seconds 60 --operation-key process-round-01
.\.venv\Scripts\python.exe scripts/b2b_leads.py status --run us-forwarders-01 --action pause
.\.venv\Scripts\python.exe scripts/b2b_leads.py process --run us-forwarders-01 --resume
.\.venv\Scripts\python.exe scripts/b2b_leads.py finish --run us-forwarders-01
.\.venv\Scripts\python.exe scripts/b2b_leads.py export --run us-forwarders-01
```

输入形状和证据要求见 [Skill 控制参考](../skills/b2b-leads/references/controller.md)。新建批次先运行离线 `brief`，向用户展示完整确认摘要并得到明确确认后，再把 `confirm_hash` 传给 `create`。未写 `spec_version` 的新任务冻结为 v2；显式 v1 与已保存的 v1 批次保持原语义。确认摘要会写明当前取证限制：新 v2 批次冻结 `shared-page-budget-1` 只用于共享目录页面记账，目录自动采集和 JS 渲染仍不可用；没有该合同的批次拒绝自动共享目录采集。国家证据仍为字面匹配，结构化发现源未覆盖东南亚。摘要里的数量和时限是预算口径，不是完成承诺。

schema 当前为 `skill_v2_004`（在 `skill_v2_003` 上追加 `skill_page_reservations`）。`prepare` 会把 003 升到 004。不要对这条迁移执行降级：2.7.0 不能读取 004。回滚是把升级前的数据库和工件备份恢复到一套新的库和目录，再把 `SCA_DATABASE_URL` 与 `SCA_ARTIFACT_ROOT` 一起切过去。详见 [共享页面预算实施记录](IMPLEMENTATION-shared-page-budget-v1.md)。

seconds 上限在企业任务之间检查；正在执行的有限任务可能跨过该时间。任务异常最多重试三次，等待退避后再 process，或对终态失败执行 status --action retry。

## 管理命令

```powershell
.\.venv\Scripts\python.exe scripts/b2b_leads.py list
.\.venv\Scripts\python.exe scripts/b2b_leads.py suppressions list
.\.venv\Scripts\python.exe scripts/b2b_leads.py suppressions add --type domain --value example.com --reason 已有客户
.\.venv\Scripts\python.exe scripts/b2b_leads.py suppressions remove --id <suppression-id>
.\.venv\Scripts\python.exe scripts/b2b_leads.py delete-company --id <company-id>
.\.venv\Scripts\python.exe scripts/b2b_leads.py maintenance
.\.venv\Scripts\python.exe scripts/b2b_leads.py maintenance --apply
```

status --action 支持 pause/resume/cancel/retry。取消和删除按明确用户指令执行；Skill 不在普通研究中自动删除记录。抑制和删除会撤销当前系统中的相关导出，已复制到系统外的文件由持有人管理。

## 实时成果页面

```powershell
.\.venv\Scripts\python.exe scripts/b2b_leads.py live --run us-forwarders-01
.\.venv\Scripts\python.exe scripts/b2b_leads.py live --run us-forwarders-01 --rebuild-only
.\.venv\Scripts\python.exe scripts/b2b_leads.py live --run us-forwarders-01 --port 8765 --idle-timeout 3600
```

`live` 是用户主动启动的前台查看命令，不是采集或后台服务：

- 启动时把已提交状态投影到 `<SCA_ARTIFACT_ROOT>/live/<run>/projection.json`，随后启动只绑定 `127.0.0.1` 的临时 HTTP 服务；端口默认由系统分配。
- 标准输出打印一行 JSON（`url`、`host`、`port`、`expires_in_seconds`）。URL 含本次进程的随机令牌，只用于本机打开，不写日志；不要转发到外部文档或聊天。
- 默认 30 分钟无活动自动退出，`--idle-timeout` 可调（30–86400 秒）；`Ctrl+C` 立即停止。`--open` 才调用默认浏览器，不会自动弹窗。
- 页面只读：不采集、不改任务状态、不自动导出。导出按钮由命令把当前筛选交给核心，核心重新校验交付政策后返回真实文件、数量和快照时间。
- 页面停止或断开时显示离线与最后更新时间；任务是否仍在运行只按已保存的真实状态说明，超过 120 秒没有新观察的“运行中”显示为未知。
- 抑制、删除企业和过期清理会重建已保存页面；重建失败写入 `pending.json`，页面提示“可能已过期”。投影默认保留 30 天，`maintenance --apply` 只删除已知页面文件。
- 对外访问控制：Host 只接受 `127.0.0.1`/`localhost` 加当前端口，POST 导出还要求同源 Origin 和 `X-Live-Request`；响应含严格 CSP，脚本样式随包提供，无 CDN。

页面展示不等于交付：已确认可交付数量、待核实名单和正式导出都来自同一已提交核心选择；待核实企业不进入正式导出，公开渠道也不代表已实际验证送达。

默认原始快照、导出、操作审计保留 30 天，企业及终态任务输入 365 天；B2B_RAW_RETENTION_DAYS / B2B_EXPORT_RETENTION_DAYS / B2B_LOG_RETENTION_DAYS / B2B_CONTACT_RETENTION_DAYS 可覆盖。备份有独立保留周期。

## 备份恢复

```powershell
.\.venv\Scripts\python.exe scripts/backup_restore.py backup --output .runtime/backups/skill-01
.\.venv\Scripts\python.exe scripts/backup_restore.py restore --source .runtime/backups/skill-01 --target-database sca_skill_restore_01 --target-artifacts .runtime/restored-skill-01
```

备份使用 PostgreSQL 一致性快照，包含引用的原始页面、导出和报告；恢复只能使用不存在的新库和新目录，并核对表数据哈希和文件哈希。恢复后将 SCA_DATABASE_URL 与 SCA_ARTIFACT_ROOT 一起切换。备份目录可能包含真实业务联系人，不纳入 Git。

新版本不续做旧库批次，不重置旧库。回退时使用已验证的代码版本及其兼容的独立数据库/文件备份，数据库与文件目录必须一起切换。

处理中断的页面消耗按持久请求观测保守恢复。若无法确定消耗或在执行中暂停，会预留该企业剩余页面额度，该企业可能以 INTERRUPTED_PAGE_BUDGET 结束；其他排队企业仍可继续。优先在企业处理命令结束后暂停。
