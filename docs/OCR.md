# 한국어 인식 엔진

## 기본 오프라인 경로

PDF에 원본 글자 층이 있으면 기본 `local` 방식은 글자·좌표·크기·굵기·색을 직접 추출하고 선·그림 배경을 보존합니다. 이 경로는 OCR 품질 시험과 구분합니다. `force_ocr=True`는 원본 글자 층을 사용하지 않고 이미지 OCR을 실행합니다.

스캔 PDF와 이미지는 PP-OCRv5 한국어 mobile 인식 모델, PP-OCRv4 검출 모델, RapidOCR 1.4.4와 ONNX Runtime CPU를 사용합니다. 모델은 전용 OCR 신경망입니다. 신뢰도는 독립 정답과 비교한 글자 정확도가 아닙니다. NPU는 검증하지 않았습니다.

개발 단계의 `python scripts/prepare_models.py`가 모델을 준비합니다. 한국어 모델 버전·SHA256은 스크립트에 고정되어 있고 검출·방향 모델은 고정된 RapidOCR 패키지에서 복사합니다. 생성 manifest와 원래 라이선스를 배포물에 보존합니다. 실행 단계에서는 모델을 자동 다운로드하지 않습니다.

선이 있는 표의 셀·병합·열 너비를 추정하고 셀 글자를 읽습니다. 기울기 보정과 분할 처리를 거친 위치를 원본 좌표로 되돌립니다. 그림과 직인은 원본에서 잘라 보존합니다. 선 없는 표, 손글씨, 크게 기울어진 사진, 복잡한 도형은 완전 복원을 보장하지 않습니다.

## 소형 AI와 외부 API

내장 소형 AI는 Qwen3-VL 2B와 llama.cpp CPU 런타임으로 글자 최대 2줄을 검수합니다. OCR 배치는 유지하며 제안은 자동 적용하지 않습니다. 숫자·부호 보존과 문자열 유사도를 검사하고 모델 실패 시 기본 OCR 결과를 제공합니다. 자원 계획에 맞을 때만 모델을 시작합니다. 모델 가중치와 런타임은 Git 소스에 포함하지 않습니다.

OpenAI와 Gemini는 사용자 API 키로 페이지 이미지를 해당 제공자에 전송합니다. Ollama는 지정한 비전 모델 서버를 사용합니다. 모델은 이미지 입력을 지원해야 합니다. API가 추정한 좌표와 내용은 원본을 대조해야 하며 생성 confidence를 실제 정확도로 사용하지 않습니다. 연결 검사는 실제 인식 성공과 별개입니다.

## 품질 평가

CER은 정답과 결과의 문자 삽입·삭제·치환을 계산합니다. 본체는 한글 NFC 정규화 후 공백을 제외한 글자 기준을 사용하고 WER은 공백으로 나눈 어절 기준을 사용합니다. 원본 글자 층이 없는 그림 영역에 임의 정답을 만들지 않습니다. 위치 대응 평가, 읽기 순서 평가, 구조·배치 평가는 별도입니다. 제한된 자료의 집계와 제외 범위는 [공개 검증 기록](VALIDATION_PUBLIC.md)에 있습니다.

합성 공문과 블러 예제를 만드는 `scripts/benchmark_ocr.py`는 개발 재현용이며 실제 문서의 정확도를 증명하지 않습니다. 일부 benchmark는 공백을 포함한 CER을 사용하므로 결과를 비교할 때 정규화 정책을 확인하세요.

## 1차 출처와 라이선스

- [RapidOCR](https://github.com/RapidAI/RapidOCR)와 [공식 모델 목록](https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/default_models.yaml)
- [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)와 [Apache-2.0 라이선스](https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE)
- [Qwen3-VL](https://github.com/QwenLM/Qwen3-VL)
- [llama.cpp](https://github.com/ggml-org/llama.cpp)
- [OpenAI 이미지 입력](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [Google generateContent](https://ai.google.dev/api/generate-content)
- [Ollama 시각 입력](https://docs.ollama.com/capabilities/vision)

소스의 Apache-2.0은 모델·템플릿·C++ 런타임·Python 의존 라이브러리의 조건을 대체하지 않습니다. NOTICE와 licenses를 보존하고 새 배포의 라이선스 목록을 다시 확인하세요.