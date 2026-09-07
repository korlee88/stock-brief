"""주간 주요 일정 캘린더 — 카카오톡 알림 (사용자 요청)

다음 주(월~일) 경제지표·통화정책·실적·컨퍼런스·크립토 일정을 Gemini + Google Search
그라운딩으로 모아 data/calendar/ 에 저장하고, 카카오톡 '나에게 보내기'로 요약을 보낸다.

카카오 text 템플릿은 200자 제한이라 일정 전체를 한 메시지에 담을 수 없다 →
★ 중요 일정만 200자로 압축해 보내고, 전체 목록은 calendar.html 링크로 연결한다.

정확도 원칙(CLAUDE.md '지어낸 숫자 금지'): 검색으로 확인된 항목만 싣는다. 출처(source)가
없는 항목은 버리고, 시각이 확인 안 되면 time을 비운다(추측 금지).

env:
  GEMINI_API_KEY       — 필수 (없으면 종료)
  KAKAO_REST_API_KEY   — 선택 (없으면 발송 생략, 파일만 갱신)
  KAKAO_REFRESH_TOKEN  — 선택
  GITHUB_REPOSITORY    — 링크 생성용 (owner/repo)
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
OUT_DIR = ROOT_DIR / "data" / "calendar"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY", "")
KAKAO_REFRESH_TOKEN = os.environ.get("KAKAO_REFRESH_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")

KST = timezone(timedelta(hours=9))
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]

# 카테고리 → 화면 배지 (프롬프트가 이 키만 쓰도록 강제)
CATEGORIES = ["지표", "통화정책", "실적", "이벤트", "크립토", "정치"]


def target_week():
    """대상 주간 (월요일, 일요일). 금·토·일에 실행하면 '다음 주', 그 외엔 '이번 주'.

    일요일 밤 크론으로 돌면 다가올 한 주가 잡히고, 주중에 수동 실행하면 진행 중인
    주가 잡힌다."""
    today = datetime.now(KST).date()
    if today.weekday() >= 4:            # 금(4)·토(5)·일(6) → 다음 주
        monday = today + timedelta(days=7 - today.weekday())
    else:                               # 월~목 → 이번 주
        monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def _fmt_day(d):
    return f"{d.month}/{d.day}({WEEKDAY_KO[d.weekday()]})"


PROMPT = """{start} ~ {end} (한국시간 기준) 한 주 동안 예정된 금융·경제 주요 일정을 조사해줘.

포함할 것:
- 경제지표 발표 (미국 CPI·PPI·고용지표, 한국 GDP·물가, 중국 CPI·PPI 등)
- 중앙은행 통화정책 (FOMC, ECB, BOJ, 한국은행 금통위 — 결정·기자회견)
- 주요 기업 실적 발표·컨퍼런스콜 (미국 대형주 위주)
- 대형 테크 이벤트 (신제품 발표, 개발자 컨퍼런스)
- 크립토 관련 일정 (주요 규제 표결, 메인넷 출시, 대형 컨퍼런스)
- 주요 정치·외교 일정 (정상회담 등 시장에 영향 있는 것만)

반드시 지킬 것:
- **검색으로 실제 확인된 일정만** 싣는다. 확실하지 않으면 아예 빼라 — 지어내지 말 것.
- 시각은 **한국시간(KST) 24시간제 "HH:MM"**. 시각이 확인 안 되면 time을 빈 문자열로.
- date는 반드시 "YYYY-MM-DD".
- category는 다음 중 하나만: {categories}
- source: 그 일정을 확인한 출처(기관·매체 이름). 출처를 못 대면 그 항목은 빼라.
- title은 25자 이내 한국어 (예: "미국 8월 CPI·근원 CPI").

- impact: **S&P500·나스닥 지수에 미칠 영향의 크기**를 "high"/"medium"/"low" 중 하나로 매긴다.
  지수 전체 기준으로 판단한다 — 개별 종목만 흔드는 재료는 지수 영향으론 낮게 본다.
  · high   지수 전체를 크게 움직일 수 있는 일정 — FOMC 금리 결정, 미국 CPI·PPI,
           고용보고서, 엔비디아·애플급 초대형주 실적, 연준 의장 발언
  · medium 섹터나 지수에 눈에 띄는 영향을 줄 수 있는 일정 — ECB·BOJ 금리 결정,
           대형주 실적, 소비자심리지수, 주요 신제품 발표
  · low    지수 방향엔 거의 영향이 없는 일정 — 지역 연은 인사 연설, 소규모 컨퍼런스,
           참고용 2차 지표, 해외 개별 이벤트
  이유는 쓰지 말고 등급만 매긴다.

출력은 **JSON 배열만** (설명·마크다운 코드펜스 없이):
[{{"date":"2026-09-10","time":"21:30","title":"미국 8월 CPI","category":"지표","impact":"high","source":"미 노동통계국"}}]
"""

# 영향도(S&P500·나스닥 지수 기준) — 정렬·표기에 사용
IMPACT_RANK = {"high": 0, "medium": 1, "low": 2}


def fetch_events(start, end):
    """Gemini + Google Search 그라운딩으로 주간 일정 수집. 실패 시 빈 리스트."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = PROMPT.format(start=start.isoformat(), end=end.isoformat(),
                           categories=", ".join(CATEGORIES))
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        ),
    )
    return _parse_events(response.text or "", start, end)


