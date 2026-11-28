"""Run Korean OCR on selected local appraisal pages and save only allowed excerpts."""
from hashlib import file_digest
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from pypdf import PdfReader

from collector import ROOT, write_json
from document_excerpts import safe_excerpts


OCR_FILE = 'ocr-excerpts.json'
MAX_PAGES = 8


def read_saved_ocr(directory):
    try:
        data = json.loads((directory / OCR_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def scan_appraisal(directory, pages, progress):
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES or any(type(page) is not int or page < 1 for page in pages):
        raise ValueError(f'쪽 번호를 1~{MAX_PAGES}개 입력하세요.')
    pages = sorted(set(pages))
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    artifacts = manifest.get('documents', {}).get('appraisal', {}).get('artifacts', [])
    filename = next((item['pdf'] for item in artifacts if item.get('pdf')), '')
    path = (directory / filename).resolve()
    if path.parent != directory or path.suffix.lower() != '.pdf' or not path.is_file():
        raise ValueError('저장된 감정평가서 PDF를 찾지 못했습니다.')
    count = len(PdfReader(path).pages)
    if max(pages) > count:
        raise ValueError(f'감정평가서는 {count}쪽입니다. PDF 뷰어의 쪽 번호를 입력하세요.')

    renderer = shutil.which('pdftoppm')
    if os.name != 'nt' or not renderer:
        raise ValueError('로컬 OCR에는 Windows 한국어 OCR과 Poppler(pdftoppm)가 필요합니다. PDF는 원문으로 열 수 있습니다.')
    options = {'capture_output': True, 'check': True, 'creationflags': subprocess.CREATE_NO_WINDOW}
    try:
        with TemporaryDirectory(prefix='auction-ocr-') as temp:
            for page in pages:
                progress(f'감정평가서 {page}쪽 이미지 준비')
                subprocess.run([renderer, '-f', str(page), '-l', str(page), '-singlefile', '-scale-to', '2200',
                                '-png', str(path), str(Path(temp) / f'page-{page}')], timeout=10, **options)
            progress('Windows 한국어 OCR 실행')
            output = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'windows_ocr.py'), temp], timeout=45, **options)
            result = json.loads(output.stdout.decode('utf-8'))
    except subprocess.TimeoutExpired as error:
        raise ValueError('OCR 시간이 초과되었습니다. 쪽 수를 줄여 다시 실행하세요.') from error
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise ValueError('Windows OCR에서 감정평가서를 읽지 못했습니다. 한국어 OCR과 PDF 상태를 확인하세요.') from error
    if result.get('status') == 'dependencies_missing':
        raise ValueError('현재 Python 환경에 로컬 OCR 모듈이 없습니다. README의 OCR 설치를 확인하세요.')
    if result.get('status') != 'ok':
        raise ValueError('이 컴퓨터에서 Windows 한국어 OCR을 사용할 수 없습니다.')

    captured = {str(page): safe_excerpts(result['pages'].get(str(page), '')[:20000]) for page in pages}
    with path.open('rb') as pdf:
        fingerprint = file_digest(pdf, 'sha256').hexdigest()
    saved = read_saved_ocr(directory)
    previous = saved.get(filename)
    merged = previous.get('pages') if isinstance(previous, dict) and previous.get('sha256') == fingerprint else None
    merged = merged if isinstance(merged, dict) else {}
    saved[filename] = {'sha256': fingerprint, 'pages': {**merged, **captured}}
    temporary = directory / (OCR_FILE + '.tmp')
    write_json(temporary, saved)
    temporary.replace(directory / OCR_FILE)
    return {'file': filename, 'pages': captured}
