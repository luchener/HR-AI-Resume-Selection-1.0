"""Validate model evidence claims against the actual resume before any scoring.

This module assigns no points. Technology relationships supply clues, never a
proficiency certificate. Missing or unverifiable evidence is never a rejection.
"""
from __future__ import annotations
import re
import technology_evidence
import requirement_branches

STATES = {'met', 'not_met', 'manual_review', 'not_mentioned'}
LEVEL_RE = re.compile(r'熟练|精通|扎实|深入|独立|主导|负责|proficien|expert|advanced', re.I)
NEGATIVE_RE = re.compile(r'不具备|不会|不熟悉|未掌握|未(?:取得|获得|通过|达到|满足|持有|掌握|使用|接触)|没有.{0,8}经验|无.{0,8}(?:经验|证书|相关)|cannot|no experience|not proficient', re.I)
LABELS = {'met':'有原文证据支持', 'not_met':'原文明确不符合', 'manual_review':'存在相关线索，待人工确认', 'not_mentioned':'简历未提供足够证据'}


def _quote(value, resume):
    text = str(value or '').strip()
    start = resume.find(text) if len(text) >= 2 else -1
    return {'quote': text, 'start': start, 'end': start + len(text)} if start >= 0 else None


def _resume_blocks(resume: str) -> list[dict]:
    """Consecutive source paragraphs retain exact offsets (including PDF wraps)."""
    blocks = []
    for match in re.finditer(r'[^\n。；;]+(?:\n(?!\s*\n)[^\n。；;]+)*[。；;]?', resume):
        value=match.group().strip()
        if value and not re.search(r'求职岗位|求职意向|期望薪资',value):
            q=_quote(value,resume)
            if q:blocks.append(q)
    return blocks


