---
name: outreach-letter
metadata:
  version: "1.0.0"
description: "Outreach letter skill 1.0.0 · Generate first-touch/follow-up/after-quote outreach letters for companies researched by b2b-leads. Every letter must cite at least one piece of stored evidence; letters with no evidence are never generated. Output is a local letters/<company_id>.md file for the human to send manually. Use for writing开发信/触达信 based on sourced company evidence."
---

# Outreach letters（开发信）

把 b2b-leads 产出的"名单 + 证据"变成一封封可人工发送的开发信。**本 skill 不发信**：
不接邮箱 API、不自动发送、不写 CRM。产出 `letters/<company_id>.md` 后由人审阅发送。

## 边界（每次启动先读）

- 每封信必须引用至少一条该公司证据，正文里能看出引用的是哪条。
- **证据为空不生成信**，返回 `{"status":"insufficient_evidence","missing":[...]}`。
  绝不生成"没有证据但看着像有"的信（硬规矩 #13）。
- 不用对方未公开信息，不提对方的内部人员关系。
- 不承诺价格、交期、认证。
- 同批次任意两封正文的 12 字重复率必须为 0；工具会校验并拒绝违规批次。
- 无 AI 时按 `角色 × 阶段` 12 档模板兜底，模板留 `{{证据}}` 占位由人填。

## 用法

```bash
python skills/outreach-letter/scripts/generate.py --input letters-input.json --out-dir letters/
```

输入 JSON（单封或 `{"letters": [...]}` 批量）：

```json
{
  "company_id": "c_ab12",
  "company_name": "Beispiel Logistik GmbH",
  "evidence": [
    {"field": "offering.services", "value": "air freight",
     "source_url": "https://example.com/services", "excerpt": "we operate daily air freight charters"}
  ],
  "contact": {"name": "Anna Smith", "role": "procurement"},
  "stage": "first_touch"
}
```

`role` ∈ procurement | owner | technical | unknown；`stage` ∈ first_touch | follow_up | after_quote。

## 写法矩阵

| 角色 | 关心什么 | 信落点 |
| --- | --- | --- |
| procurement | 价格、交期、稳定性 | 同类采购经验、报价口径 |
| owner | 风险、总成本 | 出过什么问题、怎么避免 |
| technical | 规格、认证 | 参数、认证、样品 |
| unknown | 先确认身份 | 一句话自述 + 一个问题 |

| 阶段 | 该做 | 不该做 |
| --- | --- | --- |
| first_touch | 说清你是谁、为什么找他（引用一条证据） | 谈价、附报价单、问"需要吗" |
| follow_up | 补一条新信息（新品、案例、政策变化） | 重复第一封 |
| after_quote | 问卡在哪，推进决策 | 催单 |

## 交付

输出文件头固定：

```markdown
# <公司名>
- 收件人：<角色>
- 阶段：<stage>
- 引用证据：<source_url> — <excerpt>

## 主题
## 正文
```

先人工过一遍再放开（§6 风险：信件质量不稳）。发送是人的动作。
