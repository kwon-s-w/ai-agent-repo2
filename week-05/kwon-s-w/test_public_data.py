import os
from io import BytesIO
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

from public_data import PublicDataTools, article_code, region_hints


class PublicDataTests(unittest.TestCase):
    def test_article_code_accepts_matching_text_and_rejects_conflict(self):
        self.assertEqual(article_code('제3조 000300'), '000300')
        self.assertEqual(article_code('제10조의2'), '001002')
        self.assertEqual(article_code('제3조 000800'), '')

    def test_law_uses_exact_name_and_requested_article(self):
        client = PublicDataTools(lambda: 10)
        listing = ET.fromstring('''<LawSearch>
          <law><법령ID>555555</법령ID><법령명한글>상가건물 임대차보호법 시행령</법령명한글></law>
          <law><법령ID>009276</법령ID><법령명한글>상가건물 임대차보호법</법령명한글></law>
          </LawSearch>''')
        article = ET.fromstring('''<Law>
          <법령명_한글>상가건물 임대차보호법</법령명_한글>
          <조문시행일자>20260512</조문시행일자><조문내용>제8조(경매)</조문내용>
          </Law>''')
        with patch.dict(os.environ, {'LAW_API_OC': 'synthetic'}), patch.object(client, 'get_xml', side_effect=[listing, article]) as get:
            result = client.lookup_law_article('상가건물 임대차보호법', '000800')
            duplicate = client.lookup_law_article('상가건물 임대차보호법', '000800')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['source']['effective_date'], '20260512')
        self.assertEqual(get.call_args.args[1]['ID'], '009276')
        self.assertTrue(duplicate['cached'])
        self.assertNotIn('synthetic', str(result))

    def test_trade_filters_cancellation_and_other_dong_and_converts_currency(self):
        client = PublicDataTools(lambda: 10)
        data = '''<response><header><resultCode>000</resultCode></header><body><totalCount>101</totalCount><items>
          <item><umdNm>신당동</umdNm><buildingAr>5.1</buildingAr><dealAmount>11,500</dealAmount>
          <dealYear>2026</dealYear><dealMonth>1</dealMonth><dealDay>2</dealDay></item>
          <item><umdNm>신당동</umdNm><buildingAr>5.1</buildingAr><dealAmount>99,999</dealAmount>
          <dealYear>2026</dealYear><dealMonth>1</dealMonth><dealDay>2</dealDay><cdealType>O</cdealType></item>
          <item><umdNm>회현동</umdNm><buildingAr>5.1</buildingAr><dealAmount>99,999</dealAmount>
          <dealYear>2026</dealYear><dealMonth>1</dealMonth><dealDay>2</dealDay></item>
          </items></body></response>'''
        with patch.dict(os.environ, {'MOLIT_SERVICE_KEY': 'key%2B'}), patch.object(client, 'get_xml', return_value=ET.fromstring(data)) as get:
            result = client.lookup_sales('commercial', '11140', '202601', '신당동', 3, 7, 1)
        source = result['source']
        self.assertEqual(source['matched_on_page'], 1)
        self.assertTrue(source['more_pages'])
        self.assertEqual(source['candidates'][0]['price_krw'], 115_000_000)
        self.assertEqual(source['candidates'][0]['area_field'], 'buildingAr')
        self.assertEqual(get.call_args.args[1]['serviceKey'], 'key+')

    def test_missing_credentials_prevents_http(self):
        client = PublicDataTools(lambda: 10)
        with patch.dict(os.environ, {'LAW_API_OC': ''}), patch.object(client, 'get_xml') as get:
            result = client.lookup_law_article('상가건물 임대차보호법', '000800')
        self.assertEqual(result['status'], 'missing_credentials')
        self.assertEqual(result['http_steps'], [])
        get.assert_not_called()

    def test_villa_uses_rowhouse_endpoint_and_its_own_building_fields(self):
        client = PublicDataTools(lambda: 10)
        data = '''<response><header><resultCode>000</resultCode></header><body><totalCount>2</totalCount><items>
          <item><umdNm>화곡동</umdNm><excluUseAr>44.5</excluUseAr><dealAmount>21,500</dealAmount>
          <dealYear>2026</dealYear><dealMonth>1</dealMonth><dealDay>2</dealDay><houseType>다세대</houseType>
          <mhouseNm>테스트빌라</mhouseNm><buildYear>2010</buildYear><floor>2</floor></item>
          <item><umdNm>화곡동</umdNm><excluUseAr>44.5</excluUseAr><dealAmount>99,999</dealAmount>
          <dealYear>2026</dealYear><dealMonth>1</dealMonth><dealDay>2</dealDay><cdealDay>26.01.09</cdealDay></item>
          </items></body></response>'''
        with patch.dict(os.environ, {'MOLIT_SERVICE_KEY': 'synthetic'}), patch.object(client, 'get_xml', return_value=ET.fromstring(data)) as get:
            result = client.lookup_sales('rowhouse', '11500', '202601', '화곡동', 40, 50, 1)
        self.assertTrue(get.call_args.args[0].endswith('/RTMSDataSvcRHTrade2/getRTMSDataSvcRHTrade2'))
        self.assertEqual(result['source']['matched_on_page'], 1)
        row = result['source']['candidates'][0]
        self.assertEqual((row['price_krw'], row['building_type'], row['building_name'], row['build_year'], row['floor']),
                         (215_000_000, '다세대', '테스트빌라', '2010', '2'))

    def test_trade_403_reports_gateway_code_without_echoing_body(self):
        client = PublicDataTools(lambda: 10)
        body = b'<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30</returnReasonCode><errMsg>private</errMsg></cmmMsgHeader></OpenAPI_ServiceResponse>'
        error = HTTPError('https://apis.data.go.kr/', 403, 'Forbidden', {}, BytesIO(body))
        with patch.dict(os.environ, {'MOLIT_SERVICE_KEY': 'synthetic'}), patch.object(client.opener, 'open', side_effect=error):
            result = client.lookup_sales('apartment', '11110', '201801', '', 0, 100000, 1)
        self.assertEqual(result['status'], 'authentication_error')
        self.assertEqual(result['http_steps'][0]['gateway_code'], '30')
        self.assertIn('SERVICE_KEY_IS_NOT_REGISTERED_ERROR', result['message'])
        self.assertNotIn('private', str(result))

    def test_region_hint_comes_from_manifest_parcel_number(self):
        manifest = {'property': {'objects': [{'pnuNoCtt': '1114016200102170091', 'adongSdNm': '서울특별시',
                    'adongSggNm': '중구', 'adongEmdNm': '신당동'}]}}
        self.assertEqual(region_hints(manifest)[0]['district_code'], '11140')
