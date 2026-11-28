"""Read-only collector for the court's public real-estate auction viewer.

These are website frontend endpoints, NOT a documented OpenAPI contract.
No login, payment, CAPTCHA solver, or identity rotation is implemented.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
from html.parser import HTMLParser
import http.cookiejar
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
BASE = 'https://www.courtauction.go.kr'
KST = timezone(timedelta(hours=9))
LABELS = json.loads((ROOT / 'field_labels.json').read_text(encoding='utf-8'))


class CollectionError(Exception):
    pass


class AccessBlocked(CollectionError):
    pass


def parse_case(value):
    match = re.fullmatch(r'(20\d{2})\s*타경\s*0*([1-9]\d{0,5})', value.strip())
    if not match:
        raise CollectionError('부동산 사건번호를 2024타경12345 형식으로 입력하세요. 동산 사건은 현재 지원하지 않습니다.')
    year, serial = match.groups()
    return {'year': year, 'serial': serial, 'case_number': f'{year}타경{serial}'}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def redact(value):
    """Remove transport credentials from saved responses, retain document facts."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()
                if not re.search(r'orvparam|token|encparam|ecdocid|enrrno', k, re.I)}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def image_extension(raw):
    if raw.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if raw.startswith((b'GIF87a',b'GIF89a')):
        return '.gif'
    if raw.startswith(b'BM'):
        return '.bmp'
    if raw.startswith(b'RIFF') and raw[8:12]==b'WEBP':
        return '.webp'
    raise ValueError('지원 이미지 서명 없음')


def node_executable():
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    return os.environ.get('AUCTION_NODE') or shutil.which('node') or (str(bundled) if bundled.exists() else 'node')


def browser_helper(mode, payload, workdir):
    request_file = workdir / f'helper-{mode}.json'
    write_json(request_file, payload)
    try:
        result = subprocess.run([node_executable(), str(ROOT / 'browser_helper.mjs'), mode, str(request_file)],
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=270)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CollectionError(f'브라우저 실행 실패: {type(error).__name__}') from error
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr, flush=True)
    try:
        response = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as error:
        raise CollectionError('브라우저 수집기의 결과를 읽지 못했습니다. Node/Playwright 설치를 확인하세요.') from error
    if response.get('status') == 'failed' or result.returncode:
        raise CollectionError(response.get('message', '브라우저 수집 실패'))
    return response


class CourtClient:
    def __init__(self, run_dir=None, progress=None):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener.addheaders = [('User-Agent', 'Mozilla/5.0'), ('Referer', BASE + '/pgj/index.on')]
        self.run_dir = run_dir
        self.progress = progress or (lambda message: print(message, file=sys.stderr, flush=True))
        self.calls = []
        self.blocked = False

    def get(self, url, max_bytes=60_000_000):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in {'www.courtauction.go.kr', 'ca.kapanet.or.kr'}:
            raise CollectionError('허용된 공식 문서 호스트가 아닙니다.')
        if self.blocked:
            raise AccessBlocked('접근 제한 이후 추가 요청을 중지했습니다.')
        started = time.monotonic()
        try:
            with self.opener.open(url, timeout=40) as response:
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise CollectionError('문서 크기 제한(60MB)을 초과했습니다.')
                return raw
        finally:
            self.calls.append({'method':'GET', 'path': parsed.hostname + parsed.path,
                               'seconds':round(time.monotonic()-started, 3)})

    def post(self, path, key, payload):
        if self.blocked:
            raise AccessBlocked('접근 제한 이후 추가 요청을 중지했습니다.')
        started = time.monotonic()
        self.progress(path.rsplit('/', 1)[-1] + ' 조회')
        request = urllib.request.Request(BASE + path, data=json.dumps({key: payload}).encode(),
            headers={'Content-Type':'application/json;charset=UTF-8', 'Accept':'application/json'})
        event = {'method':'POST', 'path':path, 'input':redact(payload)}
        try:
            with self.opener.open(request, timeout=40) as response:
                raw = response.read(60_000_001)
            if len(raw)>60_000_000:
                raise CollectionError('조회 응답 크기가 제한을 초과했습니다.')
            obj = json.loads(raw)
            data = obj.get('data')
            if isinstance(data, dict) and data.get('ipcheck') is False:
                self.blocked = True
                raise AccessBlocked('법원에서 접근 제한을 알렸습니다. 우회하지 않고 중지합니다.')
            if obj.get('status') not in (200, '200') or not isinstance(data, dict):
                raise CollectionError('법원 응답 형식 또는 조회 상태가 예상과 다릅니다.')
            event['status'] = 'ok'
            return data
        except urllib.error.HTTPError as error:
            event['status'] = error.code
            if error.code in (403,429):
                self.blocked = True
                raise AccessBlocked(f'법원 HTTP {error.code}: 추가 수집을 중지했습니다.') from error
            raise CollectionError(f'법원 HTTP {error.code}: 조회 실패. 자동 반복하지 않습니다.') from error
        except (ValueError, urllib.error.URLError, TimeoutError) as error:
            event['status'] = type(error).__name__
            raise CollectionError(f'조회 실패: {type(error).__name__}') from error
        finally:
            event['seconds'] = round(time.monotonic()-started, 3)
            self.calls.append(event)


