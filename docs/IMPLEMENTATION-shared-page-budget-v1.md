# 共享目录页面预算实施记录

状态：`shared-page-budget-1` 已在当前源码实现并通过隔离 PostgreSQL 测试。产品版本仍是 Skill/核心 **2.7.0**。这不是 2.8.0 发布，真实小批 canary（RT-01）没有执行。

## 行为

- 新的 v2 批次在计划里冻结 `page_budget_contract=shared-page-budget-1`。显式 v1 和没有该字段的旧批次继续用原来的企业页面计数，不写目录账本。
- 列表页在派发前占用 run 总预算 1 次，不按同页企业数相乘。详情页另占该企业最多 10 页的额度，并与企业抓取争用同一个 `pages + reserved_pages`。
- 成功、拒绝、超时和失败的已开始尝试各消耗 1 个页面单位。robots、重定向和物理重试记在 `physical_attempts`，不加进页面单位。
- 能证明 `dispatched=false` 的取消会释放预留。没有观测、观测读失败，或 `dispatched` 未知时，该单位改为 `unknown` 并继续占用，不退回预算。
- 同操作键、同 attempt、同输入重放不另扣。不同输入返回 `IDEMPOTENCY_CONFLICT`。
- 缓存只在本 run 内复用已成功结算的同一 URL 和页面类型，不计新页面。不读 robots 缓存，不跨 run。
- `refuse_automatic_collection`：没有合同返回 `SHARED_COLLECTION_REFUSED`；有合同也返回 `SHARED_COLLECTION_UNAVAILABLE`。没有目录自动采集器，也没有 JS 渲染。

企业抓取路径在没有目录详情占用时不算账本，页面加减和原来一样。

## Schema `skill_v2_004`

只新增 `skill_page_reservations`，唯一约束是 `(run_id, operation_key, attempt)`。没有给 discovery 加 `(run_id, source, source_ref)` 唯一约束。

升级：`prepare` / `initialize()`，或在 003 库上执行 `alembic upgrade head`。003 的旧表和旧 run 保留，缺的只是新表。

回滚：迁移的 `downgrade()` 直接拒绝。2.7.0 的初始化只认识到 `skill_v2_003`，看到 004 会报 `LEGACY_DATABASE`，不能就地降级后继续读写。恢复步骤：

1. 用升级前的数据库备份和对应工件目录恢复到一套新的库和新的目录。
2. 同时切换 `SCA_DATABASE_URL` 与 `SCA_ARTIFACT_ROOT`。
3. 不要把 004 的库指回 2.7.0 进程。

## 仍不可用

- 目录自动采集（切片 C）
- 自动 JS 渲染、新国家规则、东南亚结构化发现源（切片 D）
- discovery 跨源并行和导出内存专项（切片 E）
- 真实 200 家 / 8 小时 / 校准 canary
