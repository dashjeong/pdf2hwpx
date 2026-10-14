# 제3자 구성요소 고지와 배포 범위

프로그램 소스의 라이선스는 함께 사용되는 Python 패키지, 네이티브 라이브러리와 모델의 라이선스를 바꾸지 않습니다. `licenses/`의 원문 고지를 유지합니다. 이 문서는 2026-10-14의 실제 설치 환경을 조사한 결과이며 새 배포판의 파일 해시·로딩 검증은 별도로 수행해야 합니다.

## Python 구성요소

[DEPENDENCIES.json](../licenses/DEPENDENCIES.json)은 빌드에 사용한 Python 환경의 설치 패키지 이름·버전·메타데이터 라이선스와 복사한 고지 경로입니다. 모든 항목이 최종 실행 파일에 포함된다는 의미는 아닙니다. 직접 런타임 요구사항, 빌드 도구와 나머지 설치 의존성을 구분합니다. 이전에 복사한 고지를 삭제하지 않고 현재 dist-info의 LICENSE/COPYING/NOTICE/COPYRIGHT 파일을 추가했습니다.

추가 의존성은 pyvips 3.2.0(MIT), pyvips-binary 8.18.7(네이티브 라이브러리 LGPL-3.0-or-later), cffi 2.1.1(MIT-0), pycparser 3.11(BSD-3-Clause)입니다. 실제 설치된 OpenCV는 opencv-python 4.11.0.86입니다. 각각 `licenses/python/`에 원문 고지가 있습니다.

## 실제 libvips Windows 바이너리

**pyvips-binary의 dist-info에 있는 MIT LICENSE는 빌드 스크립트의 고지입니다. 실제 DLL 전체를 MIT라고 표시하면 안 됩니다.** 해당 패키지 METADATA는 LGPL-3.0-or-later를 표시하며 upstream 네이티브 구성요소 고지를 함께 사용합니다.

실제로 설치된 DLL은 19,018,240바이트이고 SHA-256은 `06dab07cc386748513337a31672b1d5269fbc770a14ff403080575665bf0813f`입니다. 공식 Windows static 배포본의 `vips-dev-8.18/lib/libvips-42.dll`과 바이트가 일치했습니다. 이 DLL 안에는 정적으로 연결된 네이티브 구성요소가 있으며 관찰한 외부 imports는 Windows/UCRT 시스템 라이브러리입니다. 배포판 내부 파일을 단순 Python 래퍼로 취급하지 않습니다.

[출처·해시·구성요소 인벤토리](../licenses/native/libvips/SOURCE_PROVENANCE.json)는 검증한 archive URL/해시, 28개 버전 항목과 libvips에 포함된 libnsgif, 각 고지의 경로 및 원본 소스 URL을 기록합니다. [원문 THIRD-PARTY-NOTICES](../licenses/native/libvips/THIRD-PARTY-NOTICES.md)와 [원문 versions.json](../licenses/native/libvips/versions.json)은 배포 archive에서 추출했습니다. upstream Windows LICENSE, LGPL 2.1 및 LGPL/GPL 3.0 원문도 보존합니다. 이 목록은 upstream이 선언한 네이티브 구성요소 목록이며, DLL에 정적으로 들어간 모든 Rust/C/C++ 전이적 코드의 독립 전수 감사가 아닙니다.

다른 라이선스 계열은 MIT, BSD, FreeType FTL, fontconfig, libpng, libtiff, Mozilla Public License 1.1 및 AOM 특허 라이선스입니다. 각 구성요소의 원문을 `licenses/native/libvips/components/`에 수집했습니다. LGPL로 사용되는 구성요소는 upstream의 “any later version” 선택에 따른 LGPL 3.0 원문과 GPL 3.0 원문을 함께 포함합니다. 원문에 있는 저작권·고지·라이선스 선택 문구를 프로그램의 라이선스로 대체하지 않습니다.

AOM·Fontconfig·TIFF의 원문은 정확한 버전의 공식 소스 archive에서 읽었습니다. Cairo/Pixman archive 서버의 TLS 연결 실패 후 해당 버전의 공식 GitLab 고지를 확인했습니다. GitHub 구성요소는 해당 버전 tag/commit의 고지를 확인했습니다. 별도 버전 없는 libnsgif는 libvips v8.18.7에 실제 포함된 고지와 대응시켰습니다.

## 소스 공개와 바이너리 공개

현재 공개 범위는 **프로그램 소스 저장소와 의존성 고지**입니다. 이 조사로 네이티브 바이너리를 공개 릴리스 자산으로 업로드하는 단계까지 완료됐다고 표시하지 않습니다.

공개 바이너리 재배포 전에는 실제 DLL에 대응하는 모든 네이티브 소스, 적용된 수정과 빌드 자료, LGPL 결합 라이브러리의 재링크/교체 조건을 검증해야 합니다. 버전 URL과 라이선스 파일을 나열한 것만으로 해당 검증을 대신하지 않습니다. upstream 소스 전체와 재현 가능한 빌드/재링크 자료가 준비되기 전에는 GitHub에 네이티브 바이너리 릴리스 자산을 게시하지 않습니다. 로컬 검증용 오프라인 패키지와 공개 소스 저장소의 범위를 구분합니다.

이 배포 경로의 근거는 [pyvips-binary](https://github.com/libvips/pyvips-binary), [libvips-packaging v8.18.7 Windows recipe](https://github.com/kleisauke/libvips-packaging/blob/v8.18.7/build/win.sh), [공식 Windows static release](https://github.com/libvips/build-win64-mxe/releases/tag/v8.18.7) 및 [upstream native notices](https://github.com/kleisauke/libvips-packaging/blob/v8.18.7/THIRD-PARTY-NOTICES.md)입니다. 조건의 원문은 저장한 LGPL/GPL 파일을 확인합니다.

기존 Python, PDFium, ONNX Runtime, OpenCV, MSVC 런타임, llama.cpp, Qwen 모델 및 HWPX 템플릿 고지도 계속 포함합니다. 새 목록은 기존 원문 고지의 삭제 근거가 아닙니다.