def lookup(court_name, case_number, workdir, court_code=None):
    reference = parse_case(case_number)
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / 'case-response.json'
    browser_helper('lookup', {**reference, 'court_name':court_name, 'output':str(target)}, workdir)
    obj = json.loads(target.read_text(encoding='utf-8'))
    data = obj.get('data') or {}
    if data.get('ipcheck') is False:
        raise AccessBlocked('법원에서 접근을 제한했습니다.')
    if obj.get('status') not in (200,'200'):
        raise CollectionError('법원 사건 조회가 정상 종료되지 않았습니다.')
    base = data.get('dma_csBasInf') or {}
    rows = data.get('dlt_dspslGdsDspslObjctLst') or []
    items = {}
    for row in rows:
        number = row.get('dspslGdsSeq')
        if number is not None:
            items.setdefault(str(number), {'item_number':int(number), 'objects':[]})['objects'].append(redact(row))
    # Never infer the encoded case ID: obtain it from the official response.
    case_id = base.get('csNo') or next((r.get('csNo') for r in rows if r.get('csNo')),None)
    code = base.get('cortOfcCd') or next((r.get('cortOfcCd') for r in rows if r.get('cortOfcCd')),None)
    if court_code and court_code != code:
        raise CollectionError('조회 응답의 법원 코드가 지정한 코드와 다릅니다.')
    write_json(target, redact(obj))
    if not case_id or not code or not items:
        raise CollectionError('해당 법원·사건에서 조회 가능한 물건을 확인하지 못했습니다. 사건 없음과 공개 종료는 이 응답만으로 구별할 수 없습니다.')
    returned = base.get('userCsNo')
    if returned and re.sub(r'\s','',returned) != reference['case_number']:
        raise CollectionError('조회 결과의 사건번호가 입력과 다릅니다.')
    return {**reference, 'court_name':court_name, 'court_code':code, 'case_id':case_id,
            'items':sorted(items.values(),key=lambda i:i['item_number'])}


def validate_pdf(path, expected_case=None):
    from pypdf import PdfReader
    raw = path.read_bytes()
    if not raw.startswith(b'%PDF-'):
        raise CollectionError('PDF 대신 HTML/오류 응답이 반환되었습니다.')
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted and reader.decrypt('') == 0:
            raise CollectionError('암호화 문서를 읽을 수 없습니다.')
        count = len(reader.pages)
        if not count:
            raise CollectionError('빈 PDF입니다.')
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
    except CollectionError:
        raise
    except Exception as error:
        raise CollectionError(f'PDF 구조 검증 실패: {type(error).__name__}') from error
    normalized = re.sub(r'\s','',text)
    return {'pages':count, 'bytes':len(raw), 'sha256':hashlib.sha256(raw).hexdigest(),
            'text_extractable':bool(text.strip()),
            'requested_case_found_in_text': expected_case in normalized if expected_case else None}


class AppraisalFrameParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.src = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'iframe' and attrs.get('id') == 'pdfFrame':
            self.src = attrs.get('src')


