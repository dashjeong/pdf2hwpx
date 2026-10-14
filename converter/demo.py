"""Ground-truth official memo fixture, explicitly separate from OCR output."""
from pathlib import Path
import copy
import json
from PIL import Image, ImageDraw, ImageFont


def font(size, bold=False):
    system = Path('C:/Windows/Fonts/malgunbd.ttf' if bold else 'C:/Windows/Fonts/malgun.ttf')
    if system.exists():
        return ImageFont.truetype(str(system), size)
    return ImageFont.load_default(size=size)


def create_demo(data_root: Path):
    folder = data_root / 'demo'
    folder.mkdir(parents=True, exist_ok=True)
    width, height = 1240, 1754
    im = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(im)
    blocks = []

    def text(x, y, value, size=27, bold=False):
        f = font(size, bold)
        box = draw.textbbox((x, y), value, font=f)
        draw.text((x, y), value, font=f, fill='#151515')
        blocks.append({'id': f'b{len(blocks)}', 'type': 'text', 'x': x, 'y': y,
                       'width': box[2] - box[0] + 8, 'height': size * 1.5,
                       'font_size': size, 'text': value, 'confidence': None, 'bold': bold})

    text(100, 110, '푸른시 교육지원과', 28, True)
    text(100, 206, '2026년 청소년 디지털 교육 운영 안내', 39, True)
    text(100, 310, '문서번호: 교육지원과-2026-1014')
    text(100, 363, '시행일자: 2026. 10. 14.   담당자: 김하늘')
    text(100, 416, '수신: 관내 중학교장 / 참조: 정보교육 담당교사')
    text(100, 520, '1. 학생들의 디지털 활용 역량을 높이기 위해 다음과 같이')
    text(132, 566, '교육 프로그램을 운영하오니 참가 신청에 협조하여 주시기 바랍니다.')
    text(100, 650, '2. 운영 계획', 30, True)
    rows = [['구분', '내용', '비고'], ['교육 기간', '2026. 11. 2. ~ 11. 20.', '총 6회'],
            ['교육 대상', '중학교 1~3학년 120명', '학교별 20명'],
            ['신청 기한', '2026. 10. 23. 오후 5시', '온라인 접수'],
            ['참가 비용', '0원 (전액 지원)', '교재 제공']]
    xs, ys = [100, 350, 930, 1140], [725 + 90 * i for i in range(6)]
    for x in xs:
        draw.line((x, ys[0], x, ys[-1]), fill='#333333', width=2)
    for y in ys:
        draw.line((xs[0], y, xs[-1], y), fill='#333333', width=2)
    for ri, row in enumerate(rows):
        for ci, value in enumerate(row):
            draw.text((xs[ci] + 16, ys[ri] + 27), value, fill='#111111', font=font(25, ri == 0))
    blocks.append({'id': 'table-1', 'type': 'table', 'x': 100, 'y': 725, 'width': 1040,
                   'height': 450, 'rows': rows, 'font_size': 25, 'confidence': None,
                   'column_widths': [250, 580, 210], 'row_heights': [90] * 5})
    text(100, 1260, '3. 문의: 교육지원과 02-1234-5678')
    text(100, 1320, '붙임  참가 신청서 1부.  끝.')
    text(820, 1500, '푸른시 교육지원과장', 29, True)
    # A document seal exercises illustration preservation.
    draw.rectangle((970, 1570, 1090, 1690), outline='#b94141', width=5)
    draw.text((991, 1585), '교육', font=font(30, True), fill='#b94141')
    draw.text((991, 1629), '지원', font=font(30, True), fill='#b94141')
    im.crop((970, 1570, 1090, 1690)).save(folder / 'seal.png')
    blocks.append({'id': 'seal-1', 'type': 'image', 'x': 970, 'y': 1570, 'width': 120,
                   'height': 120, 'image_url': '/api/assets/demo/seal.png'})
    im.save(folder / 'page-1.png')
    doc = {'id': 'demo', 'name': '공문 편집 연습', 'engine': 'demo', 'pages': [
        {'index': 0, 'width': width, 'height': height, 'dpi': 150,
         'image_url': '/api/assets/demo/page-1.png', 'blocks': blocks}],
        'warnings': ['편집 연습 문서는 정답을 미리 배치한 예제입니다. OCR 성능 측정 결과가 아닙니다.'],
        'quality': {'mean_confidence': None, 'measured': False, 'requires_reference': True}, 'elapsed_seconds': 0}
    return copy.deepcopy(doc)


def prepare_fixtures(root: Path):
    from converter.quality import document_text
    doc = create_demo(root / 'data')
    samples = root / 'samples'
    samples.mkdir(exist_ok=True)
    source = root / 'data' / 'demo' / 'page-1.png'
    with Image.open(source) as im:
        im.save(samples / 'official-memo.png')
        im.save(samples / 'official-memo-scanned.pdf', 'PDF', resolution=150)
        im.rotate(.7, fillcolor='white', resample=Image.Resampling.BICUBIC).save(samples / 'official-memo-skew.png')
        gray = im.convert('L').resize((992, 1403), Image.Resampling.LANCZOS)
        gray.save(samples / 'official-memo-compressed.jpg', quality=58)
    (samples / 'reference.txt').write_text(document_text(doc), encoding='utf-8')
    (samples / 'ground-truth.json').write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    prepare_fixtures(Path(__file__).resolve().parents[1])
