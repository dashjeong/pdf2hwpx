"""Reproducible synthetic Korean memo OCR benchmark, not a real-scan claim."""
from __future__ import annotations

import ctypes
import json
import sys
import time
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def distance(a, b):
    previous = list(range(len(b) + 1))
    for i, first in enumerate(a, 1):
        current = [i]
        for j, second in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (first != second)))
        previous = current
    return previous[-1]


def normalize(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def peak_memory_mb():
    if sys.platform != "win32":
        return None
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return round(counters.PeakWorkingSetSize / 1024 ** 2, 1)
    return None


def make_fixture(folder):
    font_path = Path("C:/Windows/Fonts/malgun.ttf")
    if not font_path.exists():
        raise RuntimeError("Benchmark requires Malgun Gothic font. Set a Korean font path.")
    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path), 25)
    title_font = ImageFont.truetype(str(font_path), 38)
    texts = [
        (80, 70, "문서 변환 품질 검수 보고서", title_font),
        (80, 145, "시행일: 2026년 10월 14일", font),
        (80, 195, "수신: 정보화 담당자", font),
        (80, 245, "제목: 한글 문서와 숫자 인식 시험", font),
        (80, 335, "1. 원본 문서의 글자와 표를 편집 가능한 형태로 변환합니다.", font),
        (80, 390, "2. 개인정보는 외부로 전송하지 않고 PC에서 처리합니다.", font),
        (80, 445, "3. 변환 결과와 원본을 비교하여 오류를 확인해 주세요.", font),
        (80, 500, "4. 예산: 1,234,567원 / 참여 학생: 120명 / 비율: 95.0%", font),
    ]
    for x, y, text, style in texts:
        draw.text((x, y), text, font=style, fill="black")
    xs, ys = [80, 500, 750, 1120], [600, 675, 750, 825, 900]
    for x in xs:
        draw.line((x, ys[0], x, ys[-1]), fill="black", width=2)
    for y in ys:
        draw.line((xs[0], y, xs[-1], y), fill="black", width=2)
    rows = [["항목", "수량", "금액"], ["도서 구입", "25권", "375,000원"],
            ["교육 자료", "120개", "240,000원"], ["합계", "145개", "615,000원"]]
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            draw.text((xs[c] + 20, ys[r] + 20), text, font=font, fill="black")
    # A non-text diagram must survive as a source image crop.
    draw.rectangle((90, 1020, 560, 1320), fill=(222, 235, 251), outline=(45, 75, 110), width=3)
    draw.ellipse((150, 1090, 300, 1240), fill=(76, 134, 190))
    draw.polygon([(350, 1090), (480, 1160), (350, 1240)], fill=(220, 120, 80))
    footer = "검수자: 김민수 / 확인일: 2026.10.14."
    draw.text((80, 1430), footer, font=font, fill="black")
    reference = "\n".join([text for _, _, text, _ in texts] + [" ".join(row) for row in rows] + [footer])
    image.save(folder / "clean.png")
    image.filter(ImageFilter.GaussianBlur(0.7)).save(folder / "blurred.jpg", quality=72)
    (folder / "reference.txt").write_text(reference, encoding="utf-8")
    return reference


def main():
    from converter.ocr import recognize_page
    folder = ROOT / "tests" / "fixtures" / "benchmark"
    folder.mkdir(parents=True, exist_ok=True)
    reference = normalize(make_fixture(folder))
    results = []
    for filename in ("clean.png", "blurred.jpg"):
        started = time.perf_counter()
        result = recognize_page(folder / filename, "local", {})
        fragments = []
        for block in result["blocks"]:
            if block["type"] == "text":
                fragments.append(block["text"])
            elif block["type"] == "table":
                fragments.extend(" ".join(row) for row in block["rows"])
        text = normalize("\n".join(fragments))
        cer = distance(reference, text) / len(reference)
        wer = distance(reference.split(), text.split()) / len(reference.split())
        entry = {"input": filename, "fixture_type": "synthetic printed Korean memo, not real scan",
                 "seconds": round(time.perf_counter() - started, 3), "cer": round(cer, 5),
                 "wer": round(wer, 5), "character_accuracy": round(max(0, 1 - cer), 5),
                 "peak_working_set_mb": peak_memory_mb(), "text": text,
                 "tables": sum(b["type"] == "table" for b in result["blocks"]),
                 "images": sum(b["type"] == "image" for b in result["blocks"])}
        results.append(entry)
        print(json.dumps({k: v for k, v in entry.items() if k != "text"}, ensure_ascii=False))
        (folder / (filename + ".result.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    output = ROOT / "tests" / "fixtures" / "ocr_benchmark.json"
    output.write_text(json.dumps({"reference": reference, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
