# 证据合同与交接

## 候选记录（EvidenceBundle v2 的入口形态）

append 的每个候选必须写明观测到的企业身份与事实。未知正式字段会被拒绝（`UNKNOWN_FIELD`）；扩展信息只放在 `extensions`，永不参与资格判断。

```json
{
  "company_name": "Observed Company GmbH",
  "country_code": "DE",
  "headquarters_country": "DE",
  "business_models": ["manufacturer"],
  "offering_products": ["reusable industrial packaging"],
  "customer_industries": ["automotive"],
  "entity_relationship": "independent",
  "website_url": "https://company.example/",
  "source_url": "https://directory.example/member/company",
  "source_type": "directory",
  "retrieved_at": "2026-10-27T02:00:00Z",
  "source_excerpt": "Observed Company GmbH, Düsseldorf, Germany, manufactures reusable industrial packaging.",
  "email": "sales@company.example",
  "sources": [
    {"field": "company_name", "value": "Observed Company GmbH",
     "source_url": "https://directory.example/member/company", "source_type": "directory",
     "retrieved_at": "2026-10-27T02:00:00Z",
     "source_excerpt": "Observed Company GmbH, Düsseldorf, Germany"},
    {"field": "entity.headquarters_country", "value": "DE",
     "source_url": "https://directory.example/member/company", "source_type": "directory",
     "retrieved_at": "2026-10-27T02:00:00Z",
     "source_excerpt": "Observed Company GmbH, headquartered in Düsseldorf, Germany"},
    {"field": "email", "value": "sales@company.example",
     "source_url": "https://directory.example/member/company", "source_type": "directory",
     "retrieved_at": "2026-10-27T02:00:00Z", "method": "agent_page_observation",
     "source_excerpt": "Observed Company GmbH — Business email: sales@company.example"}
  ]
}
```

版本化事实字段：`headquarters_country`、`registered_country`、`operating_countries`、`business_models`、`offering_products`、`offering_services`、`customer_industries`、`entity_relationship`、`parent_company`、`aliases`、`extensions`。`aliases`, 形如 `[{"name": "...", "source_url": "...", "source_excerpt": "...", "retrieved_at": "..."}]`，只有同时提到企业名与别名的观察才能用于归并。

## 观察规则

- 每条事实与渠道都需要独立观察：完整 URL、来源类型、原文摘录、带时区采集时间、方法；表单还需 `form_fields` 中的回复与留言输入。
- 摘录必须以该企业为主语。客户、合作伙伴、集团或新闻主角的能力不能转移给候选。
- 搜索摘要只进入发现池，不能作为资格或渠道证据。
- 官网归属与业务资格分别取证；名称相同不足以确认官网。
- 同一事实出现多个有来源且不一致的取值 → `conflict`，进入待核，不猜一个。
- 未知就是未知：没有证据不会被改写成 `fail`。

## 状态流转

| 状态 | 含义 |
|---|---|
| `discovered` | 发现身份线索，允许摘要；不计准入或合格 |
| `evidence_pending` | 已确定需要补哪些主体、资格或渠道证据 |
| `append_ready` | 通过逐条预检，可按原子 chunk 提交；不代表资格通过 |
| `admitted` | 核心已接收，进入正常资格与交付流程 |
| `needs_evidence` / `needs_review` | 事实未知、来源受限或存在冲突 |
| `qualified_contactable` | 必要条件满足、无命中排除、渠道满足、经抑制与去重后可交付 |
| `excluded` | 有证据证明必要条件不满足或命中排除 |

研究清单超过检查点上限（20,000 字符）时，把完整清单写入忽略的运行工件目录，检查点只保留路径、hash、schema 版本与小索引。

## 入口预检

`validate-input --spec-json ... --input-json ...` 离线逐条校验（无 HTTP/DNS、无写库、无隐式维护），返回每条记录的 `index`、`admissible`、`reason_code`、`path`、`normalized_hash` 以及可提交子集。把有效子集作为一个原子 chunk 提交；`append` 对含无效草稿的 chunk 整体拒绝，不会部分写入。同一 `--chunk` 重放返回原结果，不新增候选。

## 证据优先与补证

1. 先复用已保存证据，对每条必要条件输出 `pass`/`fail`/`unknown`/`conflict` 与理由。
2. 全部满足且渠道政策满足 → 直接完成，零网络；没有缺项不抓官网。
3. 只为缺项生成字段级补证计划；只访问允许的来源。
4. 每次新增证据后重新判断停止条件。

`supplement --run RUN --company-id ID --input-json FILE --operation-key KEY` 为已保留企业追加增量证据：

- 输入含 `expected_revision`；版本不一致返回 `VERSION_CONFLICT`，不影响新修订。
- 身份字段（企业名、官网、国家、分支）不可修改（`IMMUTABLE_IDENTITY`），不能用改名制造新候选。
- 没有新证据返回 `NO_NEW_EVIDENCE`，不重复抓取。
- 保留旧证据与采集时间，只重算受影响阶段；离线完成，不产生 HTTP/DNS，不恢复派发。
- 输入关闭后拒绝（`INPUT_CLOSED`）；暂停时可保存但不派发。
- 不增加唯一候选数。

`reassess` 仍只读保存输入/快照、零 HTTP/DNS、最多 20 家，不接受新证据，也不修复失败任务。

## 失败分类

`input_invalid`、`fact_unknown`、`subject_conflict`、`transient_network`、`access_denied`、`capacity`、`budget_exhausted`、`evidence_unavailable`、`tool_failure`。只有“有证据的事实不满足”才是资格失败。连续工具/网络故障触发熔断：停止派发、保存检查点、交付现有结果并说明阻塞；`status --action retry` 显式关闭熔断。