def status_html(data, reference, round_number):
    headings = {'dma_curstExmnMngInf':'기본정보·조사내용', 'dlt_ordTsRlet':'부동산 현황 및 점유관계',
                'dlt_ordTsLserLtn':'임대차관계', 'dlt_curstExmnDpcnMrg':'중복·병합사건',
                'dlt_spotExmnOrdCntLst':'조사 명령회차', 'dlt_ordTsPicDvs':'사진 분류별 건수'}
    def esc(value):
        return html.escape(str(value if value is not None else '기재 없음'))
    sections = []
    for key, rows in redact(data).items():
        if key == 'ipcheck':
            continue
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            continue
        tables = []
        for index, row in enumerate(rows,1):
            if not isinstance(row,dict):
                continue
            fields = ''.join(f'<tr><th>{esc(LABELS.get(k,k))}</th><td>{esc(v)}</td></tr>'
                             for k,v in row.items() if v not in (None,'') and k not in {'cortOfcCd','csNo'})
            tables.append(f'<h3>항목 {index}</h3><table>{fields}</table>')
        sections.append(f'<h2>{esc(headings.get(key,key))}</h2>' + (''.join(tables) or '<p>기재 없음</p>'))
    return '''<!doctype html><html lang="ko"><meta charset="utf-8"><title>현황조사서 열람자료 정리본</title>
<style>body{font-family:"Malgun Gothic",sans-serif;font-size:10pt;color:#172637;line-height:1.55}h1{font-size:20pt}h2{font-size:13pt;color:#164b74;border-bottom:2px solid #164b74;padding-top:12px}h3{font-size:10pt}h2,h3{break-after:avoid}table{border-collapse:collapse;width:100%;table-layout:fixed;margin-bottom:12px}th,td{border:1px solid #ccd5df;padding:7px;vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere}th{width:29%;text-align:left;background:#eef3f8}tr{break-inside:avoid}.notice{padding:12px;background:#fff4d8}</style>''' + f'''<h1>현황조사서 · 열람자료 정리본</h1><p>{esc(reference['court_name'])} / {esc(reference['case_number'])} / 명령회차 {round_number}</p>
<p class="notice">법원이 공개한 열람 JSON 데이터를 항목별로 정리한 변환본입니다. 법원 원본 PDF·인증문서가 아닙니다. 사건 전체 자료일 수 있으므로 각 부동산 목록번호를 확인하세요. 임차인 진술과 조사 사실을 구분하여 원문을 확인해야 합니다.</p>''' + ''.join(sections) + '</html>'


def document_window(date, kind, now=None):
    """Informational hint, never treat this alone as proof a document is missing."""
    if not date:
        return '매각기일 미확인'
    try:
        end = datetime.strptime(date,'%Y%m%d').date()
    except ValueError:
        return '매각기일 형식 미확인'
    today = (now or datetime.now(KST)).date()
    start = end - timedelta(days=7 if kind=='specification' else 14)
    if today < start:
        return f'일반 공개 시작 예정일 {start.isoformat()}'
    if today > end:
        return f'일반 공개기간 종료일 {end.isoformat()}'
    return '일반 공개기간 안에 있음; 실제 제공 여부는 조회 결과 확인'


