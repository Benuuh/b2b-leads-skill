# Skill 单入口重构验证记录

日期：2026-10-24。

## 已交付

- Skill 直接调用 Python 核心；保留原七个主要命令，补充 list、抑制管理、企业删除和维护。
- 模块分为 cli、runs、pipeline、rules、web、delivery、persistence。
- 移除前端、HTTP API、认证、地图与模型适配器、标准来源发现和常驻 worker。
- 资格、联系提取校验、评分保持独立；官网补齐保留两页资格确认、十页总预算、页面复用和早停。
- 进度和导出共享交付选择；默认快照随抑制和渠道新鲜度变化刷新，具名快照可重放。
- 新数据库 sca_skill_v2，十张 skill_* 表及迁移标记表；无账号、工作区、客群版本或来源目录表。
- 每次命令触发数据库节流维护，替代旧 worker 的过期清理。

## 数据保全与切换

旧版源码和数据在本地独立归档，当前核心使用独立业务库。归档、运行目录、历史批次和数据库内容不随源码分发。

## 实际执行的验证

在全新 `.runtime/skill-clean-env` 中仅安装锁定核心/测试依赖及 editable backend；确认 FastAPI、Uvicorn 不存在，uv pip check 通过。

```powershell
.\.runtime\skill-clean-env\Scripts\python.exe -m pytest backend/tests -q --junitxml=.runtime/skill-core-junit.xml
```

结果：**193 passed，33.00 秒，无跳过**。使用隔离 PostgreSQL 测试数据库，测试结束后删除临时库。

覆盖范围：

- 规则与安全网络、资格排除零请求、两页资格上限、十页预算、早停、页面复用、MX 缓存与注入式请求观测。
- chunk 重放、版本冲突、处理提交后重试、租约回收、旧执行者隔离、硬崩溃页面预留、暂停后停止后续 HTTP 请求。
- 导出与抑制并发、默认快照刷新、渠道过期、抑制撤销、最终检查点与工具计数幂等。
- 真实子进程命令链 prepare/create/append/process/status/finish/export/list/suppressions/maintenance；中文 Excel 四表及 CSV、文件撤销。该测试使用明确标识的合成企业与受控目录证据，不访问真实企业。
- 新基线拒绝旧表、原子文件及路径边界、新库备份恢复、篡改拒绝、连接 SSL 参数保留。
- 模块依赖约束：pipeline/rules/web/delivery 无数据库或旧运行模块导入，selection 无导出/指标反向依赖。

独立核查还用固定五家企业对照了重构前后筛选、指标和导出 manifest，三种抑制方案、六份导出结果一致。Skill 的 quick_validate 检查通过。

## 精简量与限制

统计口径为 backend/app Python、scripts Python/PowerShell、旧 frontend/src TypeScript/CSS，不含测试、文档和第三方依赖：89 个文件、14,400 行降到 36 个文件、5,045 行，减少约 65.0%。本地完整统计位于 `.runtime/skill-refactor-summary.json`。

没有执行新的 200 家真实获客批次，也没有宣称真实匹配率、联系方式准确率、可达性或八小时交付达标。

处理中的企业若遇到暂停或无法确定页面消耗的中断，会保守预留剩余页数并可能以 INTERRUPTED_PAGE_BUDGET 结束，以保证恢复后不重复放大抓取预算；其他排队企业可继续。保留期清理仅在命令运行时触发，空闲机器不执行定时任务。旧批次不在新库续做。
