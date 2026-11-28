"""Local UI for auction collection and optional document analysis."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
from pathlib import Path
import threading
import urllib.parse
import uuid
import zipfile

from analysis_agent import GeminiModel, analyze
from appraisal_ocr import OCR_FILE, scan_appraisal
from collector import ROOT, collect, lookup, parse_case
from document_review import DOCUMENT_REVIEW_TASK

OUTPUT = ROOT / 'output'
WEB_ASSETS = {
    '/': ('index.html', 'text/html'),
    '/styles.css': ('styles.css', 'text/css'),
    '/app.js': ('app.js', 'text/javascript'),
    '/purchase-review.js': ('purchase-review.js', 'text/javascript'),
}
JOBS = {}
LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=1)


def submit_job(kind, request):
    if kind in ('analyze', 'ocr'):
        run_id = request.get('run_id')
        if not isinstance(run_id, str) or not run_id:
            raise ValueError('분석할 수집 결과를 선택하세요.')
        directory = (OUTPUT / run_id).resolve()
        if directory.parent != OUTPUT.resolve() or directory.name != run_id or not (directory / 'manifest.json').is_file():
            raise ValueError('분석할 수집 결과가 없습니다.')
    if kind == 'analyze':
        if request.get('send_to_gemini') is not True:
            raise ValueError('문서 텍스트 전송을 확인해 주세요.')
        question = request.get('question', '')
        if not isinstance(question, str) or len(question) > 600:
            raise ValueError('분석 질문은 600자 이내로 입력하세요.')
        question = question.strip() or DOCUMENT_REVIEW_TASK
        model = GeminiModel(document_review=True)
    elif kind not in ('analyze', 'ocr'):
        parse_case(request.get('case_number',''))
        court = request.get('court_name','')
        names = {c['name'] for c in json.loads((ROOT/'courts.json').read_text(encoding='utf-8'))}
        if court not in names:
            raise ValueError('목록에서 법원을 선택하세요.')
        item = request.get('item_number',1)
        if not isinstance(item,int) or isinstance(item,bool) or not 1 <= item <= 99999:
            raise ValueError('물건번호가 올바르지 않습니다.')
    with LOCK:
        if any(j['state']=='running' for j in JOBS.values()):
            raise ValueError('진행 중인 조회가 있습니다. 완료 후 다시 요청하세요.')
        job_id = uuid.uuid4().hex
        JOBS[job_id] = {'state':'running','progress':['조회 준비 중']}

    def progress(message):
        with LOCK:
            JOBS[job_id]['progress'].append(message)

    def run():
        try:
            if kind=='lookup':
                result=lookup(court,request['case_number'],OUTPUT/('lookup-'+job_id))
            elif kind=='analyze':
                result=analyze(directory, question, model=model, progress=progress, document_review=True)
            elif kind=='ocr':
                result=scan_appraisal(directory, request.get('pages'), progress)
            else:
                result=collect(court,request['case_number'],item,OUTPUT,progress=progress)
            with LOCK:
                JOBS[job_id].update(state='finished',result=result)
        except Exception as error:
            with LOCK:
                JOBS[job_id].update(state='failed',error=str(error).split('\n')[0][:300])
    POOL.submit(run)
    return job_id


def collection_links(report, directory):
    name = urllib.parse.quote(directory.name, safe='')
    report['download_base'] = '/files/' + name + '/'
    report['zip_url'] = '/zip/' + name
    report['run_id'] = directory.name
    reports = sorted(directory.glob('analysis-*.json'), reverse=True)
    if reports:
        report['analysis_file'] = reports[0].name
        for candidate in reports:
            try:
                saved = json.loads(candidate.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if isinstance(saved, dict) and saved.get('state') == 'complete' and saved.get('report'):
                report['completed_analysis_file'] = candidate.name
                break
    if (directory / OCR_FILE).is_file():
        report['ocr_file'] = OCR_FILE
    if (directory / 'purchase-review-example.json').is_file():
        report['purchase_example'] = 'purchase-review-example.json'
    return report


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send(self, body, code=200, content_type='application/json; charset=utf-8', headers=None):
        if not isinstance(body,bytes):
            body=json.dumps(body,ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        for key,value in (headers or {}).items():
            self.send_header(key,value)
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}

    def do_GET(self):
        if not self.valid_host():
            return self.send({'error':'localhost 전용입니다.'},403)
        path=urllib.parse.urlparse(self.path).path
        if path in WEB_ASSETS:
            filename, mime = WEB_ASSETS[path]
            return self.send((ROOT / 'web' / filename).read_bytes(), content_type=f'{mime}; charset=utf-8')
        if path=='/api/courts':
            return self.send(json.loads((ROOT/'courts.json').read_text(encoding='utf-8')))
        if path=='/api/latest':
            files=sorted(OUTPUT.glob('*/manifest.json'),reverse=True)
            if not files:
                return self.send(None)
            report=json.loads(files[0].read_text(encoding='utf-8'))
            return self.send(collection_links(report, files[0].parent))
        if path.startswith('/api/jobs/'):
            with LOCK:
                job=JOBS.get(path.rsplit('/',1)[-1])
                if not job:
                    return self.send({'error':'작업을 찾을 수 없습니다.'},404)
                result=json.loads(json.dumps(job))
            report=result.get('result') or {}
            if 'run_directory' in report:
                collection_links(report, Path(report['run_directory']))
            return self.send(result)
        if path.startswith('/files/'):
            try:
                target=(OUTPUT/urllib.parse.unquote(path[len('/files/'):])).resolve()
                if not target.is_relative_to(OUTPUT.resolve()) or not target.is_file():
                    raise ValueError()
                if target.name.startswith('helper-') or target.name.startswith('browser-diagnostic') or target.name=='save-network.json':
                    raise ValueError()
                mime=mimetypes.guess_type(target)[0] or 'application/octet-stream'
                return self.send(target.read_bytes(),content_type=mime)
            except (ValueError,OSError):
                return self.send({'error':'파일을 찾을 수 없습니다.'},404)
        if path.startswith('/zip/'):
            name=urllib.parse.unquote(path[len('/zip/'):])
            directory=(OUTPUT/name).resolve()
            if directory.parent!=OUTPUT.resolve() or not (directory/'manifest.json').is_file():
                return self.send({'error':'수집 결과가 없습니다.'},404)
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as archive:
                for file in directory.rglob('*'):
                    if file.is_file() and not file.name.startswith(('helper-','browser-diagnostic')) and file.name!='save-network.json':
                        archive.write(file,str(file.relative_to(directory)))
            return self.send(stream.getvalue(),content_type='application/zip',headers={'Content-Disposition':'attachment; filename="auction-documents.zip"'})
        self.send({'error':'경로를 찾을 수 없습니다.'},404)

    def do_POST(self):
        origin=self.headers.get('Origin')
        expected={f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'}
        if not self.valid_host() or (origin and origin not in expected) or self.headers.get('X-Auction-Client')!='local-ui':
            return self.send({'error':'로컬 화면에서 요청하세요.'},403)
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':
            return self.send({'error':'JSON 입력이 필요합니다.'},415)
        if self.path not in ['/api/lookup','/api/collect','/api/analyze','/api/ocr']:
            return self.send({'error':'경로를 찾을 수 없습니다.'},404)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 8192:
                raise ValueError('입력 길이가 올바르지 않습니다.')
            data=json.loads(self.rfile.read(length))
            job=submit_job(self.path.rsplit('/',1)[-1],data)
            self.send({'job_id':job},202)
        except Exception as error:
            self.send({'error':str(error)},400)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'경매 자료 수집기: http://127.0.0.1:{args.port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__=='__main__':
    main()
