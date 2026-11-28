"""Private subprocess for local Windows OCR. Output goes to the caller's pipe."""
import asyncio
import json
from pathlib import Path
import sys


async def recognize(directory):
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage import FileAccessMode, StorageFile

    korean = next((language for language in OcrEngine.available_recognizer_languages
                   if language.language_tag.lower().split('-')[0] == 'ko'), None)
    engine = OcrEngine.try_create_from_language(korean) if korean else None
    if engine is None:
        return {'status': 'korean_missing'}

    pages = {}
    for image in directory.glob('page-*.png'):
        file = await StorageFile.get_file_from_path_async(str(image))
        stream = await file.open_async(FileAccessMode.READ)
        try:
            decoder = await BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()
            try:
                result = await engine.recognize_async(bitmap)
                pages[image.stem[5:]] = '\n'.join(line.text for line in result.lines)
            finally:
                bitmap.close()
        finally:
            stream.close()
    return {'status': 'ok', 'pages': pages}


if __name__ == '__main__':
    try:
        response = asyncio.run(recognize(Path(sys.argv[1])))
    except ImportError:
        response = {'status': 'dependencies_missing'}
    print(json.dumps(response, ensure_ascii=False))
