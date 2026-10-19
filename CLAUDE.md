# MH Wilds Mod Manager 작업 규칙

몬스터 헌터 와일즈 모드 매니저 (Python + tkinter, Windows용 exe 배포). UI 문구와 문서는 한국어로 씁니다.

## 릴리즈: 패치할 때마다 반드시 릴리즈 (사용자 요청)

앱 동작이나 화면이 바뀌는 커밋(기능, 버그 수정, UI 수정)을 할 때마다:

1. `mhwmm/__init__.py`의 `__version__`과 `pyproject.toml`의 `version`을 함께 올린다.
   - 버그 수정·작은 수정: 패치 버전 (1.1.0 → 1.1.1)
   - 새 기능: 마이너 버전 (1.1.1 → 1.2.0)
2. `CHANGELOG.md` 맨 위에 `## vX.Y.Z (YYYY-MM-DD)` 섹션을 한국어로 추가한다. 이 내용이 그대로 릴리즈 노트가 된다.
3. 기본 브랜치에 푸시하면 GitHub Actions `Build` 워크플로가 테스트 → exe 빌드 → 릴리즈 생성까지 자동으로 한다.
   - 클라우드 세션에서는 태그 푸시가 막혀 있으므로 태그를 직접 만들지 않는다. 워크플로가 태그를 만든다.
4. 푸시한 뒤 사용자에게 새 버전의 릴리즈 링크(`https://github.com/skagks/asdijofosjifjsioiofasojif/releases/tag/vX.Y.Z`)를 알려 준다.
   릴리즈가 올라왔는지는 사용자가 직접 확인하므로, 확인 예약(send_later 등)을 걸거나 빌드가 끝나기를 기다리지 않는다.
   사용자가 CI 실패를 알려 주면 고쳐서 다시 푸시한다.

문서나 CI만 바꾸는 커밋은 버전을 올리지 않는다 (이미 릴리즈된 버전이면 워크플로가 릴리즈를 건너뛴다).

## 개발

- 테스트: `python -m pytest` (GUI 테스트는 tkinter와 화면이 있을 때만 실행, 리눅스에서는 `xvfb-run -a python -m pytest`)
- UI 이미지 다시 만들기: `python tools/make_assets.py` (Pillow 필요)
- 구조
  - `mhwmm/analyzer.py`: 압축 파일 구조 분석, 옵션(variant) 찾기
  - `mhwmm/deploy.py`: 게임 폴더 적용·해제, PAK 패치 번호 관리, 원본 백업
  - `mhwmm/manager.py`: 모드 목록·옵션·설정·설치
  - `mhwmm/gui.py`, `mhwmm/theme.py`: tkinter GUI와 테마
  - `mhwmm/cli.py`: 명령줄
