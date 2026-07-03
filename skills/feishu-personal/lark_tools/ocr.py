"""
ocr.py - OCR via macOS Vision framework (pyobjc)
"""

import json
import os
import sys


def cmd_ocr(img_path: str) -> None:
    abs_path = os.path.realpath(img_path)
    if not os.path.exists(abs_path):
        print(json.dumps({'error': f'File not found: {abs_path}'}))
        sys.exit(1)

    if sys.platform != 'darwin':
        print(json.dumps({'error': 'OCR is only supported on macOS (requires Vision framework)'}))
        sys.exit(1)

    try:
        import Quartz
        import Vision
    except ImportError:
        print(json.dumps({'error': 'Missing pyobjc-framework-Vision. Run: pip install pyobjc-framework-Vision'}))
        sys.exit(1)

    # Load image as CGImage
    url = Quartz.CFURLCreateWithFileSystemPath(None, abs_path, Quartz.kCFURLPOSIXPathStyle, False)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if not src:
        print(json.dumps({'error': f'Failed to load image: {abs_path}'}))
        sys.exit(1)
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if not cg_image:
        print(json.dumps({'error': f'Failed to decode image: {abs_path}'}))
        sys.exit(1)

    # Create text recognition request
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(['zh-Hans', 'zh-Hant', 'en-US', 'ja-JP', 'ko-KR'])
    request.setUsesLanguageCorrection_(True)

    # Perform request
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)
    success, error = handler.performRequests_error_([request], None)
    if not success:
        msg = str(error) if error else 'Unknown error'
        print(json.dumps({'error': 'OCR failed', 'message': msg}))
        sys.exit(1)

    # Extract results
    lines = []
    for obs in request.results():
        candidates = obs.topCandidates_(1)
        if candidates:
            lines.append(candidates[0].string())

    print(json.dumps({'text': '\n'.join(lines), 'lines': lines, 'count': len(lines)}))
