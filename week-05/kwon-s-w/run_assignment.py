"""Run the three classroom scenarios through the real agent, keeping records local."""
import argparse
from datetime import datetime
import json
from pathlib import Path
from uuid import uuid4

from analysis_agent import AnalysisError, analyze
from collector import KST, ROOT, write_json
from local_config import KEY_NAMES, load_local_environment
from public_data import article_code

SCENARIOS = (
    {
        'id': '01-achievable',
        'type': '목표 달성 가능',
        'tool': 'lookup_law_article',
        'article': '000300',
        'question': '이 물건은 상가입니다. 상가건물 임대차보호법 제3조의 현행 대항력 요건과 매각물건명세서의 '
                    '근저당 설정일과 사업자등록 관련 기재 날짜를 대조해 주세요. 두 날짜의 선후와 현재 확인한 내용을 '
                    '조문 및 문서 쪽으로 보여주세요. 요청 범위는 요건과 날짜 대조입니다. 임대차기간 시작일을 실제 인도일로 보지 말고, '
                    '대항요건 구비일을 추정하지 말고 두 기재 날짜만 비교해 주세요. 개별 임차인의 대항력이나 낙찰자 인수 여부는 미확인으로 적어 주세요.',
        'criterion': '현행 제3조 응답과 명세서 1쪽에 근거해 날짜의 선후를 설명하고, 대항력 성립 여부는 기재만으로 단정하지 않았는지 확인합니다.',
    },
    {
        'id': '02-unverifiable',
        'type': '확인할 수 없는 조건',
        'tool': 'lookup_law_article',
        'article': '000800',
        'question': '이 상가 임대차를 현행 상가건물 임대차보호법 제8조와 현황조사서에 비춰 검토해 주세요. '
                    '오늘 실제 점유자가 누구이며 낙찰자가 임차인 보증금 중 정확히 얼마를 인수할지도 확인할 수 있나요? '
                    '문서와 법령으로 확인한 사항, 확인할 수 없는 사항, 추가 자료를 각각 알려주세요. '
                    '현재 점유자와 실제 보증금 인수액은 각각 확인하세요. 배당 결과 등 필요한 증빙이 없으면 인수액을 0원으로 추정하지 말고 '
                    '미확인으로 남겨 주세요. 답변은 조문의 일반 원칙, 조사 기록, 두 미확인 항목 중심으로 짧게 작성해 주세요.',
        'criterion': '현행 제8조와 점유 조사 기록을 연결하고, 현재 점유와 실제 인수액을 미확인으로 명시했는지 확인합니다.',
    },
    {
        'id': '03-different-route',
        'type': '다른 경로 선택',
        'tool': 'lookup_sales',
        'question': '이 상가의 공고 최저매각가격이 비교 거래에 비해 낮은지 살펴봐 주세요. 문서에서 소재지·용도·면적을 확인하고 '
                    '2026년 10월의 같은 지역 상업업무용 매매 거래를 확인해 주세요. 비교 후보를 확보했다면 조건 차이를 '
                    '설명하고, 후보가 부족하거나 서로 다른 거래 범위라면 가격 판단을 보류해 주세요.',
        'criterion': '앞선 권리 질문과 다른 Tool을 골랐는지, 거래의 지역·기간·면적과 비교 한계를 설명했는지 확인합니다.',
    },
)


def authenticated_lookup(result, scenario):
    for step in result.get('tool_calls', []):
        output = step['result']
        if step['tool'] != scenario['tool'] or not output.get('source'):
            continue
        inputs = step['input']
        if step['tool'] == 'lookup_law_article' and (
            inputs.get('law_name') != '상가건물 임대차보호법' or article_code(inputs.get('article')) != scenario['article']
        ):
            continue
        if step['tool'] == 'lookup_sales' and (
            inputs.get('property_type'), inputs.get('contract_month')
        ) != ('commercial', '202610'):
            continue
        if output.get('status') not in ('ok', 'no_matches'):
            continue
        if any(type(http['status']) is int and 200 <= http['status'] < 300 for http in output['http_steps']):
            return True
    return False


def evidence_lines(item, result):
    for citation in item.get('evidence', []):
        document = result['documents'][citation['document_id']]
        quote = ' '.join(citation['quote'].split())
        yield f'  - {document["name"]} {citation["page"]}쪽 · {quote}'
    for source_id in item.get('external_ids', []):
        source = result['external_sources'][source_id]
        yield f'  - [{source["label"]}]({source["url"]}) · {source["provider"]}'


