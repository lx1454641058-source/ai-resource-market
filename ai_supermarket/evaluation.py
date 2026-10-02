"""来源：本项目原创。可复现工程检查；不是模型评测、独立盲测或token收益证据。"""

import json
from pathlib import Path
import re
import time
import uuid

from .catalog import Catalog
from .common import compact
from .host import TaskResources
from .service import Market


QUERIES = [
    ('产品分析', 'skill', 'product-analysis'), ('需求拆解', 'skill', 'requirement-slicing'),
    ('来源核对', 'skill', 'evidence-mapping'), ('竞品比较', 'skill', 'competitor-comparison'),
    ('任务成本预算', 'skill', 'cost-accounting'), ('用户访谈', 'skill', 'interview-plan'),
    ('对照实验', 'skill', 'evaluation-plan'), ('故障复盘', 'skill', 'incident-review'),
    ('产品简报', 'template', 'product-brief'), ('PRD需求文档', 'template', 'prd-outline'),
    ('方案比较表', 'template', 'comparison-table'), ('风险登记', 'template', 'risk-register'),
    ('实验登记', 'template', 'experiment-card'), ('来源表', 'template', 'source-log'),
    ('决策记录', 'template', 'decision-log'), ('交接清单', 'template', 'handoff-checklist'),
    ('发布检查', 'template', 'release-checklist'), ('会议纪要', 'template', 'meeting-actions'),
    ('里程碑计划表', 'template', 'milestone-plan'), ('极简反馈', 'template', 'feedback-guide'),
    ('product analysis', 'skill', 'product-analysis'), ('requirements', 'skill', 'requirement-slicing'),
    ('xyzquux不存在的需求', None, None), ('量子陀螺仪设备维修', None, None),
]


def benchmark(market):
    rows = []
    for query, kind, expected in QUERIES:
        start = time.perf_counter()
        reply = market.search(query, kind)
        elapsed = (time.perf_counter() - start) * 1000
        actual = reply['items'][0]['id'] if reply['items'] else None
        rows.append({'query': query, 'expected': expected, 'actual': actual, 'pass': actual == expected,
                     'reply_bytes': len(compact(reply).encode('utf-8')), 'elapsed_ms': round(elapsed, 3)})
    catalog = Catalog(market.catalog_path)
    full_content_bytes = sum(len(catalog.read(item).encode('utf-8')) for item in catalog.items.values())
    return {'scope': 'developer_authored_retrieval_smoke_test', 'model_calls': 0, 'token_savings': None,
            'notice': '工程检查样例公开且参与开发，不是隐藏测试；字节不是token，不证明任务质量收益。',
            'cases': len(rows), 'passed': sum(r['pass'] for r in rows),
            'max_reply_bytes': max(r['reply_bytes'] for r in rows), 'all_resource_body_bytes': full_content_bytes,
            'catalog_revision': catalog.revision, 'results': rows}


def demo(output_root):
    directory = Path(output_root).resolve() / ('run-' + uuid.uuid4().hex[:12])
    directory.mkdir(parents=True, exist_ok=False)
    market = Market(directory / 'market.sqlite', 'demo-host')
    task = TaskResources(market, 'brief-demo', '产品简报', 'template')
    selected = task.candidates['items'][0]
    contract = market.resolve(selected['id'], selected['version'])
    resource = task.use(selected['id'], selected['version'], 'brief-template')
    values = {
        '产品名称': 'AI 超市：本地首版',
        '一句话定位与推荐': '让AI先搜索资源，再按需读取说明和模板。首版使用本地可信目录。',
        '对象、任务、失败及证据；无证据标假设': '直接使用者是AI。资源难发现、重复读取说明是否造成显著浪费，仍待真实任务对照验证。',
        '使用流程与必要功能': '任务前检查 → 搜索摘要 → 查看使用契约 → 获取和读取资源 → 记录已有检查。',
        '本期做、后续做、不做': '本期提供20项原创方法与模板、MCP和本地接口；支付与外部客户端自动触发尚未接通。',
        '合格标准、对照、成本和停止条件': '本例只检查模板已填充、关键章节存在；不证明模型表现、商品效果或token节约。',
        '最多三项影响方案的决定及推荐': '下一步选择实际AI宿主，测量同一任务在有无超市时的质量与总用量。',
    }
    document = re.sub(r'\{\{([^{}]+)\}\}', lambda m: values[m.group(1)], resource['content'])
    passed = '{{' not in document and all(x in document for x in ('## 结论', '## 验证', '## 范围与边界'))
    path = directory / 'product-brief.md'
    with path.open('x', encoding='utf-8', newline='\n') as handle:
        handle.write(document)
    feedback = task.checked(resource['usage_id'], 'brief-template-shape-v1', passed)
    replay = task.checked(resource['usage_id'], 'brief-template-shape-v1', passed)
    report = {'mode': 'deterministic_host_demo', 'model_calls': 0, 'real_payments': False,
              'task_quality_verified': False, 'selected': selected['id'], 'contract': contract,
              'document': str(path), 'shape_check_passed': passed, 'feedback': feedback,
              'duplicate_feedback': replay, 'retrieval': benchmark(market),
              'notice': '普通程序选择首项并填充固定示例，展示实际资料交付和记录；不是自主AI或效果对照实验。'}
    report_path = directory / 'report.json'
    with report_path.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return {'status': 'complete' if passed and report['retrieval']['passed'] == report['retrieval']['cases'] else 'failed',
            'mode': report['mode'], 'document': str(path), 'report': str(report_path),
            'retrieval_cases': report['retrieval']['cases'], 'retrieval_passed': report['retrieval']['passed'],
            'model_calls': 0, 'real_payments': False, 'token_savings': None}

