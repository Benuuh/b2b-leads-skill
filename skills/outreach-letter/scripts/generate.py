"""Outreach-letter skill（spec b2b-leads-2.6 §3.2）：证据驱动的开发信生成。

硬约束：
- 证据为空不生成信 → ``{"status":"insufficient_evidence","missing":[...]}``
- 每封信必须引用至少一条该公司证据
- 不承诺价格、交期、认证（模板层面杜绝）
- 同批次任意两封正文 12 字重复率为 0；违规整批拒绝
- 无 AI：按 角色×阶段 12 档模板兜底，{{证据}} 占位由人填
- 只产出 letters/<company_id>.md，绝不发送

仅标准库，独立于 b2b-leads core。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROLES = ('procurement', 'owner', 'technical', 'unknown')
STAGES = ('first_touch', 'follow_up', 'after_quote')
NGRAM = 12

# 角色 × 阶段 12 档模板。{{证据摘录}} 与 {{来源}} 由工具填充；
# 人工模式可留占位由人填。模板不含价格/交期/认证承诺。
TEMPLATES = {
    ('procurement', 'first_touch'): (
        '贵司的采购范围与我们的服务方向',
        '您好 {contact}：\n\n'
        '我们在{行业}领域为采购团队提供支持，注意到贵司{证据摘录}（来源：{来源}）。'
        '如果您愿意了解我们的服务口径与以往合作方式，我可以先整理一份简短介绍。\n\n'
        '{署名}'),
    ('procurement', 'follow_up'): (
        '补充一项可能与贵司采购相关的信息',
        '您好 {contact}：\n\n'
        '上次的来信之外，补充一点新信息：贵司的{证据摘录}（来源：{来源}）'
        '让我们想到一个此前服务类似客户时的做法，或许对贵司的采购稳定性有参考价值。'
        '如需细节我可以展开。\n\n{署名}'),
    ('procurement', 'after_quote'): (
        '关于贵司此次采购的决策进展',
        '您好 {contact}：\n\n'
        '此前我们围绕贵司{证据摘录}（来源：{来源}）做过沟通。'
        '如果此次采购有尚待澄清的环节，我可以针对性补充材料。\n\n{署名}'),
    ('owner', 'first_touch'): (
        '与贵司业务相关的一次主动沟通',
        '{contact} 您好：\n\n'
        '作为同业者，我们关注到贵司{证据摘录}（来源：{来源}）。'
        '同类业务里常见的几个风险点我们有些经验，愿意先做一次不带义务的交流。\n\n{署名}'),
    ('owner', 'follow_up'): (
        '再谈贵司业务中的一个风险视角',
        '{contact} 您好：\n\n'
        '结合贵司{证据摘录}（来源：{来源}），再补充一个总成本视角的观察，'
        '来自我们服务类似规模企业的经验。若您觉得有共鸣，我们可以约个短会。\n\n{署名}'),
    ('owner', 'after_quote'): (
        '推进贵司的这次决策',
        '{contact} 您好：\n\n'
        '围绕贵司{证据摘录}（来源：{来源}）的沟通还在进行。'
        '如果决策中有未解的疑虑，请直接告诉我，我来逐项回应。\n\n{署名}'),
    ('technical', 'first_touch'): (
        '技术资料与贵司业务的交集',
        '{contact} 您好：\n\n'
        '我们注意到贵司在{证据摘录}（来源：{来源}）方面的公开信息。'
        '我们有相关的参数资料与样品流程，如果您负责技术评估，可以先发您参考。\n\n{署名}'),
    ('technical', 'follow_up'): (
        '补充技术材料',
        '{contact} 您好：\n\n'
        '在贵司{证据摘录}（来源：{来源}）的基础上，补充几项技术细节。'
        '如需更深一层的规格说明，请告知具体关注点。\n\n{署名}'),
    ('technical', 'after_quote'): (
        '技术评估的未尽事项',
        '{contact} 您好：\n\n'
        '贵司{证据摘录}（来源：{来源}）相关的评估若有技术疑问尚未关闭，'
        '我可以安排工程师直接对接。\n\n{署名}'),
    ('unknown', 'first_touch'): (
        '想先确认贵司的对口同事',
        '您好：\n\n'
        '我们是一家{行业}服务商。看到贵司{证据摘录}（来源：{来源}），'
        '冒昧来信：如果这一事项不归您负责，能否指点对口同事？谢谢。\n\n{署名}'),
    ('unknown', 'follow_up'): (
        '再次来信：希望找到对口同事',
        '您好：\n\n'
        '就贵司{证据摘录}（来源：{来源}）再次来信。'
        '若上封信未送达对口同事，烦请帮忙转达或指点人选。\n\n{署名}'),
    ('unknown', 'after_quote'): (
        '跟进此前的沟通',
        '您好：\n\n'
        '关于贵司{证据摘录}（来源：{来源}）的交流，想确认目前是否有新的进展或变化。\n\n{署名}'),
}

FORBIDDEN_PROMISES = re.compile(
    r'(最便宜|保证.{0,6}(价格|交期|认证)|100%|certainty|guarantee)', re.I)


def _normalize_body(body: str) -> str:
    """12 字重复率基于去空白、小写化的正文。"""
    return re.sub(r'\s+', '', body).casefold()


def ngrams(body: str, size: int = NGRAM) -> set[str]:
    text = _normalize_body(body)
    return {text[index:index + size] for index in range(len(text) - size + 1)}


def _validate(letter: dict) -> tuple[list[str], list[str]]:
    """返回 (missing, errors)。"""
    missing, errors = [], []
    if not letter.get('company_id'):
        errors.append('company_id 必填')
    evidence = letter.get('evidence') or []
    usable = [item for item in evidence
              if isinstance(item, dict) and item.get('source_url') and item.get('excerpt')]
    if not usable:
        missing.append('evidence[].source_url')
        missing.append('evidence[].excerpt')
    role = (letter.get('contact') or {}).get('role') or 'unknown'
    stage = letter.get('stage') or 'first_touch'
    if role not in ROLES:
        errors.append(f'contact.role 必须是 {ROLES} 之一')
    if stage not in STAGES:
        errors.append(f'stage 必须是 {STAGES} 之一')
    return missing, errors


def render_letter(letter: dict, *, sender: str = '') -> dict:
    """渲染单封信。证据不足时返回 insufficient_evidence 状态，绝不编造。"""
    missing, errors = _validate(letter)
    if missing:
        return {'status': 'insufficient_evidence', 'missing': missing,
                'company_id': letter.get('company_id')}
    if errors:
        return {'status': 'invalid_input', 'errors': errors,
                'company_id': letter.get('company_id')}
    evidence = [item for item in letter['evidence']
                if item.get('source_url') and item.get('excerpt')]
    cited = evidence[0]
    role = (letter.get('contact') or {}).get('role') or 'unknown'
    stage = letter.get('stage') or 'first_touch'
    template_subject, template_body = TEMPLATES[(role, stage)]
    contact = (letter.get('contact') or {}).get('name') or '负责人'
    fill = {
        'contact': contact,
        '行业': '相关行业',
        '证据摘录': f'“{str(cited["excerpt"]).strip()}”',
        '来源': str(cited['source_url']).strip(),
        '署名': sender or '（署名占位，发送前请填写）',
    }
    subject = template_subject.format(**fill)
    body = template_body.format(**fill)
    if FORBIDDEN_PROMISES.search(body):
        return {'status': 'invalid_letter', 'errors': ['正文包含价格/交期/认证承诺措辞'],
                'company_id': letter.get('company_id')}
    document = (
        f"# {letter.get('company_name') or letter['company_id']}\n"
        f"- 收件人：{role}\n"
        f"- 阶段：{stage}\n"
        f"- 引用证据：{cited['source_url']} — {str(cited['excerpt']).strip()}\n\n"
        f"## 主题\n{subject}\n\n## 正文\n{body}\n")
    return {'status': 'ok', 'company_id': letter['company_id'], 'role': role, 'stage': stage,
            'document': document, 'cited_evidence': cited}


def _prose_for_uniqueness(document: str) -> str:
    """12 字重复率只针对行文本身，剔除规范强制要求的引用标记
    （来源 URL 与证据摘录——不同信引用同一证据时必然逐字相同）。"""
    body = document.split('## 正文\n', 1)[-1]
    body = re.sub(r'（来源：[^）]*）', '', body)
    body = re.sub(r'“[^”]{12,}”', '', body)
    body = re.sub(r'（署名占位[^）]*）', '', body)  # 占位署名由人填写，不属于行文
    return body


def render_batch(letters: list[dict], *, sender: str = '') -> dict:
    """批量渲染 + 12 字重复率校验（违规整批拒绝）。"""
    rendered, failures = [], []
    for letter in letters:
        result = render_letter(letter, sender=sender)
        if result['status'] == 'ok':
            rendered.append(result)
        else:
            failures.append(result)
    seen: dict[str, str] = {}
    duplicates = []
    for item in rendered:
        for gram in ngrams(_prose_for_uniqueness(item['document'])):
            if gram in seen and seen[gram] != item['company_id']:
                duplicates.append({'companies': sorted({seen[gram], item['company_id']}),
                                   'gram': gram})
            seen.setdefault(gram, item['company_id'])
    if duplicates:
        return {'status': 'duplicate_ngrams', 'duplicates': duplicates[:20],
                'note': f'同批任意两封正文 {NGRAM} 字重复率必须为 0；请改写后重试'}
    return {'status': 'ok', 'letters': rendered, 'failures': failures,
            'count': len(rendered), 'rejected': len(failures)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', required=True, type=Path, help='单封信或 {"letters":[...]} 的 JSON')
    parser.add_argument('--out-dir', required=True, type=Path, help='输出目录 letters/')
    parser.add_argument('--sender', default='', help='署名（可选，默认留占位由人填）')
    parser.add_argument('--check-only', action='store_true', help='只校验不写文件')
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding='utf-8-sig'))
    if isinstance(payload, dict):
        letters = payload.get('letters') or ([payload] if payload else [])
    else:
        letters = payload
    if not isinstance(letters, list) or not letters:
        print(json.dumps({'status': 'invalid_input', 'errors': ['输入必须是信件对象或列表']}))
        return 2
    result = render_batch(letters, sender=args.sender)
    if result['status'] != 'ok' or args.check_only:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 1
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for item in result['letters']:
        path = out_dir / f"{item['company_id']}.md"
        path.write_text(item['document'], encoding='utf-8')
        written.append(str(path))
    print(json.dumps({**result, 'written': written}, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
