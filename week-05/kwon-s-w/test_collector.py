import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from datetime import datetime
from pypdf import PdfWriter

from collector import (AccessBlocked, CollectionError, CourtClient, KST, collect,
                       document_window, image_extension, parse_case, redact, status_html, validate_pdf)


class CollectorTests(unittest.TestCase):
    def test_image_extension_uses_content_not_court_filename(self):
        # The court calls several GIF files .jpg. Preserve their actual format.
        self.assertEqual(image_extension(b'GIF89a'+b'\x00'*20),'.gif')
        self.assertEqual(image_extension(b'\xff\xd8\xff\xe0'),'.jpg')
        with self.assertRaises(ValueError):
            image_extension(b'<html>upstream error</html>')
    def test_parse_case_normalizes_spaces_and_leading_zeros(self):
        result=parse_case(' 2024 타경 0012345 ')
        self.assertEqual(result,{'year':'2024','serial':'12345','case_number':'2024타경12345'})

    def test_parse_case_rejects_movable_case_and_path(self):
        for value in ['2024본12345','../../2024타경12345','2024타경0','2024타경12345<script>']:
            with self.subTest(value=value),self.assertRaises(CollectionError):
                parse_case(value)

    def test_redact_removes_transport_tokens_recursively(self):
        value={'orvParam':'secret','data':[{'encParam':'secret','enrrno':'private','lesDposDts':'40,000,000'}]}
        self.assertEqual(redact(value),{'data':[{'lesDposDts':'40,000,000'}]})

    def test_status_html_escapes_untrusted_document_text(self):
        result=status_html({'dma_curstExmnMngInf':{'lesDts':'<script>alert(1)</script>'}},
                           {'court_name':'법원','case_number':'2024타경1'},1)
        self.assertNotIn('<script>',result)
        self.assertIn('&lt;script&gt;',result)
        self.assertIn('법원 원본 PDF',result)

    def test_publication_window_differs_by_document(self):
        day=datetime(2026,11,20,tzinfo=KST)
        self.assertIn('공개 시작 예정일',document_window('20261202','specification',day))
        self.assertIn('공개기간 안',document_window('20261202','appraisal',day))

    def test_publication_window_boundary_dates(self):
        for day in [datetime(2026,11,25,tzinfo=KST),datetime(2026,12,2,tzinfo=KST)]:
            self.assertIn('공개기간 안',document_window('20261202','specification',day))
        self.assertIn('공개기간 종료',document_window('20261202','specification',datetime(2026,12,3,tzinfo=KST)))

    def test_pdf_rejects_html_error_disguised_as_pdf(self):
        with TemporaryDirectory() as temporary:
            path=Path(temporary)/'fake.pdf';path.write_bytes(b'<html>Error</html>')
            with self.assertRaises(CollectionError):
                validate_pdf(path)

    def test_pdf_accepts_readable_structure_without_ocr_claim(self):
        with TemporaryDirectory() as temporary:
            path=Path(temporary)/'blank.pdf'
            writer=PdfWriter();writer.add_blank_page(595,842);writer.write(path)
            result=validate_pdf(path,'2024타경12345')
            self.assertEqual(result['pages'],1)
            self.assertFalse(result['text_extractable'])
            self.assertFalse(result['requested_case_found_in_text'])
            self.assertEqual(len(result['sha256']),64)

    def test_access_block_stops_following_requests(self):
        client=CourtClient(progress=lambda _:None)
        payload=io.BytesIO(json.dumps({'status':200,'data':{'ipcheck':False}}).encode())
        with patch.object(client.opener,'open',return_value=payload) as opened:
            with self.assertRaises(AccessBlocked):
                client.post('/test','input',{})
            with self.assertRaises(AccessBlocked):
                client.post('/test','input',{})
            self.assertEqual(opened.call_count,1)

    def test_item_not_in_case_does_not_request_other_property(self):
        with TemporaryDirectory() as temporary,patch('collector.lookup',return_value={
            'court_name':'서울중앙지방법원','case_number':'2024타경12345','case_id':'20240130012345',
            'court_code':'B000210','items':[{'item_number':1}]}),patch.object(CourtClient,'post') as post:
            result=collect('서울중앙지방법원','2024타경12345',999,Path(temporary),progress=lambda _:None)
            self.assertEqual(result['state'],'failed')
            self.assertIn('물건 999',result['termination_reason'])
            post.assert_not_called()

    def test_bad_item_rejected_before_directory_or_network(self):
        with TemporaryDirectory() as temporary,patch('collector.lookup') as lookup:
            with self.assertRaises(CollectionError):
                collect('서울중앙지방법원','2024타경12345',0,Path(temporary))
            lookup.assert_not_called()
            self.assertEqual(list(Path(temporary).iterdir()),[])

    def test_untrusted_pdf_host_rejected_before_network(self):
        with self.assertRaises(CollectionError):
            CourtClient().get('https://example.com/fake.pdf')


if __name__=='__main__':
    unittest.main()
