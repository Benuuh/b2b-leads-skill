# B2B 客户研究：Skill 单入口

通过 Skill 搜索并核对目标企业，再由本机 Python 核心有限补齐官网资料、校验联系方式、保存进度和导出中文 Excel。只需要 Python 3.12 与 PostgreSQL，不启动网页、HTTP API 或常驻 worker。

## 使用

```powershell
.\.venv\Scripts\python.exe scripts/b2b_leads.py prepare
.\.venv\Scripts\python.exe scripts/b2b_leads.py list
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install-b2b-leads.ps1
```

安装后可以说：“用 $b2b-leads 找 200 家美国货代，要公开商务联系方式，导出中文 Excel。”

Skill 负责搜索、打开页面和提交证据；核心负责资格判断、有限官网补齐、联系方式校验、去重、抑制、评分、恢复和文件交付。不自动发送邮件、打电话或写入 CRM。

默认目标 200 家，候选上限为目标三倍，累计非暂停八小时；一次最多追加 20 家。企业资格确认最多两页，通过后继续补齐，每家公司合计最多十页，满足联系目标提前停止。数量 50–1000；小批验证保留真实目标并报告缺口。

## 结构

```text
skills/b2b-leads/       Agent 工作指引和薄包装
scripts/               命令入口、安装、数据库生命周期、备份
backend/app/
  cli.py               解析命令并输出 JSON
  runs.py              批次生命周期、候选、租约和幂等
  pipeline.py          单企业处理顺序与预算
  rules/               资格、联系方式、评分
  web/                 安全 HTTP、官网抓取、DNS/MX、请求观测
  delivery/            公共筛选、质量指标、Excel/CSV 编码
  persistence/         PostgreSQL、文件、备份恢复
backend/tests/         规则及隔离 PostgreSQL 集成测试
```

详细说明见 [模块与数据流](docs/architecture.md)、[操作手册](docs/operations.md)、[重构验证记录](docs/refactor-acceptance.md)、[当前生产验收](docs/production-acceptance.md)。

## 新旧数据隔离

新版本默认使用 `sca_skill_v2` 数据库和 `data/skill-v2` 文件目录。已有私有 `.env` 的 `DATABASE_URL` 只用于复用连接参数，数据库名自动改为 `sca_skill_v2`；也可设置独立的 `SCA_DATABASE_URL`。旧登录资料不再使用。

已有旧表的数据库会被拒绝，不重置旧迁移。旧库和历史源码应独立归档；本仓库不包含历史数据或归档。旧批次不在新版本中续做。

## 安装与测试

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e backend
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

新机器先准备 PostgreSQL 16 服务及 `pg_dump`、`pg_restore` 客户端工具；Windows 本地工具可放在 `.runtime/pgsql/bin`，或加入 PATH。首次配置时将 `.env.example` 复制为本地 `.env`，替换占位密码和连接参数，不要提交该文件。

配置 PostgreSQL 连接后运行 `prepare` 创建独立业务库和基线。集成测试使用临时 `sca_test_*` 数据库；测试连接需有创建测试库权限。没有数据库配置时会跳过数据库测试，跳过不算验收通过。

自动测试和少量流程验证不等于已完成 200 家真实交付，也不证明邮箱或电话实际可达。

## 源码发布与隐私

此仓库为当前版本的脱敏源码快照，使用新的初始提交。环境配置、数据库、备份、客户资料、导出文件、个人安装标记和原工作区 Git 历史未包含在内。业务文档中的企业、人员与联系方式样例均为虚构占位数据；第三方署名和许可证保留。
