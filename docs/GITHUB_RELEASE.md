# GitHub 공개와 배포

공개 저장소 이름은 `pdf2hwpx`, 사용자 제품 이름은 스캔노트입니다. Git에는 구현·시험·공개 문서·라이선스·라이선스가 보존된 HWPX 템플릿만 넣습니다. 입력 문서, 화면, 출력, 협업 자료, API 키, 로컬 런타임과 모델 가중치는 제외합니다.

## 공개 소스 확인

```powershell
python scripts/public_source.py
python scripts/public_source.py --stage
```

이 도구는 명시적 파일 목록, 의심스러운 비밀값·로컬 경로, 바이너리·대형 파일을 확인합니다. 발견 내용을 출력할 때 원문 비밀값을 출력하지 않습니다. `public-staging`은 자동 Git 커밋 대상이 아닙니다. 이미 존재하면 덮어쓰지 않으므로 검토 후 별도로 관리합니다. 원본 폴더에서 무조건 `git add .`를 실행하지 마세요. 커밋 직전에 `python scripts/public_source.py --check-index`로 인덱스의 모든 파일이 공개 목록에 있는지 확인합니다.

## 공개 순서

1. 코드 검토에서 확인한 결함을 수정하고 관련 회귀 시험을 실행합니다.
2. 공개 문서·라이선스·NOTICE와 소스 목록을 점검합니다. 민감한 자료가 이전 커밋에도 없는지 확인합니다.
3. Python 3.12 환경의 공개 CI를 실행합니다. 생략된 원본·모델 시험을 기록합니다.
4. 최종 소스로 Windows 실행 파일을 다시 만들고 설치, OCR, 편집, HWPX/HTML 저장을 확인합니다.
5. 검토한 공개 소스를 커밋하고 GitHub에 게시합니다. 공개 승인과 권한이 있는 계정을 사용합니다.
6. 실행 배포물의 공개는 별도 조건을 충족한 뒤 소스 Git과 분리해 진행합니다. 실제 native libvips의 대응 소스·빌드·재링크 자료가 준비되어야 하며, 현재는 공개 바이너리 자산을 게시하지 않습니다. [제3자 고지와 배포 범위](THIRD_PARTY_LICENSES.md)의 조건을 확인하고 SHA256·소스 커밋·고지를 함께 제공합니다.

GitHub는 일반 Git에서 50 MiB보다 큰 파일에 경고하고 100 MiB보다 큰 파일을 차단합니다. 모델과 오프라인 배포물은 일반 Git에 넣지 않습니다. [GitHub 공식 파일 제한](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)을 기준으로 배포 자산 크기와 해당 계정의 제한을 확인하세요. 저장소 생성·공개 범위는 [GitHub 공식 생성 안내](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)를 따릅니다.

## 한계와 라이선스

업로드 한도는 1 GiB, 페이지 수 한도는 500입니다. 실제 처리는 가용 메모리·디스크와 입력 복잡도에 따라 달라집니다. 작업 메모리 계획은 최대 8 GiB, 1 GiB 여유를 두며 메모리 조회 실패 시 2 GiB의 보수적 기준을 사용합니다. 소형 AI는 자원 계획에 맞을 때만 켭니다. 이는 모든 운영체제·입력의 프로세스 메모리 상한을 보증하는 수치가 아닙니다.

원본 구현에는 Apache-2.0을 적용합니다. 모델·템플릿·런타임·의존 라이브러리에는 각각의 원래 조건이 적용됩니다. 새 바이너리 배포에서 `licenses`와 모델 출처·해시가 빠지지 않았는지 확인하세요. [Apache-2.0 원문](https://www.apache.org/licenses/LICENSE-2.0.txt)은 LICENSE 파일에 보존합니다.