def collect(court_name, case_number, item_number, output=ROOT/'output', court_code=None, progress=None):
    started = time.monotonic()
    reference = parse_case(case_number)
    if not isinstance(item_number,int) or item_number<1:
        raise CollectionError('물건번호는 1 이상의 정수여야 합니다.')
    run_dir = Path(output).resolve()/f"{datetime.now(KST):%Y%m%d-%H%M%S-%f}_{reference['case_number']}_{item_number}"
    run_dir.mkdir(parents=True)
    report = {'started_at':datetime.now(KST).isoformat(), 'request':{'court_name':court_name,**reference,'item_number':item_number},
              'state':'running','documents':{},'photos':[],'run_directory':str(run_dir), 'model_calls':0}
    client = CourtClient(run_dir,progress)
    def notify(message):
        client.progress(message)
    def save():
        report['elapsed_seconds'] = round(time.monotonic()-started,2)
        report['http_calls'] = client.calls
        write_json(run_dir/'manifest.json', report)
    def document(kind, action):
        if client.blocked:
            report['documents'][kind]={'status':'blocked','reason':'접근 제한 후 추가 요청 중지'}
            return
        notify(kind+' 수집')
        try:
            hint=report.get('publication_hints',{}).get(kind,'')
            if '공개 시작 예정일' in hint or '공개기간 종료일' in hint:
                report['documents'][kind]={'status':'unavailable','reason':'법원 안내의 일반 공개기간 밖이므로 문서 요청을 생략했습니다. '+hint}
                save()
                return
            report['documents'][kind]=action()
        except AccessBlocked as error:
            client.blocked=True
            report['documents'][kind]={'status':'blocked','reason':str(error)}
        except Exception as error:
            report['documents'][kind]={'status':'failed','reason':str(error).split('\n')[0][:300]}
        save()
    save()
    try:
        notify('법원·사건·물건 확인')
        reference=lookup(court_name,case_number,run_dir,court_code)
        matching=[i for i in reference['items'] if i['item_number']==item_number]
        if not matching:
            raise CollectionError(f'물건 {item_number}은 조회 결과에 없습니다. 가능한 물건: '+','.join(str(i['item_number']) for i in reference['items']))
        code, case_id=reference['court_code'], reference['case_id']
        data=client.post('/pgj/pgj15B/selectAuctnCsSrchRslt.on','dma_srchGdsDtlSrch',
                         {'cortOfcCd':code,'csNo':case_id,'dspslGdsSeq':str(item_number),'pgmId':'PGJ15BM01'})
        detail=data.get('dma_result') or {}
        info=detail.get('dspslGdsDxdyInfo') or {}
        base=detail.get('csBaseInfo') or {}
        if str(info.get('dspslGdsSeq'))!=str(item_number) or str(info.get('csNo'))!=str(case_id) or info.get('cortOfcCd')!=code:
            raise CollectionError('법원 상세 응답의 사건·물건 식별자가 요청과 다릅니다.')
        if base.get('cortOfcNm') and base['cortOfcNm']!=court_name:
            raise CollectionError('상세 응답의 법원이 요청과 다릅니다.')
        photo_dir=run_dir/'photos';photo_dir.mkdir()
        for index,picture in enumerate(detail.get('csPicLst') or [],1):
            try:
                raw=base64.b64decode(picture.get('picFile',''),validate=True)
                extension=image_extension(raw)
                filename=f'photos/photo-{index:02d}{extension}'
                (run_dir/filename).write_bytes(raw)
                report['photos'].append({'status':'saved','path':filename,'source':'court_detail_response',
                    'original_resolution_verified':False,'classification_code':picture.get('cortAuctnPicDvsCd'),
                    'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
            except (ValueError,TypeError) as error:
                report['photos'].append({'status':'failed','reason':str(error)})
        for picture in detail.get('csPicLst') or []:
            picture.pop('picFile',None)
        write_json(run_dir/'property.json',redact(detail))
        price_fields=['fstPbancLwsDspslPrc','scndPbancLwsDspslPrc','thrdPbancLwsDspslPrc','fothPbancLwsDspslPrc']
        report['property']={'court_name':court_name,'case_number':reference['case_number'],'item_number':item_number,
            'appraisal_amount':info.get('aeeEvlAmt'),'minimum_sale_prices':[{'field':k,'amount':info[k]} for k in price_fields if info.get(k) is not None],
            'failed_auction_count':info.get('flbdNcnt'),'sale_date':info.get('dspslDxdyYmd'),
            'objects':redact(detail.get('gdsDspslObjctLst',[])),'sale_history':redact(detail.get('gdsDspslDxdyLst',[]))}
        report['publication_hints']={k:document_window(info.get('dspslDxdyYmd'),k) for k in ['specification','status_report','appraisal']}
        save()

        def status_report():
            initial=client.post('/pgj/pgj15B/selectCurstExmndc.on','dma_srchCurstExmn',
                         {'cortOfcCd':code,'csNo':case_id,'auctnInfOriginDvsCd':'2','ordTsCnt':''})
            manager=initial.get('dma_curstExmnMngInf') or {}
            if not manager or manager.get('ordTsCnt') is None:
                return {'status':'unavailable','reason':'현황조사 열람자료가 응답에 없습니다. '+report['publication_hints']['status_report']}
            rounds=initial.get('dlt_spotExmnOrdCntLst') or [{'ordTsCnt':manager['ordTsCnt']}]
            artifacts=[]
            for round_info in rounds:
                number=round_info['ordTsCnt']
                body=initial if str(number)==str(manager['ordTsCnt']) else client.post('/pgj/pgj15B/selectCurstExmndc.on','dma_srchCurstExmn',
                    {'cortOfcCd':code,'csNo':case_id,'auctnInfOriginDvsCd':'2','ordTsCnt':str(number)})
                returned=body.get('dma_curstExmnMngInf') or {}
                if str(returned.get('csNo'))!=str(case_id) or str(returned.get('ordTsCnt'))!=str(number):
                    raise CollectionError('현황조사서의 사건 또는 명령회차가 다릅니다.')
                stem=f'status-report-round-{int(number)}'
                write_json(run_dir/(stem+'.json'),redact(body))
                (run_dir/(stem+'.html')).write_text(status_html(body,reference,number),encoding='utf-8')
                entry={'round':number,'json':stem+'.json','html':stem+'.html','representation':'court_view_data_transformed','scope':'case-level; match object numbers'}
                try:
                    browser_helper('render',{'html':str(run_dir/(stem+'.html')),'pdf':str(run_dir/(stem+'.pdf'))},run_dir)
                    entry.update(pdf=stem+'.pdf',**validate_pdf(run_dir/(stem+'.pdf'),reference['case_number']))
                except CollectionError as error:
                    entry['pdf_error']=str(error)
                artifacts.append(entry)
            return {'status':'saved' if all('pdf' in a for a in artifacts) else 'partial','artifacts':artifacts,
                    'note':'원문 열람 JSON + 항목 정리 HTML + PDF 변환본. 법원 원본 PDF가 아닙니다.'}

        def appraisal():
            query={'cortOfcCd':code,'cortSptNm':court_name,'csNo':case_id,'dspslDxdyYmd':info.get('dspslDxdyYmd',''),
                   'auctnInfOriginDvsCd':'4','pgmId':'PGJ15BP03','ordTsCnt':''}
            data=client.post('/pgj/pgj15B/selectAeeWevlInfo.on','dma_srchAeeWevl',query)
            initial=data.get('dma_ordTsIndvdAeeWevlInf') or {}
            if not initial.get('aeeWevlNo'):
                return {'status':'unavailable','reason':'감정평가서 연결정보가 없습니다. '+report['publication_hints']['appraisal']}
            rounds=data.get('dlt_aeeWevlOrdTsLst') or [{'ordTsCnt':initial['ordTsCnt']}]
            artifacts=[]
            for row in rounds:
                number=row['ordTsCnt']
                current=data if str(number)==str(initial.get('ordTsCnt')) else client.post('/pgj/pgj15B/selectAeeWevlInfo.on','dma_srchAeeWevl',{**query,'ordTsCnt':str(number)})
                fields=current.get('dma_ordTsIndvdAeeWevlInf') or {}
                if str(fields.get('csNo'))!=str(case_id) or str(fields.get('ordTsCnt'))!=str(number):
                    raise CollectionError('감정평가서 사건 또는 명령회차가 다릅니다.')
                components=[str(fields[k]) for k in ['cortOfcCd','csNo','ordTsCnt','aeeWevlNo','wrtYmd']]
                components[0]=components[0].removeprefix('B')
                viewer='https://ca.kapanet.or.kr/view/'+'/'.join(urllib.parse.quote(x,safe='') for x in components)
                parser=AppraisalFrameParser();parser.feed(client.get(viewer).decode('utf-8','replace'))
                if not parser.src:
                    raise CollectionError('감정평가 뷰어가 PDF 링크를 제공하지 않습니다. 추가 확인이 필요합니다.')
                pdf_url=urllib.parse.urljoin(viewer,html.unescape(parser.src))
                parsed=urllib.parse.urlparse(pdf_url)
                if parsed.hostname!='ca.kapanet.or.kr' or parsed.path!='/auction/display':
                    raise CollectionError('감정평가 PDF 링크의 출처가 예상과 다릅니다.')
                target=run_dir/f'appraisal-round-{int(number)}.pdf'
                target.write_bytes(client.get(pdf_url))
                validation=validate_pdf(target,reference['case_number'])
                write_json(run_dir/f'appraisal-round-{int(number)}.json',redact(current))
                artifacts.append({'round':number,'pdf':target.name,'representation':'original_pdf',
                                  'viewer':viewer,'identity_basis':'court case and round metadata; linked PDF',**validation})
            return {'status':'saved','artifacts':artifacts}

        def specification():
            target=run_dir/'sale-specification.pdf'
            result=browser_helper('specification',{**reference,'item_number':item_number,'output':str(target)},run_dir)
            if result.get('status')!='saved':
                return {'status':'unavailable','reason':result.get('message','문서가 제공되지 않습니다.'),
                        'publication_hint':report['publication_hints']['specification']}
            return {'status':'saved','artifacts':[{'pdf':target.name,'representation':'original_pdf',**validate_pdf(target,reference['case_number'])}]}

        document('status_report',status_report)
        document('appraisal',appraisal)
        document('specification',specification)
        report['photo_counts']={'provided':len(report['photos']),'saved':sum(p['status']=='saved' for p in report['photos'])}
        docs_complete=all(report['documents'].get(k,{}).get('status')=='saved' for k in ['status_report','appraisal','specification'])
        photos_complete=all(p['status']=='saved' for p in report['photos'])
        report['state']='complete' if docs_complete and photos_complete else 'partial'
        report['termination_reason']='세 문서 유형과 제공된 사진 저장 완료' if report['state']=='complete' else '수집 종료: 미제공·실패·접근 제한은 문서별·사진별 결과 확인'
    except Exception as error:
        report['state']='blocked' if isinstance(error,AccessBlocked) else 'failed'
        report['termination_reason']=str(error).split('\n')[0][:400]
    finally:
        report['finished_at']=datetime.now(KST).isoformat()
        save()
        make_index(run_dir,report)
    return report


def make_index(run_dir, report):
    # Local artifact index only. It does not make a legal or investment judgement.
    summary=html.escape(json.dumps(report,ensure_ascii=False,indent=2))
    links=''.join(f'<li><a href="{urllib.parse.quote(str(p.relative_to(run_dir)).replace(os.sep,"/"))}">{html.escape(str(p.relative_to(run_dir)))}</a></li>'
                  for p in sorted(run_dir.rglob('*')) if p.is_file() and p.name!='index.html')
    (run_dir/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>수집 결과</title><style>body{font-family:Malgun Gothic,sans-serif;margin:32px;max-width:1100px}pre{white-space:pre-wrap;background:#f1f5f9;padding:20px}li{margin:8px}</style><h1>경매 자료 수집 결과</h1><p>원본 문서와 열람자료 변환본은 manifest의 representation으로 구분됩니다. 개인정보가 포함될 수 있으므로 이 폴더를 공개 저장소에 올리지 마세요.</p><ul>'+links+'</ul><pre>'+summary+'</pre>',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description='법원 부동산 경매 자료 수집기')
    parser.add_argument('command',choices=['lookup','collect'])
    parser.add_argument('--court',default='서울중앙지방법원')
    parser.add_argument('--court-code')
    parser.add_argument('--case',required=True,dest='case_number')
    parser.add_argument('--item',type=int,default=1)
    parser.add_argument('--output',type=Path,default=ROOT/'output')
    args=parser.parse_args()
    try:
        if args.command=='lookup':
            work=args.output.resolve()/f'lookup-{datetime.now(KST):%Y%m%d-%H%M%S-%f}'
            result=lookup(args.court,args.case_number,work,args.court_code)
        else:
            result=collect(args.court,args.case_number,args.item,args.output,args.court_code)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result.get('state','complete')=='complete' else 2
    except CollectionError as error:
        print(str(error),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