def judge(requirements: list[dict], claims: list[dict], resume: str, gate_items: list[dict]) -> list[dict]:
    """Return one audited assessment per requirement, including preferred duties."""
    by_id = {str(c.get('id')): c for c in claims if isinstance(c, dict)}
    gates = {str(g.get('id')): g for g in gate_items}
    output = []
    for req in requirements:
        rid, text = str(req.get('id') or ''), str(req.get('text') or '')
        claim = by_id.get(rid, {})
        quotes = claim.get('source_quotes', [])
        if not isinstance(quotes, list): quotes = []
        if not quotes and claim.get('source_quote'):
            quotes = [claim.get('source_quote')]
        evidence = [q for value in quotes[:5] if (q := _quote(value, resume))]
        if not evidence and isinstance(claim.get('evidence'), list):
            evidence = [q for entry in claim['evidence'][:5] for value in [entry.get('quote') if isinstance(entry, dict) else entry] if (q := _quote(value, resume))]
        status = str(claim.get('status') or 'not_mentioned')
        if status not in STATES: status = 'not_mentioned'
        reason = str(claim.get('reason') or '').strip()[:400]
        # Candidate source blocks supplement model omissions; only task-relevant
        # text may support a component, never all same-verb sentences in the CV.
        task_terms = re.findall(r'Agent|智能体|接口|API|评测|问数|RAG|检索|前端|数据语义|数据库|工具调用|规划|对话',text,re.I)
        for block in _resume_blocks(resume):
            if task_terms and any(term.casefold() in block['quote'].casefold() for term in task_terms):
                if block not in evidence:evidence.append(block)
        claim_evidence = list(evidence)
        relation = technology_evidence.match_requirement(text, resume)
        kind = 'direct'
        gate = gates.get(rid)
        if gate and req.get('category') in {'education', 'experience', 'certificate'}:
            # Existing gates have specialized degree/year/certificate comparison.
            # An unsupported model-only fact must not become a deterministic blocker.
            status = gate['status']
            reason = gate.get('resume_basis') or reason
            if not evidence:
                terms = re.findall(r'本科|硕士|博士|大专|专科|\d+\s*年|[A-Za-z][A-Za-z0-9+#.-]{1,}', text)
                for line in resume.splitlines():
                    relevant_degree = req.get('category') == 'education' and re.search(r'本科|硕士|博士|大专|专科|学士|研究生', line)
                    if relevant_degree or any(t.casefold() in line.casefold() for t in terms):
                        q = _quote(line.strip(), resume)
                        if q: evidence.append(q)
                        if len(evidence) >= 3: break
            if status == 'not_met':
                proven = False
                if req.get('category') == 'education':
                    ranks = {'专科':1,'大专':1,'本科':2,'学士':2,'硕士':3,'研究生':3,'博士':4}
                    required = [rank for token,rank in ranks.items() if token in text]
                    actual = [rank for e in evidence for token,rank in ranks.items() if token in e['quote']]
                    proven = bool(required and actual and max(actual) < min(required))
                elif req.get('category') == 'certificate':
                    proven = bool(evidence) and any(re.search(r'未取得|未持有|没有|无|二级|三级', e['quote']) for e in evidence)
                else:
                    # Only a same-context work/experience duration can confirm a
                    # years gate conflict; project duration and calendar dates do not.
                    relevant_years = re.compile(r'(?:累计|具备|拥有|相关|工作|开发|从业|任职|经验).{0,12}\d+\s*年|\d+\s*年.{0,12}(?:经验|工作|开发|从业|任职)')
                    proven = any(relevant_years.search(e['quote']) for e in evidence)
                if not proven:
                    status, reason = 'manual_review', '门槛反向事实未能由原文独立确认，需人工核实'
            elif not evidence:
                status, reason = 'not_mentioned', '未找到可定位的门槛事实，不等于不符合'
            elif req.get('category') == 'education' and re.search(r'计算机.*相关专业|计算机.*软件工程|计算机.*人工智能', text) and any(re.search(r'专业|专\s*业', q['quote']) and re.search(r'计算机|软件工程|人工智能|数据科学',q['quote']) for q in evidence):
                status, reason = 'met', '简历原文专业与岗位列举的相关专业方向一致；真实性另行核验'
            elif req.get('category') == 'education' and re.fullmatch(r'(?:本科|硕士|博士|大专|专科|研究生|学士)(?:及以上|以上)?(?:学历)?[。；;]?', text.strip()):
                ranks = {'专科':1,'大专':1,'本科':2,'学士':2,'硕士':3,'研究生':3,'博士':4}
                required = [rank for token,rank in ranks.items() if token in text]
                actual = [rank for e in evidence for token,rank in ranks.items() if token in e['quote']]
                if required and actual and max(actual) >= min(required):
                    status, reason = 'met', '简历自述学历层级满足本条要求；真实性另行核验'
        elif relation.get('has_technology'):
            matches = relation.get('matches') or []
            evidence = [{'quote':m.get('context', m['quote']), 'start':m.get('context_start', m['start']), 'end':m.get('context_end', m['end'])} for m in matches[:5]]
            negatives = relation.get('explicit_negative') or []
            if negatives and not matches:
                evidence = [{'quote':n.get('context', n['quote']), 'start':n.get('context_start', n['start']), 'end':n.get('context_end', n['end'])} for n in negatives[:5]]
                groups = relation.get('groups') or []
                denied = {n.get('concept') for n in negatives}
                fully_denied = any(set(group).issubset(denied) for group in groups)
                status = 'not_met' if fully_denied else 'manual_review'
                reason = '原文明确否定本条必需条件' if fully_denied else '仅部分可选技术被否定，不能推断整条不符合'
            elif matches:
                # Prefer concrete work/project context over a desired job title.
                concrete = [m for m in matches if not re.search(r'求职岗位|求职意向|期望薪资|目标岗位', m.get('context', ''))]
                if concrete:
                    matches = concrete
                    evidence = [{'quote':m.get('context', m['quote']), 'start':m.get('context_start', m['start']), 'end':m.get('context_end', m['end'])} for m in matches[:5]]
                for quote in claim_evidence:
                    if quote not in evidence:
                        evidence.append(quote)
                related = any(m.get('match_type') == 'related' for m in matches)
                kind = 'related' if related else ('alias' if any(m.get('match_type') == 'alias' for m in matches) else 'direct')
                duty = req.get('category') == 'responsibility' or bool(re.search(r'设计|开发|维护|评测|调研|跟踪|建设|优化|落地|实现', text))
                action_groups = [pattern for pattern in (r'设计', r'开发|实现|搭建|构建', r'维护|运维', r'评测|效果分析', r'调研|跟踪|原型') if re.search(pattern, text)]
                duty_supported = bool(duty and action_groups and all(any(re.search(pattern, e['quote']) for e in evidence) for pattern in action_groups) and claim.get('status') == 'met' and concrete)
                # A level adjective near one technology cannot certify another.
                level_supported = not LEVEL_RE.search(text)
                concrete_concepts = {m.get('concept') for m in concrete if m.get('satisfies')}
                concrete_matched = bool(concrete) and all(concrete_concepts.intersection(group) for group in relation.get('groups', []))
                if related or not concrete_matched or (duty and not duty_supported) or relation.get('conflicting_concepts') or not relation.get('matched') or not level_supported:
                    status, reason = 'manual_review', '原文存在相关技术实践线索，但不能据此确认全部要求或熟练程度'
                elif evidence:
                    status, reason = 'met', '原文直接或同义提及所需技术；仅确认该项基础实践证据'
            else:
                status, reason = 'not_mentioned', '未找到可定位的相关技术证据，不代表候选人不具备'
        else:
            if not evidence:
                status, reason = 'not_mentioned', '未提供可核验的原文证据，不等于不符合'
            elif status == 'not_met' and not any(NEGATIVE_RE.search(e['quote']) for e in evidence):
                status, reason = 'manual_review', '缺少明确反向证据，不能从未体现推断不符合'
            elif status == 'met' and LEVEL_RE.search(text) and not all(any(level in e['quote'] for e in evidence) for level in LEVEL_RE.findall(text)):
                status, reason = 'manual_review', '相关经历已找到，能力程度仍需结合实际任务确认'
            elif status == 'met':
                # A quote's existence alone does not prove semantic entailment.
                terms = re.findall(r'[A-Za-z][A-Za-z0-9+#.-]{1,}|[\u4e00-\u9fff]{2,}', text)
                shared = any(t.casefold() in e['quote'].casefold() for t in terms for e in evidence)
                if not shared:
                    status, reason = 'manual_review', '已定位相关原文，但与本条要求的含义对应仍需确认'
        for pattern in (r'数据语义|业务口径',r'多智能体|多代理|A2A',r'问数|SQL.*生成',r'RAG|检索增强'):
            if status == 'met' and re.search(pattern,text,re.I) and not any(re.search(pattern,q['quote'],re.I) for q in evidence):
                status, reason = 'manual_review', '专项能力只有相关原文，不能用通用开发经历代替'
        components, supported = [], []
        positive_framework = None
        named_frameworks = bool(re.search(r'框架', text) and re.search(r'Dify|LangChain|LlamaIndex|AutoGen',text,re.I))
        if named_frameworks and not re.search(r'自研',text):
            named = re.findall(r'Dify|LangChain|LlamaIndex|AutoGen',text,re.I)
            positive_framework = next((m for m in relation.get('matches',[]) if any(m.get('concept','').casefold()==name.casefold() for name in named)),None)
            if not positive_framework:
                status, reason = 'not_mentioned', '未找到岗位列举框架任一项的原文证据；自研Agent经验不自动等同于指定框架'
                evidence = []
        evidence = [source for quote in evidence for block in _resume_blocks(quote['quote']) if (source := _quote(block['quote'], resume))]
        # Component schema is derived from JD conditions, never from a desired score.
        if relation.get('has_technology'):
            concrete_matches = [m for m in relation.get('matches', []) if m.get('satisfies') and not re.search(r'求职岗位|求职意向|期望薪资|目标岗位', m.get('context', ''))]
            for index, group in enumerate(relation.get('groups', [])):
                cid = f'technology-{index}'
                components.append({'id':cid, 'text':' / '.join(group) + '基础实践'})
                match = next((m for m in concrete_matches if m.get('concept') in group), None)
                if match and not relation.get('conflicting_concepts'):
                    supported.append({'id':cid,'text':components[-1]['text'], 'source_quote':match.get('context', match['quote']), 'kind':match['match_type']})
            if LEVEL_RE.search(text):
                components.append({'id':'proficiency','text':'岗位要求的能力程度'})
            # Domain-specific conditions are not certified by generic Agent/API tokens.
            for cid, pattern in [('multi_agent',r'多智能体|多代理|A2A|协同任务'),('tool_calling',r'多工具调用|工具调用'),('data_semantics',r'数据语义|业务口径|字段映射|指标定义'),('text_to_query',r'自然语言.*查询|SQL\s*自动生成'),('rag',r'RAG|混合型.*检索')]:
                if re.search(pattern,text,re.I):
                    components.append({'id':cid,'text':pattern})
                    quote=next((q for q in evidence if re.search(pattern,q['quote'],re.I)),None)
                    if quote:supported.append({'id':cid,'text':pattern,'source_quote':quote['quote'],'kind':'direct'})
                    elif status == 'met':
                        status, reason = 'manual_review', '存在基础技术线索，但专项职责或经验尚未得到原文确认'
            for index, pattern in enumerate((r'设计', r'开发|实现|搭建|构建', r'维护|运维', r'评测|效果分析', r'调研|跟踪|原型', r'规划|任务编排', r'对话管理', r'结果生成|输出.*报告')):
                if re.search(pattern, text):
                    cid=f'action-{index}'
                    components.append({'id':cid,'text':pattern})
                    # Action evidence must itself mention the relevant technology.
                    quote = next((q for q in evidence if re.search(pattern,q['quote']) and technology_evidence.match_requirement(text,q['quote']).get('matches') and not re.search(r'求职岗位|求职意向',q['quote'])), None)
                    if quote: supported.append({'id':cid,'text':pattern,'source_quote':quote['quote'],'kind':'direct'})
        elif re.search(r'独立.*(?:智能体|Agent).*设计.*上线|独立.*从设计到上线',text,re.I):
            # A complete delivery requirement has several independently verifiable stages.
            for cid,pattern in [('design',r'独立设计|主导.*设计'),('development',r'全栈开发|开发.*(?:Agent|智能|系统)'),('deployment',r'生产环境落地|部署|上线')]:
                components.append({'id':cid,'text':pattern})
                quote=next((q for q in _resume_blocks(resume) if re.search(pattern,q['quote'],re.I) and re.search(r'Agent|智能|Flask|系统',q['quote'],re.I)),None)
                if quote:supported.append({'id':cid,'text':pattern,'source_quote':quote['quote'],'kind':'direct'})
        elif req.get('category') == 'responsibility':
            for index, pattern in enumerate((r'设计',r'开发|实现|构建|搭建',r'维护|稳定调用|排查|运维',r'评测|效果分析',r'样本|数据整理',r'调研|原型|验证')):
                if re.search(pattern,text):
                    cid=f'action-{index}'
                    components.append({'id':cid,'text':pattern})
                    quote=next((q for q in evidence if re.search(pattern,q['quote']) and not re.search(r'求职岗位|求职意向',q['quote'])),None)
                    if quote:supported.append({'id':cid,'text':pattern,'source_quote':quote['quote'],'kind':'direct'})
        elif status == 'met':
            components=[{'id':'requirement','text':text}]
            supported=[{'id':'requirement','text':text,'source_quote':evidence[0]['quote'],'kind':'direct'}] if evidence else []
        # A proficiency claim needs evidence about BOTH level and target skill.
        # Concrete project delivery supports practice even without the adjective.
        if any(c['id']=='proficiency' for c in components):
            levels=LEVEL_RE.findall(text)
            proof=next((q for q in claim_evidence if all(level.casefold() in q['quote'].casefold() for level in levels) and technology_evidence.match_requirement(text,q['quote']).get('matched') and all(m.get('match_type')!='related' for m in technology_evidence.match_requirement(text,q['quote']).get('matches',[])) and not re.search(r'求职岗位|求职意向',q['quote'])),None)
            if proof and len(relation.get('groups', [])) == 1 and not relation.get('conflicting_concepts'):
                supported.append({'id':'proficiency','text':'简历自述能力程度','source_quote':proof['quote'],'kind':'direct'})
        if named_frameworks and not positive_framework and not re.search(r'自研',text):
            status='not_mentioned'
            supported=[]
        if components and supported and status == 'not_mentioned':
            status, reason = 'manual_review', '简历原文支持部分岗位子条件，剩余条件尚未体现'
        if named_frameworks and status == 'not_mentioned':
            components=[{'id':'named_framework_practice','text':'岗位列举框架之一的实践'}]
            supported=[]
        branch_result = requirement_branches.match_practice_alternatives(text, resume)
        if branch_result is not None:
            components=[{'id':'practice-alternative','text':'岗位列举的任一完整实践经验分支'}]
            supported=[]
            evidence=[{k:m[k] for k in ('quote','start','end')} for m in branch_result['matches'] + branch_result['negative_matches']]
            if branch_result['matched'] and not branch_result['conflicting']:
                status,kind,reason='met','direct','原文有岗位列举的一个完整实践分支证据，按任一条件判定'
                supported=[{'id':'practice-alternative','text':branch_result['matches'][0]['branch'],'source_quote':branch_result['matches'][0]['quote'],'kind':'direct'}]
            elif branch_result['conflicting']:
                status,reason='manual_review','同一经验分支存在正负原文冲突，需人工确认'
            else:
                status,reason='not_mentioned','未找到任一完整经验分支的原文支持，不等于候选人不具备'
        negative_quotes = {m['quote'] for m in (branch_result['negative_matches'] if branch_result is not None else [])}
        for quote in evidence:
            quote['polarity'] = 'contradicts' if quote['quote'] in negative_quotes else 'supports' if status == 'met' else 'clue'
        item = {**(gate or {}), **req, 'status':status, 'reason':reason,
                'source_quotes':[e['quote'] for e in evidence], 'evidence':evidence,
                'source_quote':evidence[0]['quote'] if evidence else '',
                'resume_basis': ('；'.join(e['quote'] for e in evidence) + ('；'+reason if reason else '')).strip('；'),
                'evidence_kind':kind, 'evidence_label':LABELS[status],
                'explicit_negative':status == 'not_met' and any(NEGATIVE_RE.search(e['quote']) for e in evidence),
                'required_components':components, 'supported_components':supported,
                'technology_clues':relation if relation.get('has_technology') else None}
        output.append(item)
    return output
