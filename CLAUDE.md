# stock-brief — 온디맨드 종목 영상 생성기

> **이 파일은 매 턴 주입된다 — 항상 간결하게 유지할 것 (상세 이력은 git log·PR 참고).**

## 작업 원칙
- Claude가 할 수 있는 작업은 자율 진행: **PR 생성 → 머지까지 직접** (squash). 수동 개입 필요할 때만 사용자에게 알림.
- 요청이 기존 구조와 충돌하면 조용히 구현하지 말고 먼저 피드백.
- 코드 상수·동작 변경 시 이 파일의 관련 줄도 같은 커밋에서 갱신.

## 토큰 절감 수칙
- 큰 파일 전체 Read 금지 — `weekly_video_prep.py`(2,700줄+)·`on-demand.html`(700줄+)은 Grep으로 위치 찾아 부분만.
- 씬 프리뷰는 절반 해상도로 확인, 변경된 씬만 렌더. 문구 검증은 이미지 대신 TTS 덤프·대본 텍스트 우선.
- **새 작업은 stock-brief만 연결한 새 세션에서** (rklb-dashboard 연결 금지 — 34KB CLAUDE.md가 매 턴 주입됨).

## 프로젝트 개요
아무 티커나 입력하면 뉴스 분석 → 대본 → 씬 이미지 → 영상 → 메일. 정기 크론 없음(요청 시만).
- **실행**: GitHub Pages 리모컨 `on-demand.html` (또는 Actions → on-demand-video.yml). PAT는 localStorage.
- **기본 브랜치**: `main`. 작업 브랜치: `claude/stock-brief-video-error-7dlxzp` (머지 후엔 main에서 재시작).

## 파이프라인 (scripts/)
`resolve-ticker.js`(티커→회사 메타, configs/<T>/ 캐시) → `on-demand-collect.js`(뉴스 3→7→14일→무기한 확대 수집) → `weekly_video_prep.py`(대본+씬 PNG) → `weekly_video_make.py`(TTS+영상) → `gws_publish.py`(메일·YouTube 카피)

## 핵심 규칙 (사용자 확정 사항 — 회귀 금지)
- **통화**: `.KS/.KQ` 종목 = 원화 정수(`318,000원`), 그 외 = `$` 2소수점. `fmt_price()` 일원화.
- **한국 티커 오인 방지**: resolve-ticker가 `data/kr-stocks.json`(KRX 전 종목)에서 회사명 선조회 → 프롬프트 명시, 한국 거래소 아니면 캐시 안 함 (케냐항공 "KQ" 오인 사건).
- **파일명**: 영상 `{회사명}_YYYYMMDD.mp4`, 씬 `{YYMMDD}_{회사명}_씬N.png` (읽는 쪽은 구명 폴백 유지).
- **씬0** = "어떤 회사인가요?" 소개 씬 (6줄: 간략 주가 1줄 + 주력사업·방향·투자·제품·시장지위). 주가 분석 금지, 헤더에 현재가·전일%만.
- **씬1** = 핵심 뉴스 3선. 출처 신뢰도 "신뢰도 높음/중간/낮음" 3단, 3일 초과 기사엔 "M/D 기사" 배지, 뉴스 슬롯 비면 대본 줄로 카드 채움(placeholder 문장 제외).
- **씬2** = 다음주 전망. 서브에 "다음주 관전 포인트" 금지(중복), "이벤트 부재" 같은 결핍 고백 문구 금지.
- **문체**: 초보 기준 쉬운 말 — 전문용어·약어는 풀어 쓰고, 맥락 없는 결과 수치 나열 금지 (재검토 2회가 검증).
- **429(이미지 쿼터)**: 65초 대기 1회 재시도 → 계속 429면 남은 씬 스킵, 그라데이션 폴백 배경.

## 웹 (on-demand.html — GitHub Pages)
3열: 리모컨(한국·미국 종목 검색 `data/kr-stocks.json`·`us-stocks.json`) | 구글 관심 TOP30(`data/stock-trends.json`, 관리자 리셋 시만 갱신) | 최근 생성 영상(`data/on-demand/latest.json`, 씬 미리보기+YouTube 카피).
**관리자 탭**: 구글 로그인(허용 계정 sinlee01@gmail.com)으로만 열림 — GitHub 토큰 설정·순위 리셋. 실행 권한의 실질 경계는 PAT(localStorage, 미커밋).

## 워크플로 (.github/workflows/)
- `on-demand-video.yml` — 영상 생성 (workflow_dispatch, 티커 입력)
- `update-kr-stocks.yml` — 한국·미국 종목 목록 갱신 (수동)
- `update-stock-trends.yml` — 관심 주식 순위 (웹 리셋 버튼이 호출)

## 환경 제약 (로컬 검증 시)
- Gemini·Yahoo·KRX·edge-tts는 샌드박스 프록시가 차단 — 실호출 검증은 CI에서. 렌더는 `fonts-nanum` 설치 후 로컬 가능.
- Playwright: `pip install playwright` + `executable_path=/opt/pw-browsers/chromium-1194/chrome-linux/chrome`.
