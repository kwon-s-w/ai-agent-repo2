"""Read-only adapters for the official law and transaction APIs."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode
from urllib.request import build_opener, HTTPRedirectHandler, Request
import xml.etree.ElementTree as ET

from collector import KST

LAW_BASE = 'https://www.law.go.kr/DRF/'
LAW_NAMES = ('민사집행법', '상가건물 임대차보호법', '주택임대차보호법')
TRADE_BASE = 'https://apis.data.go.kr/1613000/'
TRADE_SERVICES = {
    'commercial': ('RTMSDataSvcNrgTrade', 'https://www.data.go.kr/data/15126463/openapi.do'),
    'apartment': ('RTMSDataSvcAptTrade', 'https://www.data.go.kr/data/15126469/openapi.do'),
    'rowhouse': ('RTMSDataSvcRHTrade2', 'https://www.data.go.kr/data/15126467/openapi.do'),
}
LAW_GUIDE = 'https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=lsEfYdJoListGuide'


class PublicDataError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    # Credentials are query parameters. Keep them on the configured provider host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def text(node, path):
    return (node.findtext(path) or '').strip()


def compact(value):
    return ''.join(value.split())


def article_code(value):
    """Accept a six-digit code or a matching human-readable article reference."""
    if not isinstance(value, str):
        return ''
    value = value.strip().rstrip(',.;。')
    if re.fullmatch(r'[0-9]{6}', value):
        return value
    match = re.fullmatch(r'제\s*([0-9]{1,4})\s*조(?:\s*의\s*([0-9]{1,2}))?(?:\s+([0-9]{6}))?', value)
    if not match:
        return ''
    number, branch, supplied = match.groups()
    code = f'{int(number):04d}{int(branch or 0):02d}'
    return code if not supplied or supplied == code else ''


def region_hints(manifest):
    hints = {}
    for item in (manifest.get('property') or {}).get('objects', []):
        pnu = str(item.get('pnuNoCtt') or '').strip()
        if item.get('adongSggNm') and re.fullmatch(r'[1-9]\d{18}', pnu):
            district = pnu[:5]
            dong = item.get('adongEmdNm') or ''
            hints[district, dong] = {
                'district_code': district, 'dong': dong,
                'district': f'{item.get("adongSdNm") or ""} {item["adongSggNm"]}'.strip(),
            }
    return list(hints.values())


class PublicDataTools:
    def __init__(self, deadline):
        self.deadline = deadline
        self.sources = {}
        self.law_ids = {}
        self.cache = {}
        self.http_steps = []
        self.opener = build_opener(NoRedirect())

    def lookup_law_article(self, law_name, article):
        payload = {'law_name': law_name, 'article': article_code(article)}
        return self.run('law', payload, self.fetch_law)

    def lookup_sales(self, property_type, district_code, contract_month, dong, area_min, area_max, page):
        payload = {'property_type': property_type, 'district_code': district_code,
                   'contract_month': contract_month, 'dong': dong, 'area_min': area_min,
                   'area_max': area_max, 'page': page}
        return self.run('trade', payload, self.fetch_sales)

    def run(self, kind, payload, fetch):
        cache_key = (kind, tuple(payload.values()))
        if cache_key in self.cache:
            return {**self.cache[cache_key], 'cached': True, 'http_steps': []}
        start = len(self.http_steps)
        config = 'LAW_API_OC' if kind == 'law' else 'MOLIT_SERVICE_KEY'
        try:
            key = os.environ.get(config, '').strip()
            if not key:
                raise PublicDataError('missing_credentials', f'{config}를 설정해야 공식 API를 호출할 수 있습니다.')
            data = fetch(key=key, **payload)
        except PublicDataError as error:
            data = {'status': error.status, 'message': str(error)}
        result = {**data, 'http_steps': self.http_steps[start:]}
        self.cache[cache_key] = result
        return result

    def get_xml(self, endpoint, params):
        remaining = self.deadline()
        if remaining <= 0:
            raise PublicDataError('time_limit', '분석 시간 한도에 도달했습니다.')
        step = {'endpoint': endpoint, 'status': 'network_error'}
        self.http_steps.append(step)
        request = Request(endpoint + '?' + urlencode(params), headers={'Accept': 'application/xml'})
        try:
            with self.opener.open(request, timeout=min(12, remaining)) as response:
                step['status'] = response.status
                data = response.read(4_000_001)
        except HTTPError as error:
            step['status'] = error.code
            if error.code == 403 and endpoint.startswith(TRADE_BASE):
                try:
                    body = error.read(4096)
                    code = text(ET.fromstring(body), './/returnReasonCode') if b'<!' not in body else ''
                except (ET.ParseError, OSError):
                    code = ''
                if code == '30':
                    step['gateway_code'] = code
                    raise PublicDataError('authentication_error',
                                          '공공데이터포털 30 SERVICE_KEY_IS_NOT_REGISTERED_ERROR. 해당 실거래가 서비스의 활용 승인과 일반 인증키를 확인하세요.') from error
            status = 'authentication_error' if error.code in (401, 403) else 'api_error'
            raise PublicDataError(status, f'공식 API가 HTTP {error.code}를 반환했습니다.') from error
        except (URLError, OSError) as error:
            raise PublicDataError('network_error', '공식 API 연결에 실패했습니다.') from error
        try:
            if len(data) > 4_000_000 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
                raise ET.ParseError()
            return ET.fromstring(data)
        except ET.ParseError as error:
            raise PublicDataError('invalid_response', '공식 API 응답을 XML로 해석할 수 없습니다.') from error

    @staticmethod
    def validate_law_xml(root):
        if root.tag == 'Response' and root.find('result') is not None:
            raise PublicDataError('api_error', '법제처가 조회를 거부했습니다. 인증값과 등록 IP·도메인, 신청 상태를 확인하세요.')

    def fetch_law(self, key, law_name, article):
        if law_name not in LAW_NAMES or not re.fullmatch(r'[0-9]{6}', article):
            raise PublicDataError('invalid_input', '지원 법령명과 6자리 조 번호가 필요합니다.')
        law_id = self.law_ids.get(law_name)
        if not law_id:
            root = self.get_xml(LAW_BASE + 'lawSearch.do', {
                'OC': key, 'target': 'eflaw', 'type': 'XML', 'query': law_name, 'display': 20,
            })
            self.validate_law_xml(root)
            law_id = next((
                text(row, '법령ID') for row in root.findall('.//law')
                if compact(text(row, '법령명한글')) == compact(law_name)
            ), '')
            if not re.fullmatch(r'\d{1,6}', law_id):
                raise PublicDataError('no_data', '일치하는 현행 법령 ID를 확인하지 못했습니다.')
            self.law_ids[law_name] = law_id
        root = self.get_xml(LAW_BASE + 'lawService.do', {
            'OC': key, 'target': 'eflawjosub', 'type': 'XML', 'ID': law_id, 'JO': article,
        })
        self.validate_law_xml(root)
        number, branch = int(article[:4]), int(article[4:])
        label = f'제{number}조' + (f'의{branch}' if branch else '')
        pieces = [item.text.strip() for item in root.iter()
                  if item.tag in {'조문내용', '항내용', '호내용', '목내용'} and item.text and item.text.strip()]
        if not pieces:
            raise PublicDataError('no_data', '요청 조문의 본문을 확인하지 못했습니다.')
        actual_name = text(root, './/법령명_한글')
        expected_label = '^' + re.escape(label) + r'(?![0-9의])'
        if compact(actual_name) != compact(law_name) or not re.search(expected_label, compact(pieces[0])):
            raise PublicDataError('invalid_response', '응답 법령 또는 조 번호가 요청과 다릅니다.')
        source_id = f'law-{law_id}-{article}'
        text_body = '\n'.join(pieces)
        source = {'id': source_id, 'kind': 'law', 'label': f'{law_name} {label}',
                  'effective_date': text(root, './/조문시행일자') or text(root, './/시행일자'),
                  'url': 'https://www.law.go.kr/법령/' + quote(compact(law_name)) + '/' + quote(label),
                  'provider': '법제처 국가법령정보센터'}
        self.sources[source_id] = source
        return {'status': 'ok', 'source': source, 'text': text_body[:8500],
                'truncated': len(text_body) > 8500}

    def fetch_sales(self, key, property_type, district_code, contract_month, dong, area_min, area_max, page):
        if property_type not in TRADE_SERVICES or not re.fullmatch(r'[1-9]\d{4}', district_code):
            raise PublicDataError('invalid_input', '지원 물건 유형과 법정동코드 앞 5자리가 필요합니다.')
        if not re.fullmatch(r'\d{6}', contract_month):
            raise PublicDataError('invalid_input', '계약년월은 YYYYMM 형식이어야 합니다.')
        try:
            month = datetime.strptime(contract_month, '%Y%m')
        except ValueError as error:
            raise PublicDataError('invalid_input', '계약년월이 유효하지 않습니다.') from error
        month_key = month.strftime('%Y%m')
        if month_key != contract_month or month_key > datetime.now(KST).strftime('%Y%m'):
            raise PublicDataError('invalid_input', '미래 계약년월은 조회할 수 없습니다.')
        if type(page) is not int or not 1 <= page <= 50 or len(dong) > 24:
            raise PublicDataError('invalid_input', '동 이름과 페이지 번호를 확인하세요.')
        if not (0 <= area_min <= area_max <= 100_000):
            raise PublicDataError('invalid_input', '면적 범위는 0~100,000㎡입니다. 제한 없으면 0과 100000을 입력하세요.')
        service, source_url = TRADE_SERVICES[property_type]
        root = self.get_xml(TRADE_BASE + f'{service}/get{service}', {
            'serviceKey': unquote(key), 'LAWD_CD': district_code, 'DEAL_YMD': contract_month,
            'pageNo': page, 'numOfRows': 100,
        })
        error_code = text(root, './/returnReasonCode')
        if error_code == '04':
            raise PublicDataError('upstream_error', '국토교통부 API 게이트웨이가 04 HTTP_ERROR를 반환했습니다. 공식 서비스 응답을 확인하세요.')
        if error_code:
            raise PublicDataError('api_error', f'국토교통부 API 오류 코드 {error_code[:12]}. 이용 신청과 키 상태를 확인하세요.')
        result_code = text(root, 'header/resultCode')
        if result_code not in ('0', '00', '000'):
            raise PublicDataError('api_error', '국토교통부 API가 정상 수집 코드를 반환하지 않았습니다.')
        total = text(root, 'body/totalCount')
        if root.find('body') is None or not total.isdecimal():
            raise PublicDataError('invalid_response', '국토교통부 거래 목록과 전체 건수를 확인할 수 없습니다.')
        candidates = []
        for row in root.findall('body/items/item'):
            trade = matching_trade(row, property_type, district_code, contract_month, dong, area_min, area_max)
            if trade:
                candidates.append(trade)
        source_id = f'trade-{len([item for item in self.sources.values() if item["kind"] == "trade"]) + 1}'
        source = {'id': source_id, 'kind': 'trade', 'label': f'{district_code} {contract_month} 실거래가',
                  'url': source_url, 'provider': '국토교통부', 'property_type': property_type,
                  'district_code': district_code, 'contract_month': contract_month, 'page': page,
                  'total_count': int(total), 'matched_on_page': len(candidates), 'more_pages': page * 100 < int(total),
                  'candidates': candidates[:10]}
        self.sources[source_id] = source
        message = ('표시 건은 조회 조건에 맞는 거래 후보입니다. 용도·면적 기준·층·거래 조건 차이를 확인하세요.'
                   if candidates else '조회한 페이지에서 동·면적 조건에 맞는 거래 후보를 찾지 못했습니다.')
        return {'status': 'ok' if candidates else 'no_matches', 'source': source, 'message': message}


def matching_trade(row, property_type, district, month, dong, minimum, maximum):
    row_district = text(row, 'sggCd')
    neighborhood = text(row, 'umdNm')
    if (row_district and row_district != district) or (dong and compact(dong) != compact(neighborhood)):
        return None
    cancellation = text(row, 'cdealType')
    if cancellation not in ('', '-') or text(row, 'cdealDay') not in ('', '-'):
        return None
    area_field = 'buildingAr' if property_type == 'commercial' else 'excluUseAr'
    try:
        area = Decimal(text(row, area_field).replace(',', ''))
        amount = int(text(row, 'dealAmount').replace(',', ''))
        day = date(*(int(text(row, key)) for key in ('dealYear', 'dealMonth', 'dealDay')))
        if not area.is_finite() or not (0 < area and minimum <= area <= maximum) or amount <= 0:
            return None
    except (ValueError, InvalidOperation):
        return None
    if day.strftime('%Y%m') != month:
        return None
    trade = {'contract_date': day.isoformat(), 'dong': neighborhood, 'area_m2': float(area),
            'area_field': area_field, 'price_krw': amount * 10_000, 'floor': text(row, 'floor'),
            'lot': text(row, 'jibun'),
            'share_deal': text(row, 'shareDealingType'), 'deal_type': text(row, 'dealingGbn')}
    if property_type == 'rowhouse':
        trade.update(building_type=text(row, 'houseType'), building_name=text(row, 'mhouseNm'),
                     build_year=text(row, 'buildYear'))
    else:
        trade.update(building_use=text(row, 'buildingUse'), building_type=text(row, 'buildingType'),
                     building_name=text(row, 'aptNm'))
    return trade
