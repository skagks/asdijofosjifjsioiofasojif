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

- 테스트: `python -m pytest` (Pillow가 없으면 그림 관련 테스트는 건너뜀. GUI 테스트는 tkinter와 화면이 있을 때만 실행, 리눅스에서는 `xvfb-run -a python -m pytest`)
- UI 이미지 다시 만들기: `python tools/make_assets.py` (Pillow 필요)
- 구조
  - `mhwmm/analyzer.py`: 압축 파일 구조 분석, 옵션(variant) 찾기
  - `mhwmm/deploy.py`: 게임 폴더 적용·해제, PAK 모드 pak_mods 배치, 원본 백업, 사용자가 바꾼 모드 파일 보관(`edited\`)
  - `mhwmm/manager.py`: 모드 목록·옵션·설정·설치(옵션별 미리보기 그림은 `mods/<id>/previews/<옵션 id>.<확장자>`),
    재질 점검·복구 기록(`mods/<id>/matfix.json`, 고친 파일은 `matfix/`)
  - `mhwmm/matfix/`: 재질(.mdf2) 복구 (MHWmodfixer 기반, MIT). `pak.py` 게임 pak 읽기·모드 pak 쓰기, `game.py` 게임 파일 색인,
    `mdf2.py` 재질 파일, `donors.py` 현재 재질 찾기, `repair.py` 점검·복구, `rsz.py` .user/.pfb/.scn 복구
    (타입 정보 `rsz_types.json.xz`는 `tools/make_rsz_types.py`로 REasy 덤프 기록에서 만듦). 테스트에 zstandard 필요
    - 점검·복구 결과가 달라질 수 있는 변경을 하면 `manager.MATFIX_FORMAT`을 올린다. 그래야 업데이트 후 기존 기록이 '점검 전'이
      되어 다시 점검하고 복구를 다시 묻는다 (올리지 않으면 예전 결과가 그대로 남음).
  - `mhwmm/equip/`: 장비 외형. `model.py` 공통 정의(Target 키 형식), `paths.py` 게임 경로 → 장비, `pathlist.py`·
    `equip_paths.txt.xz` PAK 안 파일 이름 해시 찾기(`tools/make_equip_paths.py`로 REasy 목록에서 만듦), `scan.py` 모드 파일 살펴보기,
    `catalog.py`·`userdata.py` 게임 데이터 표·msg에서 이름·그림 경로, `msg.py` 텍스트, `tex.py`·`gdeflate.py` 텍스처,
    `icons.py` 게임 아이콘·레어도 색, `overview.py` 창에 보일 장비 목록. 창은 `mhwmm/equipview.py`
    - 적용 후 미리보기(3D): `mesh.py` 모델(.mesh) 읽기, `render.py` 순수 파이썬 3D 그리기, `look.py` 모드·게임 파일에서
      부위의 모델·재질·텍스처를 골라 그림·색 칩·텍스처 견본 만들기. 매니저의 `equipment_preview`가 `cache\equip-previews\`에 저장.
      `selftest.py` 실제 게임 파일로 미리보기 점검(글만, 진단 zip의 `preview-selftest.txt`와 `equip-preview --self-test`)
    - 게임 파일(텍스트·그림·데이터)은 저장소에 넣지 않는다. 테스트는 직접 만든 가짜 파일로 한다.
    - 모드 파일을 장비에 대응시키는 규칙(paths.py, scan.py)을 바꾸면 `scan.SCAN_FORMAT`을, 게임 데이터에서 읽는 내용(catalog.py)을
      바꾸면 `catalog.CATALOG_FORMAT`을, 게임 텍스처로 만드는 그림·아이콘(icons.py, tex.py, gdeflate.py)을 바꾸면
      `icons.PICTURE_FORMAT`을, 미리보기 그림이 달라질 수 있는 변경(mesh.py, render.py, look.py)을 하면 `look.PREVIEW_FORMAT`을
      올린다. 그래야 데이터 폴더 `cache\`의 예전 결과를 다시 만든다. 미리보기 캐시 폴더 이름에는 `PICTURE_FORMAT`과
      `SCAN_FORMAT`도 들어 있어(`ModManager.preview_formats`) 텍스처 해석이나 파일 규칙을 바꿔도 미리보기를 다시 그린다.
  - `mhwmm/crashlog.py`: REFramework 기록(re2_framework_log·reframework_faulty_files·re2_fw_config) 읽기, 게임 오류 창·
    이벤트 뷰어 문구 인식. `mhwmm/crashes.py`: 게임 튕김 기록 — 매니저를 켤 때와 게임 실행 직전에 데이터 폴더 `crashes\`로 복사
    (최근 10개), 모드와 맞춰 보기(explain, 항상 '의심'과 근거), 진단 zip(사용자 이름 가림, state.json 제외). 창은 gui.py의
    CrashDialog. 실제 사용자 기록·덤프는 저장소에 넣지 않고 테스트는 직접 만든 가짜 기록으로 한다
  - `mhwmm/bisect.py`: 원인 모드 찾기. 다음 단계는 지금까지의 답만으로 다시 계산(`next_step`)하고, 진행 상황은 데이터 폴더
    `bisect.json`. 현재 프로필을 복제한 임시 프로필 '원인 찾기 (임시)'에서만 켜고 끈다(사용자 프로필은 건드리지 않음).
    진행 중에는 GUI·CLI가 설치·삭제·켜기/끄기·옵션·순서·프로필·게임 폴더 변경을 막는다(`bisect.BLOCKED`, 매니저 자체는 막지 않음).
    결과는 답 하나에만 기대지 않는다(원인 모드로 튕긴 것과 그 모드 없이 괜찮았던 것을 각각 두 번 본다). 찾는 순서를 바꾸면
    test_bisect.py의 답 하나가 틀린 경우 시험이 엉뚱한 모드를 짚지 않는지 확인한다
    창은 gui.py의 BisectStartDialog·BisectAskDialog·BisectResultDialog와 목록 위 안내 줄
  - `mhwmm/gamewatch.py`: 매니저에서 실행한(또는 켤 때 실행 중인) 게임 프로세스가 어떻게 끝났는지 확인(Windows API, 안 되면
    tasklist, 다른 OS에서는 아무것도 하지 않음). 종료 코드는 모두 manager.log에 남기고, 정상 종료면 `crashes\last_good.json`.
    테스트는 가짜 ProcessApi로 한다 (진짜 프로세스는 Windows CI에서만)
  - `mhwmm/precheck.py`: 실행 전 점검. 빠른 `check()`(폴더 몇 개만 훑어 1초 안에, 예외를 내지 않음)와 느린 `background()`(지난
    게임 기록, 매니저가 넣지 않은 natives 파일의 읽기 전용 재질 점검. 결과는 `cache\precheck-scan.json`이고 `check()`가 지금도
    맞는 것만 씀). 목록의 ⚠ 문제(`ModManager.problems`)도 여기서 만든다. 게임 실행 직전에는 새로 생긴 높음·보통 항목만 묻고
    (`to_ask`, '같은 내용이면 다시 묻지 않기'는 데이터 폴더 `precheck.json`에 게임 버전마다) 실행은 막지 않는다(실패해도 실행).
    게임 폴더는 바꾸지 않고, REFramework 설정만 사용자가 동의했을 때 게임이 꺼져 있을 때 `set_refw_option`으로 한 줄 바꾼다.
    느린 점검의 결과가 달라지는 변경을 하면 `precheck.BACKGROUND_FORMAT`을 올린다. 원인 모드 찾기 중에는 묻지 않는다.
    창은 gui.py의 PrecheckDialog(게임 실행 직전, ☰ → 실행 전 점검), 명령줄은 `check`
  - `mhwmm/gui.py`, `mhwmm/theme.py`: tkinter GUI와 테마
  - `mhwmm/cli.py`: 명령줄
