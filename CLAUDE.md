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
    이벤트 뷰어 문구 인식, 게임 자체 오류 보고서(`<게임>\CrashReport\*.zip`의 CrashReport.txt; 그래픽 오류 등은 이것만 남고
    게임은 종료 코드 0으로 끝남) 읽기. `mhwmm/crashes.py`: 게임 튕김 기록 — 매니저를 켤 때와 게임 실행 직전에 데이터 폴더 `crashes\`로 복사
    (최근 10개), 모드와 맞춰 보기(explain, 항상 '의심'과 근거), 진단 zip(사용자 이름 가림, state.json 제외). 창은 gui.py의
    CrashDialog. 실제 사용자 기록·덤프는 저장소에 넣지 않고 테스트는 직접 만든 가짜 기록으로 한다
  - `mhwmm/bisect.py`: 원인 모드 찾기. 다음 구성은 시작할 때 고정한 것(사전 확률·지난 실행 관찰·파라미터)과 지금까지의
    답만으로 다시 계산하고(재시작해도 같은 구성), 진행 상황은 데이터 폴더 `bisect.json`(형식 2). 현재 프로필을 복제한 임시 프로필
    '원인 찾기 (임시)'에서만 켜고 끈다(사용자 프로필은 건드리지 않고, 모드를 끄는 것은 결과 창에서 사용자가 고를 때만 `finish(disable=)`).
    진행 중에는 GUI·CLI가 설치·삭제·켜기/끄기·옵션·순서·프로필·게임 폴더 변경을 막는다(`bisect.BLOCKED`, 매니저 자체는 막지 않음).
    다른 모드에 파일을 덮어쓰이는 모드는 덮어쓰는 모드와 함께만 켜고(`masking`), 파일이 모두 밀린 모드는 덮어쓰는 모드가
    모두 켜질 때만 켠다(`hidden_winners`: 사용자 구성에서 읽히지 않던 파일이 들어가지 않게). 형식 1(v2.2.2·v2.2.3에서 시작한 것)은
    예전 `_Search`/`next_step` 그대로 이어 간다. 결과는 늘 '의심'과 가능성(%)·근거로 말하고 확실하다고 하지 않는다.
    창은 gui.py의 BisectStartDialog·BisectAskDialog·BisectResultDialog와 목록 위 안내 줄. 엔진 계산(`Session.settle`·
    `make_plan`·재시작 뒤 첫 `next()`)은 GUI 스레드 밖에서 하고, GUI는 `session.ready`를 확인한 뒤에 `next()`를 부른다
  - `mhwmm/bisect_engine.py`: 원인 찾기의 베이즈 계산(순수 함수, 입출력 없음). 원인 후보(한 모드·함께일 때만 튕기는 두/세 모드·
    각각 튕기는 두 모드·모드 문제 아님·그 밖) × 재현율 r, 답의 무게(배경 튕김·틀린 답·플레이 시간 g·파일을 읽지 않은 모드),
    정보량이 가장 큰 구성 고르기, 결과를 내는 규칙(피벗 규칙). 모델을 바꾸면 `ENGINE_FORMAT`을 올린다. 진행 중인 원인 찾기는
    `bisect.json`에 저장한 파라미터로 계속한다. 같은 답이면 같은 구성(문자열 set을 그대로 돌지 않음, PYTHONHASHSEED와 무관)
    - 피벗 규칙: 모드를 짚으려면 P(원인에 들어 있음) ≥ 0.95이고, 그 모드를 켜고 튕긴 실행(원인 찾기 실행이나 튕김 기록)과
      그 모드만 빼고(나머지는 그 튕긴 구성 그대로) 충분히 오래 괜찮았던 원인 찾기 실행의 쌍이 서로 겹치지 않게 2개 이상(가끔
      튕기면, 또는 '한 번 더 확인' 뒤에는 3개) 있어야 한다. 지난 실행 기록·마지막 정상 종료는 확률만 바꾸고 쌍에는 쓰지 않는다.
      사전 확률을 읽지 않는 규칙이라 기록이 틀려도 실행만 늘어나고, 틀린 답 하나로는 엉뚱한 모드를 짚지 않는다.
      무엇을 켜든 가끔 튕기면(모드 문제 아님) 답이 우연히 한 모드를 가리켜 쌍도 우연히 생기므로, 모드를 짚을 때
      P(모드 문제 아님) ≥ `Params.none_controls`이면 먼저 모든 모드를 끈 실행을 본다(괜찮은 것이 짧게 한 것까지 세어
      `max_controls`번이 될 때까지, 튕기면 확률이 '모드 문제 아님'으로 옮겨 가 계속 찾음). 남은 실행 수 짐작은 짚기 직전이
      아니면 이 실행을 위쪽 끝에만 더한다. 기록의 오류 종류는 보지 않는다(그래픽 오류 DEVICE_HUNG 등도 다른
      튕김과 같고 사전 확률은 '언제 튕기나요?' 답대로). 새 원인 찾기는 `Params.current()`(`bisect_engine.CURRENT`, '가끔'은
      `WHEN_RULES`로 더 엄격하게)를 저장하고, 새 Params 필드의 기본값은 예전 규칙이라 키가 없는 `bisect.json`은 시작한 규칙
      그대로 이어 간다(v2.2.5에서 그래픽 오류 기록으로 시작한 것은 저장한 `controls` 2, 모든 모드를 끈 실행 두 번). 모델은
      그대로라 `ENGINE_FORMAT`은 올리지 않았다. 이 문턱은 정확한 튕김의 실행 수와 맞바꾸는 값이라(낮출수록 엉뚱한 모드를 덜
      짚고 실행이 늘어남) 바꾸면 `tools/bisect_sim.py S17`(모드 문제 아님 × 기록 종류)과 S1~S4를 함께 본다
      찾는 순서나 규칙을 바꾸면 test_bisect_engine.py의 '답 하나가 틀린 경우' 시험(엉뚱한 모드를 짚지 않음)을 꼭 확인한다.
      `tools/bisect_sim.py --quick|--all|--timing`으로 모의 실험(CI에서는 돌리지 않음)
  - `mhwmm/evidence.py`: 원인 찾기를 시작할 때의 근거 — 튕김 기록(crashes.explain의 발견)·재질 상태·오래된 REFramework·
    최근 설치·모드 종류로 모드별 가중치와 강한 단서의 몫(share), 함께 튕길 만한 쌍, '모드 문제 아님' 배율, 기록·지난 실행
    (`crashes\launches.json`)을 관찰로. 근거마다 이유 글을 붙이고, 출처 하나가 실패해도 나머지로 계속한다(`build`는 예외를
    내지 않음). 숫자는 짐작이라 결과를 정하지 않는다(피벗 규칙). bisect_engine을 가져오지 않고 속성(`Evidence`)으로만 넘긴다.
    그래픽 쪽 튕김(D3D12·드라이버 안, 게임 보고서의 DEVICE_HUNG 등)은 '모드 문제 아님' 근거가 아니다(MS 문서: 앱이 보낸 잘못된
    명령). 모델·텍스처·재질·셰이더·이펙트·GPU 옷감·털 파일(`RENDER_TYPES`)이나 그래픽 플러그인이 있는 모드(PAK은 장비 경로
    목록으로 안의 파일 확인, 맞지 않는 재질이면 더)를 올리고 Lua·데이터만 있는 모드는 조금 내린다(`graphics`)
  - `mhwmm/accessed.py`: REFramework의 `reframework_accessed_files.txt`(LooseFileLoader_LogAccessedFiles) 읽기, 요청을 모드에
    대응(루스 파일 우선, 그다음 높은 PAK), 실행마다 '읽지 않은 모드'와 튕기기 직전 10초의 모드(`step_run`). 경로 형식이 실제
    파일로 확인되지 않아 너그럽게 읽고, 대응이 안 되면 '근거 없음'이 되지 '읽지 않음'이 되지 않는다. 설정은 사용자가 고를 때만,
    게임이 꺼져 있을 때만 바꾸고(`natives\`에 파일이 있으면 LooseFileLoader는 켜지 않음) 원인 찾기를 마치면 되돌린다(게임이
    켜져 있으면 데이터 폴더 `refw_restore.json`에 두었다가 `apply_pending`)
  - `mhwmm/gamewatch.py`: 매니저에서 실행한(또는 켤 때 실행 중인) 게임 프로세스가 어떻게 끝났는지 확인(Windows API, 안 되면
    tasklist, 다른 OS에서는 아무것도 하지 않음). 종료 코드는 모두 manager.log에 남기고, 정상 종료면 `crashes\last_good.json`,
    실행마다 `crashes\launches.json`(최근 90일 100번). 게임 자체 오류 보고서가 있으면 종료 코드 0이어도 튕긴 것으로 본다.
    테스트는 가짜 ProcessApi로 한다 (진짜 프로세스는 Windows CI에서만)
  - `mhwmm/precheck.py`: 실행 전 점검. 빠른 `check()`(폴더 몇 개만 훑어 1초 안에, 예외를 내지 않음)와 느린 `background()`(지난
    게임 기록, 매니저가 넣지 않은 natives 파일의 읽기 전용 재질 점검. 결과는 `cache\precheck-scan.json`이고 `check()`가 지금도
    맞는 것만 씀). 목록의 ⚠ 문제(`ModManager.problems`)도 여기서 만든다. 게임 실행 직전에는 새로 생긴 높음·보통 항목만 묻고
    (`to_ask`, '같은 내용이면 다시 묻지 않기'는 데이터 폴더 `precheck.json`에 게임 버전마다) 실행은 막지 않는다(실패해도 실행).
    게임 폴더는 바꾸지 않고, REFramework 설정만 사용자가 동의했을 때 게임이 꺼져 있을 때 `set_refw_option`으로 한 줄 바꾼다.
    느린 점검의 결과가 달라지는 변경을 하면 `precheck.BACKGROUND_FORMAT`을 올린다. 원인 모드 찾기 중에는 묻지 않는다.
    창은 gui.py의 PrecheckDialog(게임 실행 직전, ☰ → 실행 전 점검), 명령줄은 `check`
  - `mhwmm/gui.py`, `mhwmm/theme.py`: tkinter GUI와 테마. 창 크기를 바꿀 때 버벅이지 않게(Windows는 위젯마다 창이고 Tk의 창
    클래스 TkTopLevel·TkChild가 CS_HREDRAW|CS_VREDRAW라, 끄는 단계마다 모든 자식 창을 다시 배치하고 그린 뒤에야 다음 마우스 움직임을
    받았음): `RESIZE_CLASS_REDRAW`('both' 기본, `class_redraw`)로 두 클래스의 그 스타일을 프로세스에서 한 번 끄고(클래스 이름은
    GetClassNameW로 toplevel의 창과 그 GetParent(`wm frame`)에서 읽음. 자식 클래스는 창 크기를 지켜보기 시작할 때, 바깥 창은 창이 보인
    뒤 첫 `<Configure>`에서. 실패하면 그대로 두고 `_CLASS_REDRAW`에 기록, Windows 밖에서는 아무것도 하지 않음), 창의 내용 전체를 한
    프레임(메인 창 `App.content`, 장비 외형 창 `content`, 사용법·충돌 창은 `Dialog.FREEZE`로 `body`)에 넣어 place로 창을 채운 뒤
    `FreezeContent`가 끄는 동안 그 크기를 그대로 둔다(늘어난 곳은 창 배경, 줄어든 쪽은 잘림. 멈추고 `RESIZE_SETTLE_MS` 뒤에 한 번
    배치). `RESIZE_MODE`는 'freeze'(기본)와 벤치마크 비교용 'off'(매 단계 배치, 예전 `RESIZE_FREEZE = False`도 'off'). 실제
    Windows에서 `tools/resize_bench.py`와 `Resize bench` 워크플로로 잼: 끄는 한 단계 ~145 ms → ~10 ms(맨 Tk 창과 같음), 화면 점검에서
    달라진 화소 없음(얼리기만 하면 ~50 ms. 끄는 동안 내용의 그림을 대신 보여 주는 'snapshot'도 ~10 ms였지만 코드가 많아 뺐음).
    끄는 중인지는 `resizing`(크기가 `RESIZE_SETTLE_MS` 안에 연달아 바뀔 때. 최대화·복원처럼 한 번 바뀌면 바로 따라감. 한 번 바뀐 뒤
    `RESIZE_FIRST_MS` 안의 변경과, 끄다가 멈춰 한 번 배치한 뒤(`FreezeContent._settled`) 그 안의 변경도 끌기. 이것이 시간 규칙).
    Windows에서는 `RESIZE_FIRST_STEP`(`_drag_step`, 마우스·창 조회 `_WinPointer`/`_pointer`)으로 첫 단계부터 끌기로 본다: wm
    state가 바뀐 변경(최대화·복원, 제목 표시줄 더블클릭)은 끌기가 아니고, 주 마우스 버튼이 떼어져 있고 이동·크기 조절 루프도
    아니면(또는 '창을 끄는 동안 내용 표시'가 꺼져 있으면) 바로 앞 변경의 `RESIZE_SETTLE_MS` 안일 때만(Windows에서 `RESIZE_FIRST_MS`와
    `_settled` 뒤의 규칙은 버튼이 눌려 있거나 루프 중일 때만), 버튼이 눌려 있거나 루프 중이면 시간 규칙(`RESIZE_SETTLE_MS`·
    `RESIZE_FIRST_MS`·`_settled`)에 맞거나, 커서가 크기 조절 테두리에 있거나, 창 틀의 두 변 이상이 그대로이고 바뀌기 전후 모두 작업
    영역만큼 넓거나 높지 않으면 끌기(시간 규칙을 빼면 끌다가 멈춘 뒤 다시 끄는 단계가, 커서가 화면에 붙인 창의 가장자리보다 앞서
    있을 때 창 전체를 다시 배치함. 창 메시지를 보내지 않는 조회만, 조회가 실패하면 시간 규칙). place는 창에 크기를 요청하지
    않으므로 창 크기는 geometry()로 정하고(대화 상자는 show()에서 pack으로
    잰 크기), 메인 창에 새로 넣는 위젯은 root가 아니라 `self.content`에 둔다. 모드 목록의 열은 `_place_sash`에서 열 너비를 한 번 다시
    줘서 ttk의 slack을 0으로 만든다(얼린 끌기는 중간 폭을 건너뛰어 파일/충돌 열이 잘렸음). 스크롤되는 카드 목록의 폭은 `follow_width`,
    로그 창은 `LOG_LINES`줄만(전체는 manager.log), `<Configure>` 처리기는 값이 바뀔 때만 일하고, 막대 오른쪽 위젯들은 한 프레임에
    묶는다(왼쪽 위젯들 다음에 만들어야 Tab 순서가 그대로). 창 크기 시험은 `maxsize`를 크게 주고(Windows는 화면보다 크게 만들지 않음),
    첫 단계는 가짜 마우스(`_pointer`, 시간 규칙 시험은 None)로, `_WinPointer`·`_WinClasses`는 가짜 user32(`_win_functions`)로 본다
    (Windows CI에서는 앱의 기본값대로 실제 클래스도 바뀌고, 시험은 클래스 이름과 스타일을 읽을 수 있는지만 확인)
    - 창 크기 벤치마크: `tools/resize_bench.py`(가짜 모드 150개로 실제 App을 열고, Windows에서는 SendInput으로 창 모서리를 끌어
      Configure 간격·지연을 잼, 그 밖에서는 geometry 단계만. 가짜 단계는 버튼을 누르지 않으므로 `RESIZE_FIRST_STEP`을 끄고 잰다).
      Windows 측정은 GitHub Actions `Resize bench` 워크플로를 수동 실행(`args` 입력, 결과는 작업 요약과 `resize-bench` 아티팩트, 화면
      점검 그림은 `resize-bench-frames/<변형>/`. `patch` 입력 `file:tools/bench-patches/<이름>.diff`로 커밋 전 변경도 잼). 'all'은
      bare, base(앱의 기본값), off, freeze, keep+freeze, both+off, both+freeze+nofirststep, scale150, scale150+off
      (`keep`/`top`/`child`/`both`는 `RESIZE_CLASS_REDRAW`, 스위치가 없는 gui면 벤치마크가 직접 바꿈). 그림 변형(snapshot, unmap,
      fakecapture)은 그 모드가 있는 gui에서만 돌고 없으면 이유를 적고 건너뜀. 위젯을 뺀 변형(nolog 등)은 `+off`일 때만 배치에서
      무엇이 비싼지 보여 줌. 화면 점검(Windows, GDI로 창 클라이언트 영역, `--no-visual`로 끔): 끌기·단계·최대화/복원·테마 전환 뒤
      강제로 다시 그린(RedrawWindow ALLCHILDREN|UPDATENOW) 창과 같은지(달라지면 stale), 끄는 동안 마우스 스레드가 잡은 몇 단계가 끌기
      전과 같은지(mid, 늘어난 곳은 창 배경), 끌기 뒤 같은 크기로 바로 바꾼 창과 같은지(direct), 창 클래스는 프로세스 전체에 걸리므로
      사용법·충돌 한눈에 보기 대화 상자와 장비 외형 창도(`--no-windows`로 끔). 화소는 그대로 비교(한 화소만 달라도 실패, 끄는 동안의
      프레임은 `SHARE_TOL`까지 minor). 그동안 자동 가비지 수집은 끄고 창을 닫을 때마다 주 스레드에서 수집(장비 외형 창의 작업
      스레드에서 Tk 객체가 풀리면 프로세스가 죽음). 리눅스 시험은 가짜 Windows API(tests/test_resize_bench.py), 리눅스에서는
      `xvfb-run -a python tools/resize_bench.py --quick`로 도구만 점검(CI에서는 돌리지 않음)
    - `tools/bench-patches/`: 커밋하기 전에 워크플로로 잰 변경의 diff(임시). 그 변경을 커밋하거나 버리면 지운다
  - `mhwmm/cli.py`: 명령줄
