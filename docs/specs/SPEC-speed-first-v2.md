# SPEC：B2B Leads 提速优先 v2

日期：2026-10-27。状态：已实施、测量并安装 2.2.0；结果见 [实施与性能记录](../IMPLEMENTATION-speed-first-v2.md)。用户优先级：**提速优先，漏洞修复其次**。

## 1. 目标及基线

基线为已安装 2.1.0，release `<release-2.1.0-id>`。当前完整测试 361 项通过。不得用测试通过代替提速数据。

本轮依次减少：Agent 的串行工具往返、不相关页面请求、重复 HTML 解析和重复联系方式提取。先交付可用的小批，再持续补齐。保持原目标、来源、渠道、去重、抑制与预算口径；不靠放宽资格或减少任务范围提速。

## 2. GitHub 调研与取舍

以下均于本轮通过 GitHub 连接实际读取；只参考机制，不复制第三方源码或引入整套框架。

| 来源 | 观察到的实现 | 本轮应用 |
|---|---|---|
| [Scrapy TextResponse](https://github.com/scrapy/scrapy/blob/master/scrapy/http/response/text.py)；blob `6ba03229a806872e210dd2a876226a1eb4a202a7` | 对响应文本、selector、JSON 做惰性缓存 | 单企业作用域内复用只读解析结果，内容变化即失效 |
| [Crawlee AutoscaledPool](https://github.com/apify/crawlee-python/blob/master/src/crawlee/_autoscaling/autoscaled_pool.py)；blob `fb751289ded64e1bcd8229f0f77907d75f2c32cd` | 有空闲容量且任务就绪才派发，统一协调并发和结束 | 保留预算预留与限流；新建速度配置可用 4 个公司 worker，已有批次政策不改 |
| [Scrapy AutoThrottle](https://github.com/scrapy/scrapy/blob/master/scrapy/extensions/throttle.py)；blob `44876ebcc1742bd50d8f7ccf3e4fc482b698b9f7` | 并发与 host 延迟分开，错误响应不能导致加速打源 | 不下调来源间隔、不绕过 429/robots，跨公司并行 |
| [selectolax](https://github.com/rushter/selectolax/blob/master/README.md)；blob `bcc336387b88e2f097838119c887dc46c870eaad` | Cython/Lexbor HTML 解析与 CSS 选择器 | 本轮先消除重复解析；暂不整体替换 BS4，避免兼容迁移占用提速时间。其公开 benchmark 不当作本项目结果 |

## 3. 必须实现

| 编号 | 优先级 | 行为 |
|---|---|---|
| SP-01 | P0 | 更新 Skill 的研究节奏：独立搜索/打开调用按当前工具上限批量并行；从信息密度高的目录一次获取多家；必要事实与官网确认后立即交给核心并发补渠道，不让 Agent 串行打开每家 Contact/Team；一页支持多项事实时一次交接 |
| SP-02 | P0 | 新增 `advance --run --chunk --input-json [--max-jobs --max-seconds --workers --resume]`，一次启动完成原子准入预检、append 和有界 process；旧命令保留；原输入和 chunk 重试不重复候选或处理 |
| SP-03 | P0 | v2 无具名/岗位要求时，已发现的联系页优先于团队与介绍页；具名政策及 v1 保留原优先级；不猜网址、不预取未需要页面 |
| SP-04 | P0 | 同一企业处理中复用内容相同的 HTML 解析结果。原始树与剥离脚本后的树隔离；缓存有数量/输入体积上限，结束即释放，不跨企业或线程共享可变树 |
| SP-05 | P0 | v2 同一页集合的联系提取结果在停止回调和最终保存间复用；新增页后失效；不改变采集时间、渠道校验或证据归属 |
| SP-06 | P1 | 新建“速度优先”任务显式配置 `company_workers:4`，先 5 家形成首批快照，后续 10–20 家一块；既有冻结配置不变，`--workers` 只允许收窄 |
| SP-07 | P1 | 输出 HTML 解析次数、命中和耗时，以及联系提取次数。基准分开报告请求数量、受控延迟、P50/P95 和真实企业任务，不能把受控延迟包装成线上速度 |
| SP-08 | P2 | 仅对上一轮最危险、能低成本修复的否定与跨主体误放行补窄修；不以扩大语义引擎或全面改规则阻塞 SP-01–07 |

`advance` 不提供部分提交：任何无效记录保持 append 原子拒绝。可继续用离线 `validate-input` 找出有效子集。append 已提交、process 中断时重试原命令；不得新建 run。可选 process 参数错误须在写入候选前拒绝。第一次成功的处理结果按派生 operation key 重放；追加证据使用新 chunk。

## 4. 研究编排

1. 默认选 v2；读取 capabilities 一次，完成配置与必要澄清。
2. 生成多条互补查询，独立搜索批量执行；按工具能力合并多条查询/URL，执行期间保持来源及调用计数。
3. 首批积累 5 家必要事实和官网已确认的企业即 `advance`，渠道可由核心并发补齐，不等满 20，也不为可选社媒/人员/规模继续搜；允许的并行核心调用期间，可继续独立发现工作。
4. 后续块 10–20 家；从 `advance` 返回的进度直接安排补证，避免每块再做无变化的 status/export。只有故障、会话恢复或用户询问才额外查看状态。
5. 达标即结束；用同一最终筛选导出。暂缺必要事实的企业保留缺项，先推进证据齐全的企业。

## 5. 验收

- 与同一冻结基线比较相同合成公司、HTML、政策、延迟及返回渠道；报告环境、重复次数与原始 JSON。
- 普通商务渠道场景：先 Contact，少于原来进入 Team 再 Contact 的页面请求；具名模式仍访问 Team。
- 固定受控批次的中位耗时目标下降至少 25%；若达不到，保留实测结果并继续找瓶颈，不宣称验收完成。
- HTML 重复解析次数至少下降 50%；同一停止回调/最终结果的联系提取不重复。
- `advance` 将正常块的独立 controller 启动从 validate/append/process 三次减为一次；测试提交后重试、坏输入零候选、参数错误零候选、暂停需显式恢复。
- 完整原测试及必要新测试通过，无数据库跳过；冻结、安装、capabilities 与真实入口的隔离流程验证通过。
- 本轮不接触既有真实批次，不发布 GitHub 仓库变更。真实 200 家或 8 小时目标不在没有实际研究任务时宣称达成。

## 6. 发布

版本 2.2.0。保存 2.1.0 冻结发布作为回滚点；统一 VERSION、核心、Skill 和安装记录。只用标准发布安装器激活，不直接修改安装副本。
