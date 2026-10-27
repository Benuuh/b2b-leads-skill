# 提速优先 v2：实施与性能记录

日期：2026-10-27。依据：[优化 spec](specs/SPEC-speed-first-v2.md)。已安装 Skill/核心 **2.2.0**，发布 `<release-2.2.0-id>`（61 个文件）。schema 不变；新规则标识 `b2b-policy-2`。

## 提速效果

所有数据均为本机实际测量。合成数据与受控请求延迟不能替代真实搜客的端到端速度，也不能将不同实验的下降百分比相加。

| 指标 | 2.1.0 冻结基线 | 2.2.0 | 变化 |
|---|---:|---:|---:|
| 受控页面处理：6 家/批、3 次重复，批次中位耗时 | 4.435 秒 | 1.312 秒 | 耗时减少 70.4%，约 3.38 倍吞吐 |
| 同一 18 次企业处理所需的页面请求 | 54 | 36 | 减少 33.3% |
| 单企业处理 P50 | 0.722 秒 | 0.182 秒 | 此合成场景的实测分位值 |
| 单企业处理 P95 | 0.807 秒 | 0.406 秒 | 此合成场景的实测分位值 |
| 独立 cProfile 轮次的 BS4 解析次数 | 792 | 90 | 减少 88.6% |
| 独立 cProfile 轮次的联系方式提取次数 | 90 | 54 | 减少 40.0% |
| 实际 CLI 正常块：三命令 vs advance，3 次交替顺序测量的中位耗时 | 2.736 秒 | 1.169 秒 | 耗时减少 57.3% |
| 正常块的 controller 启动次数 | 3 | 1 | 减少两次启动、重复准备与返回配置 |

页面基准使用相同合成企业、政策、HTML（每页 80 个产品块）、返回渠道和每请求 50 ms 的模拟延迟。基线先进入 Team 再 Contact；新路径直接进入 Contact。18 次结果的资格状态和返回渠道逐一相同。没有真实网络调用。

环境为 Python 3.12.2、Windows 11，本地锁定依赖。用于耗时结论的轮次未开启 profiler；解析与提取次数来自另一个双方均开启 cProfile 的轮次，避免把分析器开销混入日常速度结论。18 个样本不足以推断生产 P95。

## 已实现

1. **Agent 研究节奏。** 新任务用 v2；速度优先显式选 4 个公司 worker。多条独立查询/URL 按工具能力批量处理，优先信息密度高的目录。必要资格事实和官网关联确认后立即交给核心补渠道，Agent 继续发现企业；目录上已有的渠道直接复用。首批 5 家即可提交，后续 10–20 家一块，避免每轮无变化 status/export。
2. **单命令 advance。** `advance --run RUN --chunk ROUND --input-json FILE` 内部执行原子准入检查、追加和有界处理，只初始化一次。坏输入不部分写入；参数错误在候选写入前拒绝；append 和 process 的独立持久账本支持提交后中断恢复。精简响应保留进度、缺项和快照，省去重复 spec/plan。
3. **按需求选页面。** v2 普通公司渠道优先已发现的 Contact，再 About，最后 Team。具名或岗位任务及 v1 保留原选择；不猜链接，不并行预取不需要的页面。
4. **单企业 HTML 复用。** 新增 `web/parsing.py`。完整 HTML 内容和清理投影构成键；原始树保留 JSON-LD、表单等证据，清理树隔离；每 worker 独立作用域、结束清空，最多 32 个投影、累计 4 Mi 字符的输入预算。没有跨实体共享可变树。
5. **复用联系提取。** v2 同一组保留页面的停止回调与最终保存共用提取结果；任一页面内容、时间或来源变化都会使键变化。只保留最新一组，保持零网络与联网提取路径区分。
6. **约束与观测。** `--workers` 不得提高已冻结并发；记录 `html_parse_count`、`html_cache_hits`、`html_parse_seconds`、`contact_extractions`。资格缓存指纹覆盖全部页面字段与完整 spec，避免文本/来源变化漏掉失效。

## GitHub 参考

- [Scrapy TextResponse](https://github.com/scrapy/scrapy/blob/master/scrapy/http/response/text.py)：借鉴惰性、响应范围内的解析复用。
- [Crawlee AutoscaledPool](https://github.com/apify/crawlee-python/blob/master/src/crawlee/_autoscaling/autoscaled_pool.py)：参考有界派发，保留现有协调器和事务预算。
- [Scrapy AutoThrottle](https://github.com/scrapy/scrapy/blob/master/scrapy/extensions/throttle.py)：并发与 host 限速分别控制。本轮没有缩短 robots/429 等待。
- [selectolax](https://github.com/rushter/selectolax/blob/master/README.md)：实际查看后暂缓替换解析引擎；本轮消除重复解析已经达到性能目标，未为换框架扩大范围。

具体文件 blob 标识记录在 spec。未复制这些项目源码，未新增采集框架依赖；核心依赖锁不变。PyYAML 仅安装到忽略的 `.runtime/skill-validator` 用于执行 Skill Creator 校验，不进入核心或发布依赖。

## 验证与实际安装

- 完整测试：**374 passed，0 skipped，62.11 秒**。新覆盖包括原始/清理树隔离、线程隔离与缓存上限、具名/普通渠道选页、联系结果复用、advance 的准入原子性、非法参数、暂停和提交后恢复。
- Skill Creator `quick_validate.py` 通过；版本号放在受支持的 `metadata.version`，正文及核心仍统一 2.2.0。版本测试再次通过。
- `pip check`：无冲突。
- 标准安装器冻结并激活发布，安装入口 capabilities 返回 2.2.0、`advance`、速度配置及规则版本。
- 实际安装入口隔离验收：临时 PostgreSQL 数据库中创建合成任务，advance 的相同输入重试结果完全一致；Excel 四表及导出 manifest 的实际发布标识通过；临时数据库在 finally 中删除。
- 原 2.1.0 完整冻结发布保留，可用安装器重新激活。未创建/恢复已有真实搜客任务，未向 GitHub 发布代码。

## 次要修复与保留边界

按用户优先级，仅加了窄的资格防护：明显否定、自身仅向制造商销售软件、同一目录 URL 另一企业的能力，均不再直接交付；有相应回归。

上一轮发现的总部/经营国家谓词区分、资格事实有效期，以及更复杂的语义表达尚未全面修复；本轮没有宣称所有证据漏洞消失。真实 Agent 搜索耗时仍受工具与来源影响，尚未对一个完整真实 200 家任务做前后对照。4 worker 是受限并发配置，不代表所有网站或任务都自动快两倍。

## 复现

从项目根目录执行相同参数的两轮（依次运行，避免争抢 CPU）：

```powershell
.\.venv\Scripts\python.exe -B scripts/benchmark_speed.py --backend .runtime/skill-releases/<release-2.1.0-id>/backend --output .runtime/speed-v2-baseline-timing.json
.\.venv\Scripts\python.exe -B scripts/benchmark_speed.py --backend backend --output .runtime/speed-v2-optimized-timing.json
```

原始工件：`.runtime/speed-v2-comparison.json`、`.runtime/speed-v2-baseline.prof`、`.runtime/speed-v2-optimized.prof`、`.runtime/speed-v2-cli-smoke.json`、`.runtime/speed-v2-junit.xml`；实际安装验收脚本 `.runtime/speed_v2_cli_smoke.py`。业务与测试工件不随源码发布。