def render_summary(entries, collection):
    lines = [
        '# 경매 문서 분석 과제 실행 기록',
        '', f'수집 자료 폴더 `{collection.name}`',
        '', '이 기록은 로컬 검토용입니다. 원문 인용과 모델 응답에 개인정보가 남을 수 있어 제출 전 확인이 필요합니다.',
    ]
    for entry in entries:
        scenario = entry['scenario']
        lines.extend(['', f'## {scenario["id"]} · {scenario["type"]}', '', scenario['question'], ''])
        if not entry.get('record'):
            lines.append(entry['reason'])
            continue
        result = entry['result']
        expected = '확인' if entry['official_api_response'] else '미확인'
        lines.extend([
            f'- 독립 실행 기록 `{entry["record"]}`',
            f'- 모델 {result["model"]}, 호출 {result["model_calls"]}회, {result["elapsed_seconds"]}초',
            f'- 상태 {result["state"]} · {result["stop_reason"]}',
            f'- 해당 공개 API의 정상 응답 {expected}',
            '', '### 도구 선택과 결과', '',
        ])
        for order, step in enumerate(result['tool_calls'], 1):
            output = dict(step['result'])
            if 'text' in output:
                output['text_characters'] = len(output.pop('text'))
            lines.extend([
                f'{order}. `{step["tool"]}` · 모델 호출 {step["model_call"]} · {step["elapsed_seconds"]}초',
                '   - 입력 `' + json.dumps(step['input'], ensure_ascii=False) + '`',
                '   - 결과 `' + json.dumps(output, ensure_ascii=False) + '`',
            ])
        report = result.get('report')
        if report:
            opinion = report['assessment']
            lines.extend(['', '### 최종 판단과 근거', '',
                          f'**{opinion["verdict"]}** · {opinion["statement"]}', ''])
            lines.extend(evidence_lines(opinion, result))
            for name in ('condition', 'rights', 'advantages', 'risks'):
                for finding in report[name]:
                    lines.append(f'- {finding["statement"]}')
                    lines.extend(evidence_lines(finding, result))
            lines.extend(['', '### 미확인 사항', ''])
            for item in report['unknowns'] + result['limitations']:
                lines.append(f'- **{item["item"]}** · {item["reason"]} · {item["check"]}')
        lines.extend(['', '### 사람의 목표 달성 판정', '',
                      scenario['criterion'], '', '판정 및 근거 작성 대기'])
    return '\n'.join(lines) + '\n'


def run_suite(collection, selected=None):
    directory = ROOT / 'output/assignment-runs' / f'{datetime.now(KST):%Y%m%d-%H%M%S}-{uuid4().hex[:8]}'
    directory.mkdir(parents=True)
    attempts = []
    abort = ''
    summary = directory / 'execution.md'
    for scenario in (item for item in SCENARIOS if not selected or item['id'] in selected):
        entry = {'scenario': scenario}
        attempts.append(entry)
        print(f'\n{scenario["id"]} · {scenario["type"]}', flush=True)
        if abort:
            entry.update(reason=abort)
        else:
            try:
                result = analyze(collection, scenario['question'])
            except (AnalysisError, OSError, ValueError) as error:
                abort = f'실행 준비 단계에서 중지했습니다. {error}'
                entry.update(reason=abort)
            else:
                filename = scenario['id'] + '.json'
                write_json(directory / filename, result)
                entry.update(result=result, record=filename,
                             official_api_response=authenticated_lookup(result, scenario))
                if not result['tool_calls'] and result['stop_reason'].startswith('Gemini API'):
                    abort = '첫 Gemini API 호출 오류로 이후 시나리오를 실행하지 않았습니다.'
        summary.write_text(render_summary(attempts, collection), encoding='utf-8')
    print(f'\n실행 기록 {summary}', flush=True)
    return 0 if all(entry.get('official_api_response') and entry['result']['state'] == 'complete' for entry in attempts) else 2


def main():
    parser = argparse.ArgumentParser(description='과제용 3개 요청을 실제 모델과 공개 API로 검증')
    parser.add_argument('--collection', type=Path, help='manifest.json이 있는 수집 결과 폴더')
    parser.add_argument('--check-config', action='store_true', help='키 설정 여부만 출력하고 네트워크 요청을 보내지 않음')
    parser.add_argument('--scenario', action='append', choices=[item['id'] for item in SCENARIOS],
                        help='지정한 시나리오만 실행. 여러 건은 옵션을 반복하세요.')
    parser.add_argument('--send-to-gemini', action='store_true', help='문서 발췌를 Gemini로 전송하는 실제 실행')
    args = parser.parse_args()
    try:
        missing = load_local_environment()
        if args.check_config or missing:
            for name in KEY_NAMES:
                print(f'{name} {"미설정" if name in missing else "설정됨"}')
            return 2 if missing else 0
        if not args.send_to_gemini:
            parser.error('실제 실행에는 --send-to-gemini를 지정하세요.')
        if args.collection is None:
            parser.error('실제 실행에는 --collection을 지정하세요.')
        return run_suite(args.collection.resolve(), args.scenario)
    except (AnalysisError, OSError, ValueError) as error:
        parser.exit(2, f'{error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