def _parse_events(raw, start, end):
    """모델 응답에서 JSON 배열을 뽑아 검증·정렬. 출처 없는 항목·기간 밖 항목은 버린다."""
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        print("   ⚠ JSON 배열을 찾지 못함", file=sys.stderr)
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        print(f"   ⚠ JSON 파싱 실패: {e}", file=sys.stderr)
        return []

    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        date = str(it.get("date", "")).strip()
        title = str(it.get("title", "")).strip()
        source = str(it.get("source", "")).strip()
        # 출처 없는 항목은 지어냈을 가능성이 커 제외 (CLAUDE.md 지어낸 정보 금지 규칙)
        if not (date and title and source):
            continue
        try:
            d = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (start <= d <= end):
            continue
        time_s = str(it.get("time", "")).strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", time_s):
            time_s = ""
        cat = str(it.get("category", "")).strip()
        # 등급이 없거나 이상하면 "medium" — 모르는 걸 "low"로 깎으면 놓쳐선 안 될 일정이 묻힌다
        impact = str(it.get("impact", "")).strip().lower()
        out.append({
            "date": date,
            "time": time_s,
            "title": title[:40],
            "category": cat if cat in CATEGORIES else "이벤트",
            "impact": impact if impact in IMPACT_RANK else "medium",
            "source": source[:40],
        })
    out.sort(key=lambda e: (e["date"], e["time"] or "99:99"))
    return out


def build_kakao_text(events, start, end):
    """카카오 200자 제한에 맞춰 영향도 높은 순으로 압축. 넘치면 잘라내고 '외 N건'.

    지수 영향 '높음'은 ★로 표시 — 한 줄만 훑어도 뭘 챙겨야 하는지 보이게."""
    head = f"📅 {_fmt_day(start)}~{_fmt_day(end)} 주요 일정"
    # 영향 큰 것부터, 같은 등급이면 날짜·시각 순
    picks = sorted(events, key=lambda e: (IMPACT_RANK[e["impact"]], e["date"], e["time"] or "99:99"))

    lines = [head]
    used = len(head)
    shown = 0
    for e in picks:
        d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        when = f"{d.month}/{d.day}" + (f" {e['time']}" if e["time"] else "")
        star = "★" if e["impact"] == "high" else ""
        line = f"{star}{when} {e['title']}"
        # 마지막 '외 N건' 자리(약 12자)를 남겨 둔다
        if used + len(line) + 1 > 200 - 12:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1

    rest = len(events) - shown
    if rest > 0:
        lines.append(f"…외 {rest}건, 전체 보기 ↓")
    return "\n".join(lines)[:200]


def send_kakao(text, link_url):
    """카카오톡 '나에게 보내기' (text 템플릿 + 링크 버튼).

    gws_publish.py의 발송 로직과 같은 방식이지만, 그 모듈은 import 시점에
    ticker.json을 읽어야 해서 여기서는 최소한만 자체 구현한다."""
    import urllib.parse
    import urllib.request

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": KAKAO_REST_API_KEY,
        "refresh_token": KAKAO_REFRESH_TOKEN,
    }).encode()
    req = urllib.request.Request("https://kauth.kakao.com/oauth/token", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        token = json.loads(r.read()).get("access_token")
    if not token:
        raise RuntimeError("액세스 토큰 발급 실패")

    template = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": link_url, "mobile_web_url": link_url},
        "button_title": "전체 일정 보기",
    }
    body = urllib.parse.urlencode({
        "template_object": json.dumps(template, ensure_ascii=False)
    }).encode()
    req = urllib.request.Request(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send", data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")


def calendar_url():
    if not REPO or "/" not in REPO:
        return "https://github.com"
    owner, repo = REPO.split("/", 1)
    return f"https://{owner}.github.io/{repo}/calendar.html"


def main():
    if not GEMINI_API_KEY:
        print("⚠ GEMINI_API_KEY 없음 — 종료", file=sys.stderr)
        sys.exit(1)

    start, end = target_week()
    print(f"📅 대상 주간: {start} ~ {end}")

    try:
        events = fetch_events(start, end)
    except Exception as e:
        print(f"⚠ 일정 수집 실패: {e}", file=sys.stderr)
        sys.exit(1)

    if not events:
        print("⚠ 확인된 일정 없음 — 파일·발송 모두 건너뜀", file=sys.stderr)
        sys.exit(0)

    tally = {k: sum(1 for e in events if e["impact"] == k) for k in IMPACT_RANK}
    print(f"   ✅ {len(events)}건 수집 — 지수 영향 높음 {tally['high']} · 중간 {tally['medium']} · 낮음 {tally['low']}")

    payload = {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "generated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "events": events,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    (OUT_DIR / "latest.json").write_text(body, encoding="utf-8")
    (OUT_DIR / f"{start.isoformat()}.json").write_text(body, encoding="utf-8")
    print(f"   💾 data/calendar/latest.json · {start.isoformat()}.json")

    text = build_kakao_text(events, start, end)
    print(f"\n--- 카카오 발송 문구 ({len(text)}자) ---\n{text}\n")

    if KAKAO_REST_API_KEY and KAKAO_REFRESH_TOKEN:
        try:
            send_kakao(text, calendar_url())
            print("   💬 카카오톡 발송 완료")
        except Exception as e:
            print(f"   ⚠ 카카오 발송 실패(파일은 갱신됨): {e}", file=sys.stderr)
    else:
        print("   [SKIP] KAKAO 시크릿 없음 — 발송 생략")


if __name__ == "__main__":
    main()
