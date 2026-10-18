# 몬스터 헌터 와일즈 모드 매니저

Monster Hunter Wilds(몬스터 헌터 와일즈) PC판용 모드 관리 프로그램입니다.
Nexus Mods 등에서 받은 모드 압축 파일을 끌어 놓기만 하면 설치되고, 체크박스로 켜고 끌 수 있으며,
게임 폴더에 넣은 파일을 모두 기록해 두었다가 깨끗하게 되돌립니다.

## 주요 기능

- **압축 파일 그대로 설치**: zip / 7z / rar / 폴더 / 단일 `.pak`·`.lua`·`.dll` 파일
  - 압축 안의 폴더 구조를 분석해서 게임 폴더 기준 위치를 자동으로 찾습니다.
  - Fluffy Mod Manager 형식(`modinfo.ini`)의 이름·제작자·버전·설명·미리보기 이미지를 읽습니다.
  - 옵션이 여러 개 들어 있는 모드는 설치할 옵션을 고르는 창이 뜹니다.
  - Nexus 파일 이름(`이름-123-1-2-1740000000.zip`)에서 모드 이름과 버전을 뽑아냅니다.
  - 한글 파일 이름이 들어 있는 zip(cp949)도 깨지지 않습니다.
- **모든 모드 형식 지원**
  - 루스 파일 모드 (`natives/STM/...`)
  - PAK 모드 (`re_chunk_000.pak.sub_000.pak.patch_XXX.pak`) — **번호를 자동으로 매깁니다**
  - REFramework 스크립트/플러그인 (`reframework/autorun/*.lua`, `reframework/plugins/*.dll`,
    `reframework/` 폴더 없이 `autorun/`만 들어 있는 경우 포함)
  - REFramework 자체(`dinput8.dll`)와 기타 DLL 모드
- **켜기/끄기와 로드 순서**: 체크박스로 켜고 끄며, 드래그 또는 ▲▼ 버튼으로 순서를 바꿉니다.
  목록 아래쪽 모드일수록 우선 적용됩니다.
- **충돌 표시**: 같은 파일을 바꾸는 모드가 있으면 어느 모드가 이기는지 파일 단위로 보여 줍니다.
- **안전한 적용/해제**
  - 게임 폴더에 넣은 파일을 모두 기록하고, 바뀐 부분만 복사·삭제·이름 변경합니다.
  - 모드가 원래 게임 파일을 덮어쓰면 백업해 두었다가 모드를 끌 때 복원합니다.
  - 모드를 끄면 비게 된 폴더까지 정리합니다. 직접 넣은 다른 파일은 건드리지 않습니다.
- **게임 업데이트 대응**: 업데이트가 모드 PAK과 같은 번호의 공식 패치를 내려받아 덮어쓴 경우를 감지해서,
  그 파일은 게임 파일로 두고 모드 PAK만 새 번호로 다시 배치합니다.
- **기타**: Steam 라이브러리에서 게임 폴더 자동 감지, REFramework 설치 여부 표시, 게임 실행 버튼,
  모드 업데이트(같은 이름 모드 교체 시 순서·활성 상태 유지), 명령줄(CLI) 지원

## 설치 및 실행

### 방법 1: 실행 파일(.exe)

GitHub 저장소의 **Actions → Build** 실행 결과에서 `MHWildsModManager-windows` 아티팩트를 받아
`MHWildsModManager.exe`를 실행합니다. 직접 만들려면 Python을 설치한 뒤 `build_exe.bat`를 실행하면
`dist\MHWildsModManager.exe`가 만들어집니다.

### 방법 2: Python으로 실행

