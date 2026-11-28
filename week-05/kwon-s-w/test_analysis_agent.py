import copy
import errno
from hashlib import sha256
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
import unittest
from unittest.mock import patch

from analysis_agent import AnalysisError, DocumentTools, GeminiModel, SECTIONS, analyze
from document_excerpts import safe_excerpts
from document_review import DOCUMENT_CHECK_LABELS
from server import collection_links


class ScriptedModel:
    """Inject a deterministic tool sequence to test the transport boundary and loop."""

    name = 'test-double'

    def __init__(self, calls):
        self.calls = iter(calls)
        self.seen = []

    def respond(self, history, timeout):
        self.seen.append(copy.deepcopy(history))
        name, args = next(self.calls)
        return {'responseId': f'test-{len(self.seen)}', 'candidates': [{
            'finishReason': 'STOP', 'content': {'role': 'model', 'parts': [{
                'functionCall': {'id': str(len(self.seen)), 'name': name, 'args': args},
                'thoughtSignature': 'same-signature',
            }]},
        }]}


class AnalysisAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        (self.directory / 'manifest.json').write_text(json.dumps({
            'state': 'partial',
            'request': {'court_name': '테스트 법원', 'case_number': '2024타경1', 'item_number': 1},
            'documents': {'specification': {'status': 'saved', 'artifacts': [{'pdf': 'specification.pdf'}]}},
        }), encoding='utf-8')
        self.sample = '경매 물건의 전유면적은 5.10 제곱미터입니다.'
        self.manifest = json.loads((self.directory / 'manifest.json').read_text(encoding='utf-8'))
        pages = [type('Page', (), {'extract_text': lambda _: self.sample})(), None]
        reader = patch('analysis_agent.PdfReader', return_value=type('Reader', (), {'pages': pages})())
        reader.start()
        self.addCleanup(reader.stop)
        self.report = {name: [] for name in SECTIONS}
        self.report['condition'] = [{
            'statement': '전유면적 5.10㎡', 'basis': 'documented',
            'evidence': [{'document_id': 'specification-1', 'page': 1, 'quote': '전유면적은 5.10 제곱미터'}],
            'external_ids': [],
        }]
        self.report['unknowns'] = [{'item': '최신 등기', 'reason': '등기 자료 없음', 'check': '등기사항증명서'}]
        self.report['assessment'] = {
            'scope': 'overall', 'verdict': '판단 보류', 'statement': '등기와 거래 비교가 필요합니다.',
            'evidence': [], 'external_ids': [],
        }

    def test_a_page_match_must_be_read_and_quote_must_match(self):
        tools = DocumentTools(self.directory.resolve(), self.manifest)
        self.assertEqual(tools.search_documents('전유면적')['matches'][0]['page'], 1)
        with self.assertRaises(AnalysisError):
            tools.submit_report(**self.report)
        tools.read_page('specification-1', 1, 1)
        for changed in ({'page': 2}, {'quote': '존재하지 않는 설명 문장입니다.'}):
            forged = copy.deepcopy(self.report)
            forged['condition'][0]['evidence'][0].update(changed)
            with self.assertRaises(AnalysisError):
                tools.submit_report(**forged)
        self.assertEqual(tools.submit_report(**self.report), self.report)

    def test_reviewed_scan_excerpts_are_whitelisted_and_bound_to_the_pdf(self):
        content = b'original scan'
        pdf = self.directory / 'specification.pdf'
        pdf.write_bytes(content)
        (self.directory / 'reviewed-pages.json').write_text(json.dumps({'specification.pdf': {
            'sha256': sha256(content).hexdigest(),
            'pages': {'2': '기준시점 2026.02.13.\nRaw record with a personal name'},
        }}), encoding='utf-8')
        tools = DocumentTools(self.directory.resolve(), self.manifest, excerpts_only=True)
        result = tools.read_page('specification-1', 2, 1)
        self.assertTrue(result['reviewed_page'])
        self.assertEqual(result['text'], '기준시점 2026.02.13.')
        pdf.write_bytes(b'replacement scan')
        tools = DocumentTools(self.directory.resolve(), self.manifest, excerpts_only=True)
        self.assertFalse(tools.read_page('specification-1', 2, 1)['reviewed_page'])

    def test_latest_failure_keeps_completed_analysis_available(self):
        (self.directory / 'analysis-01.json').write_text(json.dumps({'state': 'complete', 'report': self.report}), encoding='utf-8')
        (self.directory / 'analysis-02.json').write_text('{"state":"incomplete"}', encoding='utf-8')
        result = collection_links({}, self.directory)
        self.assertEqual(result['analysis_file'], 'analysis-02.json')
        self.assertEqual(result['completed_analysis_file'], 'analysis-01.json')

    def test_gemini_reports_a_blocked_outbound_connection(self):
        error = URLError(PermissionError(errno.EACCES, 'private operating system details'))
        with patch('analysis_agent.load_local_environment'), patch.dict('os.environ', {'GEMINI_API_KEY': 'test-key'}), \
                patch('analysis_agent.urllib.request.urlopen', side_effect=error):
            with self.assertRaisesRegex(AnalysisError, '나가는 연결이 차단'):
                GeminiModel().respond([], 1)

    def test_loop_feeds_actual_tool_results_back_and_validates_submission(self):
        evidence = self.report['condition'][0]['evidence'][0]
        del evidence['quote']
        evidence['excerpt_id'] = '1-1'
        model = ScriptedModel([
            ('search_documents', {'query': '전유면적'}),
            ('submit_report', self.report),
            ('read_page', {'document_id': 'specification-1', 'page': 1, 'part': 1}),
            ('submit_report', self.report),
        ])
        result = analyze(self.directory, model=model, progress=lambda _: None)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['model_calls'], 4)
        self.assertIn('error', result['tool_calls'][1]['result'])
        forwarded_turn, forwarded_reply = model.seen[-1][-2:]
        self.assertEqual(forwarded_turn['parts'][0]['thoughtSignature'], 'same-signature')
        reply = forwarded_reply['parts'][0]['functionResponse']
        self.assertEqual(reply['id'], '3')
        self.assertEqual(reply['response']['excerpts'], [{'excerpt_id': '1-1', 'text': '전유면적은 5.10 제곱미터'}, {'excerpt_id': '1-2', 'text': '5.10 제곱미터'}])
        self.assertEqual(result['report']['condition'][0]['evidence'][0]['quote'], '전유면적은 5.10 제곱미터')
        self.assertNotIn('quote', evidence)
        self.assertNotIn('2024타경1', json.dumps(model.seen[-1], ensure_ascii=False))
        self.assertEqual(result['read_pages']['specification-1'], [1])
        self.assertIn('2쪽', result['limitations'][-1]['item'])
        self.assertTrue((self.directory / result['file']).is_file())

    def test_document_review_rejects_market_calls_and_requires_the_full_review(self):
        evidence = {'document_id': 'specification-1', 'page': 1, 'excerpt_id': '1-1'}
        self.report['condition'][0]['evidence'] = [evidence]
        self.report['assessment'].update(scope='documents', statement='최신 등기 등 추가 증빙이 필요합니다.')
        checks = {key: {'status': '추가 확인', 'facts': '확보한 발췌에서 기재를 찾지 못했습니다.',
                        'effect': '해당 항목을 추가 확인해야 합니다.', 'next_evidence': '해당 문서 원문',
                        'evidence': [], 'external_ids': []} for key in DOCUMENT_CHECK_LABELS}
        checks['property'].update(status='확인', facts='전유면적 5.10 제곱미터 기재가 있습니다.', evidence=[evidence])
        self.report['document_checks'] = checks
        missing_check = copy.deepcopy(self.report)
        del missing_check['document_checks']['rights']
        model = ScriptedModel([('lookup_sales', {}),
                               ('read_page', {'document_id': 'specification-1', 'page': 1, 'part': 1}),
                               ('submit_report', missing_check), ('submit_report', self.report)])
        with patch('analysis_agent.PublicDataTools.lookup_sales', side_effect=AssertionError('unexpected request')) as sales:
            result = analyze(self.directory, model=model, progress=lambda _: None, document_review=True)
        sales.assert_not_called()
        self.assertEqual(result['state'], 'complete')
        self.assertIn('error', result['tool_calls'][0]['result'])
        self.assertIn('다섯', result['tool_calls'][2]['result']['error'])
        self.assertEqual(result['report']['document_checks']['property']['evidence'][0]['quote'], '전유면적은 5.10 제곱미터')

    def test_document_review_advertises_evidence_checks_without_sales_tool(self):
        response = io.BytesIO(b'{"responseId":"sample"}')
        with patch('analysis_agent.load_local_environment'), patch.dict('os.environ', {'GEMINI_API_KEY': 'test-key'}), \
                patch('analysis_agent.urllib.request.urlopen', return_value=response) as send:
            GeminiModel(document_review=True).respond([], 5)
        payload = json.loads(send.call_args.args[0].data)
        tools = {tool['name']: tool for tool in payload['tools'][0]['functionDeclarations']}
        self.assertNotIn('lookup_sales', tools)
        report = tools['submit_report']['parametersJsonSchema']
        self.assertEqual(report['properties']['document_checks']['required'], list(DOCUMENT_CHECK_LABELS))

    def test_call_limit_does_not_present_unverified_report(self):
        model = ScriptedModel([('search_documents', {'query': '전유면적'})])
        with patch('analysis_agent.MAX_CALLS', 1):
            result = analyze(self.directory, model=model, progress=lambda _: None)
        self.assertEqual(result['state'], 'incomplete')
        self.assertIsNone(result['report'])
        self.assertIn('한도', result['stop_reason'])

    def test_favorable_price_review_requires_successful_external_evidence(self):
        tools = DocumentTools(self.directory.resolve(), self.manifest)
        tools.read_page('specification-1', 1, 1)
        assessment = self.report['assessment']
        assessment.update(scope='price', verdict='검토 가능', evidence=self.report['condition'][0]['evidence'])
        with self.assertRaisesRegex(AnalysisError, '외부 근거'):
            tools.submit_report(**self.report)
        tools.sources['trade-1'] = {'kind': 'trade', 'candidates': []}
        assessment['external_ids'] = ['trade-1']
        with self.assertRaisesRegex(AnalysisError, '거래 후보'):
            tools.submit_report(**self.report)

    def test_categorical_tenant_rights_conclusion_is_rejected(self):
        tools = DocumentTools(self.directory.resolve(), self.manifest)
        tools.read_page('specification-1', 1, 1)
        self.report['assessment']['statement'] = '임차인은 대항력이 없는 것으로 판단됩니다.'
        with self.assertRaisesRegex(AnalysisError, '대항력 결론'):
            tools.submit_report(**self.report)

    def test_commercial_opposability_requirements_need_article_three(self):
        tools = DocumentTools(self.directory.resolve(), self.manifest)
        tools.read_page('specification-1', 1, 1)
        tools.sources.update({
            'law-commercial-8': {'kind': 'law', 'label': '상가건물 임대차보호법 제8조'},
            'law-commercial-3': {'kind': 'law', 'label': '상가건물 임대차보호법 제3조'},
            'law-housing-3': {'kind': 'law', 'label': '주택임대차보호법 제3조'},
        })
        assessment = self.report['assessment']
        assessment['statement'] = '대항력은 건물 인도 및 사업자등록 요건을 갖춘 다음 날부터 발생합니다.'
        for source in ('law-commercial-8', 'law-housing-3'):
            assessment['external_ids'] = [source]
            with self.assertRaisesRegex(AnalysisError, '상가건물 임대차보호법 제3조'):
                tools.submit_report(**self.report)
        assessment['external_ids'] = ['law-commercial-3']
        self.assertEqual(tools.submit_report(**self.report), self.report)

    def test_gemini_posts_native_tool_schema_and_header_key(self):
        response = io.BytesIO(b'{"responseId":"sample"}')
        with patch('analysis_agent.load_local_environment'), patch.dict('os.environ', {
            'GEMINI_API_KEY': 'test-key', 'GEMINI_MODEL': 'gemini-3.1-flash-lite',
        }), patch('analysis_agent.urllib.request.urlopen', return_value=response) as send:
            model = GeminiModel()
            self.assertEqual(model.respond([], 5)['responseId'], 'sample')
        request = send.call_args.args[0]
        payload = json.loads(request.data)
        self.assertTrue(request.full_url.endswith('gemini-3.1-flash-lite:generateContent'))
        self.assertNotIn('test-key', request.full_url)
        self.assertEqual(request.get_header('X-goog-api-key'), 'test-key')
        self.assertEqual(payload['toolConfig']['functionCallingConfig']['mode'], 'ANY')
        self.assertIn('parametersJsonSchema', payload['tools'][0]['functionDeclarations'][0])

    def test_excerpts_keep_expected_facts_without_other_page_text(self):
        sample = ('사건번호 2024타경1, 이름 홍길동, 연락처 010-0000-0000\n'
                  '최선순위 설정 2019.07.30. 근저당권\n'
                  '권리신고 점포 임차인 2023.10.12.~ 40,000,000 1,800,000 2020.10.15. 2024.10.02. 2025.8.8.')
        output = safe_excerpts(sample)
        self.assertIn('2019.07.30. 근저당권', output)
        self.assertIn('40,000,000', output)
        for secret in ('홍길동', '010-0000-0000', '2024타경1'):
            self.assertNotIn(secret, output)
