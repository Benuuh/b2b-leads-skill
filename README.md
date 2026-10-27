# B2B 客户研究：Skill 单入口

通过 Skill 搜索并核对目标企业，再由本机 Python 核心有限补齐官网资料、校验联系方式、保存进度和导出中文 Excel。只需要 Python 3.12 与 PostgreSQL，不启动网页、HTTP API 或常驻 worker。

同一 Skill 与核心可加载不同的客群配置，按相同证据、状态、预算与交付机制研究不同行业：`spec_version: 2` 把资格条件、渠道政策与执行参数配置化，行业代码不需要改动。已有的无版本输入仍按 v1 解释。

## 使用

```powershell
.\.venv\Scripts\python.exe scripts/b2b_leads.py capabilities
.\.venv\Scripts\python.exe scripts/b2b_leads.py prepare
.\.venv\Scripts\python.exe scripts/b2b_leads.py list
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install-b2b-leads.ps1
```

`capabilities` 与 `validate-input` 完全离线：前者列出支持的配置版本、事实字段、运算符、渠道政策和核心限额，后者逐条预检候选并返回可提交子集。`process` 在 v2 批次按冻结的执行政策有界并发（默认 2 个 worker），`supplement` 为已保留企业追加增量证据。

2.2.0 的速度路径：`advance --run RUN --chunk ROUND --input-json FILE` 将原子预检/追加及有界处理合为一次启动；v2 普通渠道先访问已观察到的联系页，并在单企业内复用 HTML 解析和联系提取。提速优先的新任务可显式选 4 个公司 worker，首批 5 家形成快照。说明与实测见 `docs/specs/SPEC-speed-first-v2.md` 及本轮实施记录。

安装后可以说：“用 $b2b-leads 找 200 家美国货代，要公开商务联系方式，导出中文 Excel。”

Skill 负责搜索、打开页面和提交证据；核心负责资格判断、有限官网补齐、联系方式校验、去重、抑制、评分、恢复和文件交付。不自动发送邮件、打电话或写入 CRM。

默认目标 200 家，候选上限为目标三倍，累计非暂停八小时；一次最多追加 20 家。企业资格确认最多两页，通过后继续补齐，每家公司合计最多十页，满足联系目标提前停止。数量 50–1000；小批验证保留真实目标并报告缺口。

## 结构

```text
skills/b2b-leads/       Agent 工作指引和薄包装
scripts/               命令入口、安装、数据库生命周期、备份
backend/app/
  cli.py               解析命令并输出 JSON
  capabilities.py      版本化能力注册表（离线）
  schemas.py           客户配置 v1/v2 与输入合同
  runs.py              批次生命周期、候选、租约、并发、快照和幂等
  pipeline.py          单企业处理顺序、证据优先与预算
  failures.py          失败分类与熔断判定
  rules/               资格政策、身份、联系方式、评分
  web/                 安全 HTTP、官网抓取、DNS/MX、请求观测
  delivery/            渠道政策、公共筛选、质量指标、Excel/CSV 编码
  persistence/         PostgreSQL、文件、备份恢复
backend/tests/         规则及隔离 PostgreSQL 集成测试
```

详细说明见 [模块与数据流](docs/architecture.md)、[操作手册](docs/operations.md)、[客群配置](skills/b2b-leads/references/segment-spec.md)、[证据合同](skills/b2b-leads/references/evidence-contract.md)、[本轮改造实施记录](docs/IMPLEMENTATION-skill-performance-v1.md)。

## 新旧数据隔离

新版本默认使用 `sca_skill_v2` 数据库和 `data/skill-v2` 文件目录。已有私有 `.env` 的 `DATABASE_URL` 只用于复用连接参数，数据库名自动改为 `sca_skill_v2`；也可设置独立的 `SCA_DATABASE_URL`。旧登录资料不再使用。

已有旧表的数据库会被拒绝，不重置旧迁移。旧库和历史源码应独立归档；本仓库不包含历史数据或归档。旧批次不在新版本中续做。

## 安装与测试

版本号以根目录 `VERSION`、`backend/app/__init__.py`、`backend/pyproject.toml` 与 `skills/b2b-leads/SKILL.md` 头部一致；运行时可从 `capabilities` 的 `skill_version`/`core_version`、`prepare`/`status` 的 `release` 以及导出清单的 `runtime_release` 读取。当前版本 `2.2.0`。

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