1. [Python 3.10 이상](https://www.python.org/downloads/)을 설치합니다 (설치할 때 *tcl/tk* 포함, 기본값).
2. (선택) 추가 기능 설치: `pip install -r requirements.txt`
   - `tkinterdnd2`: 탐색기에서 파일을 끌어 놓아 설치
   - `Pillow`: JPG 미리보기 이미지 표시
   - `py7zr`: 7-Zip 없이 `.7z` 압축 풀기
3. `MHWildsModManager.pyw`를 더블클릭하거나 `python -m mhwmm`을 실행합니다.

> `.rar` 파일과 (py7zr이 없을 때) `.7z` 파일은 [7-Zip](https://www.7-zip.org)이 설치되어 있어야 풀 수 있습니다.

## 사용법

1. 처음 실행하면 Steam 라이브러리에서 게임 폴더를 찾습니다. 못 찾으면 **찾아보기...**로
   `MonsterHunterWilds.exe`가 있는 폴더를 지정하세요.
2. **＋ 모드 추가**를 누르거나 모드 압축 파일을 창에 끌어 놓습니다.
3. 체크박스(또는 더블클릭, 스페이스바)로 켜고 끕니다. **즉시 적용**이 켜져 있으면 바로 게임 폴더에 반영되고,
   꺼져 있으면 **게임에 적용**(Ctrl+S)을 눌러야 반영됩니다.
4. 같은 파일을 바꾸는 모드끼리는 아래쪽 모드가 이깁니다. 오른쪽 **충돌** 탭에서 확인할 수 있습니다.

### REFramework

루스 파일(`natives`) 모드와 Lua 스크립트는 [REFramework](https://github.com/praydog/REFramework-nightly/releases)가
필요합니다. 받은 `MHWILDS.zip`을 다른 모드처럼 이 프로그램으로 설치해도 됩니다.
루스 파일 모드는 게임 안 REFramework 메뉴의 **LooseFileLoader → Enable Loose File Loader**가 켜져 있어야 합니다.

### PAK 모드 번호는 어떻게 매기나요?

게임은 `re_chunk_000.pak.sub_000.pak.patch_001.pak`, `_002`, `_003` … 순서로 읽다가 빈 번호에서 멈춥니다.
이 프로그램은

- 공식 패치나 다른 프로그램이 넣은 PAK 번호는 건너뛰고,
- 빈 번호 없이 이어지도록 모드 PAK에 번호를 매기며 (로드 순서대로, 뒤 번호가 우선),
- 모드를 끄거나 순서를 바꾸면 파일을 다시 복사하지 않고 이름만 바꿔 번호를 다시 맞춥니다.

압축 안의 PAK 이름이 이미 `re_chunk_000.pak.patch_005.pak`처럼 되어 있으면 그 체인(`re_chunk_000.pak`)을 따르고,
그 외에는 설정의 기본 대상(`re_chunk_000.pak.sub_000.pak`)을 사용합니다.

### 게임 업데이트 후

게임 업데이트가 새 공식 패치를 내려받으면서 모드 PAK을 덮어쓸 수 있습니다. 프로그램을 실행하면 이를 감지해서
다시 적용할지 묻습니다(또는 **도구 → 적용 상태 점검**). 다시 적용하면 공식 패치는 그대로 두고 모드 PAK만 뒤쪽
번호로 옮깁니다. 공식 파일을 지우는 일은 없습니다.

### 모드를 전부 빼고 싶을 때

**도구 → 게임 폴더에서 모든 모드 파일 제거**를 누르면 이 프로그램이 넣은 파일을 모두 지우고 백업해 둔 원본을
복원합니다. 설치된 모드 목록은 그대로 남아서 나중에 **게임에 적용**으로 다시 넣을 수 있습니다.

## 데이터 저장 위치

| 항목 | 위치 |
| --- | --- |
| 기본 | `%LOCALAPPDATA%\MHWildsModManager` |
| 포터블 모드 | 실행 파일과 같은 폴더에 빈 `portable.txt` 파일을 만들면 그 옆 `data\` 폴더 사용 |
| 직접 지정 | `--data-dir <폴더>` 옵션 또는 `MHWMM_DATA_DIR` 환경 변수 |

- `mods\<id>\files\` : 설치된 모드 파일 (게임 폴더 기준 구조)
- `state.json` : 모드 목록, 순서, 활성 상태, 설정
- `deployment.json` : 게임 폴더에 넣은 파일 기록
- `backups\` : 모드가 덮어쓴 원본 게임 파일
- `manager.log` : 로그

## 명령줄(CLI)

```
python -m mhwmm list                         # 모드 목록
python -m mhwmm game [경로]                  # 게임 폴더 설정 (생략 시 자동 감지)
python -m mhwmm install 모드.zip [--variant 2 | --all] [--replace] [--disabled]
python -m mhwmm enable 1 "모드 이름"          # 번호, 이름, id로 지정
python -m mhwmm disable 3
python -m mhwmm move "모드 이름" 5            # 로드 순서 변경 (아래일수록 우선)
python -m mhwmm remove 2
python -m mhwmm deploy | purge | verify
```

`enable`/`disable`/`install` 등은 바로 게임 폴더에 적용합니다 (`--no-deploy`로 끌 수 있음).

## 주의 사항

- 같은 게임 폴더에 Fluffy Mod Manager 등 다른 모드 관리자를 같이 쓰면 서로의 파일을 알 수 없습니다.
  이 프로그램은 다른 프로그램이 넣은 PAK 번호를 피하고 그 파일들을 건드리지 않지만, 한 가지 관리자만 쓰는 것을 권장합니다.
- 게임이 실행 중일 때는 PAK 파일이 잠겨 있어 적용할 수 없습니다. 게임을 끈 뒤 적용하세요.
- PAK 모드끼리 같은 대상을 바꾸는지는 PAK 내용을 열어 보지 않으므로 충돌 표시에 나오지 않습니다.

## 개발

```
pip install pytest
python -m pytest
```

- `mhwmm/analyzer.py` : 압축 내용 구조 분석
- `mhwmm/deploy.py` : 게임 폴더 적용/해제, PAK 번호 관리, 백업
- `mhwmm/manager.py` : 모드 목록·설정·설치
- `mhwmm/gui.py` : tkinter GUI, `mhwmm/cli.py` : 명령줄
