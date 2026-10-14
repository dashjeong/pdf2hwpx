"""Reproducible, reference-based OCR quality measurements."""
import re
import unicodedata


def distance(a, b):
    if len(a) < len(b):
        a, b = b, a
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        nxt = [i]
        for j, y in enumerate(b, 1):
            nxt.append(min(nxt[-1] + 1, row[j] + 1, row[j - 1] + (x != y)))
        row = nxt
    return row[-1]


def measure(reference, text):
    ref = unicodedata.normalize('NFC', str(reference)).strip()
    pred = unicodedata.normalize('NFC', str(text)).strip()
    if not ref:
        raise ValueError('정답 문장을 입력해 주세요.')
    if len(ref) > 50000 or len(pred) > 50000:
        raise ValueError('품질 검사는 50,000자 이내로 나눠 실행해 주세요.')
    chars = re.sub(r'\s+', '', ref)
    observed = re.sub(r'\s+', '', pred)
    if not chars:
        raise ValueError('공백 외 정답 문장을 입력해 주세요.')
    if len(chars) * len(observed) > 25000000:
        raise ValueError('긴 문서는 페이지별로 나눠 품질 검사해 주세요.')
    ce = distance(chars, observed)
    words, predicted_words = ref.split(), pred.split()
    we = distance(words, predicted_words)
    cer, wer = ce / len(chars), we / len(words)
    return {'cer': cer, 'wer': wer, 'accuracy': max(0, 1 - cer),
            'character_errors': ce, 'reference_characters': len(chars),
            'word_errors': we, 'reference_words': len(words),
            'passes_target': cer <= .05, 'target': .95,
            'normalization': 'NFC, CER 공백 제외 / WER 공백 기준 어절'}


def document_text(document):
    lines = []
    for page in document.get('pages', []):
        for b in sorted(page.get('blocks', []), key=lambda b: (round(b.get('y', 0) / 12), b.get('x', 0))):
            if b.get('type') == 'text':
                lines.append(b.get('text', ''))
            elif b.get('type') == 'table':
                lines.extend(' '.join(map(str, row)) for row in b.get('rows', []))
    return '\n'.join(lines)
