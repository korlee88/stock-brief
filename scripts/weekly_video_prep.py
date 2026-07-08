"""
주간 영상 자료 생성 스크립트
- 최근 LOOKBACK_DAYS(2일) auto-sessions.json 데이터 기반 (격일 생성 주기에 맞춘 신선 윈도우)
- Gemini API → 한국어 영상 대본(4 씬)
- Pillow → 씬별 1080×1920 카드 이미지 (YouTube Shorts 세로 포맷)
- 저장: data/weekly-report/YYYY-MM-DD/

종목 설정: config/ticker.json
"""

import os, json, sys, re, random, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT_DIR          = Path(__file__).parent.parent
# 온디맨드 멀티 종목 모드: env로 config·세션·출력 경로 오버라이드 (기본값 = 기존 RKLB 경로)
_CFG_PATH         = Path(os.environ.get("TICKER_CONFIG") or (ROOT_DIR / "config" / "ticker.json"))
TICKER_CONFIG     = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
TICKER            = TICKER_CONFIG["ticker"]
COMPANY_KO        = TICKER_CONFIG["company_ko"]
COMPANY_EN        = TICKER_CONFIG.get("company_en", TICKER)
INDUSTRY_KO       = TICKER_CONFIG.get("industry_ko", "")
EXCHANGE          = TICKER_CONFIG.get("exchange", "")
FUTURE_TECH_EN    = TICKER_CONFIG.get("image_future_tech_en", "")
BRAND_LABEL       = TICKER_CONFIG.get("brand_label", f"{TICKER} BRIEF")
REPO              = TICKER_CONFIG.get("repo", os.environ.get("GITHUB_REPOSITORY", ""))
COMPETITOR_TICKER = TICKER_CONFIG.get("competitor_ticker", "")

# 상단 헤더 브랜드 라벨: 티커(005930.KS)가 아니라 종목명(삼성전자)으로 표기 — 사람이 읽는 제목
# (v1.2: "BRIEF" 접미 삭제 — 사용자 요청, 종목명만 표기)
HEADER_BRAND      = COMPANY_KO or BRAND_LABEL

# ── 통화·가격 표기: 상장국 기준 ─────────────────────────────────────
# 한국거래소(.KS=KOSPI / .KQ=KOSDAQ) 종목은 원화·정수(318,000원), 그 외(미국 등)는 $·2소수점.
IS_KRW = TICKER.upper().endswith((".KS", ".KQ")) or "korea" in EXCHANGE.lower()

def fmt_price(value, decimals=None):
    """종목 통화에 맞춘 가격 문자열. 한국='318,000원'(정수), 그 외='$318,000.00'.
    값이 비거나 숫자가 아니면 빈 문자열. decimals로 소수점 자릿수 강제 지정 가능(미국만 적용)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if IS_KRW:
        return f"{v:,.0f}원"
    return f"${v:,.{2 if decimals is None else decimals}f}"

RULES_CONFIG = json.loads((ROOT_DIR / "config" / "rules.json").read_text(encoding="utf-8"))
RULES_MAP    = {r["id"]: r for r in RULES_CONFIG.get("rules", [])}   # 대시보드 'topRules' 배지와 동일 출처 (config 단일 진실)

# 대시보드 '매수지수 분해' scoringLayers 키 → 한국어 라벨 (대본 그라운딩용, 점수 자체는 노출 안 함)
SCORING_LAYER_LABELS = {
    "catFinancial":    "실적·재무 신호",
    "meanReversion":   "단기 과열·과매도 반동",
    "macdTrend":       "MACD 추세",
    "trendFilter":     "추세 필터",
    "googleTrends":    "검색 관심도",
    "youtubeInterest": "유튜브 관심도",
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
AUTO_SESSIONS     = Path(os.environ.get("SESSIONS_FILE") or (ROOT_DIR / "data" / "auto-sessions.json"))
OUTPUT_BASE       = Path(os.environ.get("REPORT_BASE") or (ROOT_DIR / "data" / "weekly-report"))
LOOKBACK_DAYS     = 2   # 격일(월·수·금) 생성 주기와 일치 — 변동률·뉴스가 '직전 영상 이후' 구간이 되어 영상 간 겹침 없음 (v1.0.32에서 3→2)
                        # (윈도우가 넓으면 격일 영상끼리 겹쳐 옛 내용 반복. 더 좁히려면 2, 빈 날 여유는 4~5)
RECENT_NEWS_DAYS  = 2   # 호재/악재 BEST 픽 — 당일~이 기간 이내 뉴스를 점수와 무관하게 우선
SCRIPT_REVIEW_ROUNDS = 2   # 대본 생성 후 자기 재검토·수정 반복 횟수 (어색한 문구·미래비전 전달력 점검)

# ── 팔레트 ────────────────────────────────────────────────────────────────
BG      = (24, 32, 54)         # 14,17,23 → 밝은 미드나이트 네이비
WHITE   = (255, 255, 255)
GRAY    = (120, 128, 148)
LGRAY   = (185, 192, 210)      # 더 밝은 회색
GREEN   = (34, 197, 94)
RED     = (239, 68, 68)
AMBER   = (245, 158, 11)
PURPLE  = (167, 139, 250)
CYAN    = (6, 182, 212)
BLUE    = (59, 130, 246)
W, H    = 1080, 1920

PAD     = 40
COL_W   = W - PAD
SAFE_BOTTOM = 1680
KEY     = (255, 215, 0)
STROKE  = (8, 12, 30)          # 0,0,0 → 부드러운 다크 네이비 (과한 검정 윤곽 완화)

HEADER_H    = 500
PHOTO_Y     = HEADER_H
PHOTO_H     = 500
BODY_Y      = PHOTO_Y + PHOTO_H
START_Y     = BODY_Y
NAVY        = (30, 60, 115)    # 15,32,70 → 밝은 네이비 블루
NAVY_DEEP   = (22, 45, 92)     # 10,22,50 → 밝은 딥 네이비
CYAN_LIGHT  = (160, 235, 255)  # 더 밝게

# ── 카드 배경색 (씬별 톤) ──────────────────────────────────────────────────
CARD_BG     = (36, 46, 78)     # 중립 카드 (was ~14-20 range)
CARD_GREEN  = (22, 58, 36)     # 초록 카드
CARD_RED    = (58, 24, 24)     # 빨강 카드
CARD_AMBER  = (58, 46, 16)     # 앰버 카드
CARD_PURPLE = (42, 20, 78)     # 보라 카드
BADGE_BG    = (20, 26, 48)     # 배지·푸터 배경

SCENE_ACCENTS = [PURPLE, GREEN, (236, 72, 153)]  # 브리핑/호재/미래비전 (인트로·시장반응 제거)

# ── 양산형 탈피: 영상마다 변형 (생성일 시드로 결정 → 격일 생성 시 매번 달라짐) ──
# 인트로/클로징(썸네일) 색상 테마 2~3종 로테이션. 씬1(호재)은 의미상 항상 초록 유지.
ACCENT_THEMES = [
    [(167, 139, 250), GREEN, (236, 72, 153)],  # A 보라·초록·마젠타 (기존)
    [(56, 189, 248),  GREEN, (251, 146, 60)],  # B 시안·초록·오렌지
    [(129, 140, 248), GREEN, (250, 204, 21)],  # C 인디고·초록·골드
]

def _theme_idx(date_str):
    """생성일 문자열로 결정적 테마 인덱스 (prep·make 동일 함수 → 색상 동기화)."""
    return sum(ord(c) for c in (date_str or "")) % len(ACCENT_THEMES)

# 오프닝 훅 스타일 풀 — 매 영상 다른 첫 줄로 '오늘의 뉴스' 식 고정 오프닝 탈피.
HOOK_STYLES = [
    "질문형 — 시청자에게 질문을 던지며 시작 (예: '최근 OO, 무슨 일이 있었을까요?')",
    "충격 수치형 — 최근 가장 큰 등락률·수치를 앞세워 강하게 시작",
    "역발상형 — 통념을 뒤집는 한마디로 시작 (예: '다들 걱정했지만, 의외로…')",
    "결론 선공개형 — 핵심 결론을 먼저 던지고 근거로 이어가기",
    "스토리·장면형 — 한 장면을 묘사하듯 몰입감 있게 시작",
    "비교형 — 경쟁사·지난주 대비로 대조를 주며 시작",
    "호기심 유발형 — '왜 갑자기?' 식으로 궁금증을 자극하며 시작",
    "임팩트형 — 최근 최대 이슈 한 방으로 훅을 걸며 시작",
]

def pick_hook(seed):
    return random.Random(str(seed)).choice(HOOK_STYLES)

# 씬0 배경용 — 서울 단일 묘사 탈피, 세계 주요 도시 실제 야경 랜드마크 로테이션 (생성일 시드)
WORLD_CAPITALS_NIGHT = [
    "뉴욕 맨해튼 스카이라인과 자유의 여신상",
    "런던 빅벤과 템스강, 런던아이",
    "도쿄 도쿄타워와 시부야 스카이라인",
    "파리 에펠탑과 세느강변",
    "두바이 부르즈할리파 스카이라인",
    "싱가포르 마리나베이샌즈와 가든스바이더베이",
    "시드니 오페라하우스와 하버브릿지",
    "서울 한강과 롯데타워 스카이라인",
]

# 씬2 배경용 — 각국이 꿈꾸는 미래도시 비전 로테이션 (생성일 시드, 씬0과 다른 솔트로 독립 분산)
WORLD_FUTURE_CITIES = [
    "아랍에미리트 두바이 네옴시티풍 미래 메가시티",
    "싱가포르 스마트네이션 미래 정원도시",
    "일본 도쿄 미래형 스마트시티",
    "한국 서울 미래형 스마트시티",
    "사우디아라비아 네옴 더라인 미래도시",
    "중국 상하이 미래형 초고층 스마트시티",
    "아랍에미리트 마스다르시티 친환경 미래도시",
    "북유럽풍 친환경 미래 스마트시티",
]

def pick_capital(seed):
    """씬0 배경 도시 — 생성일 시드로 결정적 선택(영상마다 다른 도시, 재실행 시 동일)."""
    return random.Random(str(seed) + "_capital").choice(WORLD_CAPITALS_NIGHT)

def pick_future_city(seed):
    """씬2 배경 미래도시 — 씬0과 다른 솔트로 독립 선택."""
    return random.Random(str(seed) + "_futurecity").choice(WORLD_FUTURE_CITIES)

SCENE_WIKI_ARTICLES = TICKER_CONFIG.get("scene_wiki_articles", [])   # 온디맨드 종목은 없을 수 있음
GOOGLE_TRENDS_KEYWORDS = TICKER_CONFIG.get("google_trends_keywords", [])

SCENE_BG_DIR = ROOT_DIR / "data" / "scene-backgrounds"
SCENE_STATIC_BG = [
    (SCENE_BG_DIR / name) if name else None
    for name in TICKER_CONFIG.get("scene_static_bg_files", [])
]

CALENDAR_JSON = ROOT_DIR / "data" / "calendar.json"

# ── 데이터 로드 ───────────────────────────────────────────────────────────

def load_week_sessions():
    if not AUTO_SESSIONS.exists():
        return []
    with open(AUTO_SESSIONS, encoding="utf-8") as f:
        raw = json.load(f)
    sessions = raw if isinstance(raw, list) else raw.get("sessions", [])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    return [s for s in sessions if s.get("date", "") >= cutoff]


def summarize(sessions):
    if not sessions:
        return None

    def _price(s):
        # 범용 필드(latestPrice) 우선, 구버전 alias(latestTslaPrice) 폴백
        return s.get("latestPrice") if s.get("latestPrice") is not None else s.get("latestTslaPrice")

    buy_indices = [s["buyIndex"] for s in sessions if s.get("buyIndex") is not None]
    prices      = [_price(s) for s in sessions if _price(s)]

    # 가장 최근 세션의 날짜 = "오늘"(이 파이프라인 실행일) 기준 — 호재 심층(씬1) 그라운딩용
    today_str = sessions[0].get("date", "")

    bullish, bearish, neutral = [], [], []
    total_analyzed = 0
    for s in sessions:
        news_map = {str(n["id"]): n for n in s.get("news", [])}
        for nid, a in (s.get("analyses") or {}).items():
            n     = news_map.get(str(nid), {})
            title = n.get("title", "")
            if not title:
                continue
            total_analyzed += 1
            score    = a.get("impact_score", 0) or 0
            dir_     = a.get("direction", "")
            reason   = a.get("reasoning", "")
            source   = n.get("source", "")
            date     = n.get("date", "")
            category = n.get("category", "")
            if dir_ == "bullish" and score >= 2:
                bullish.append({"title": title, "score": score, "reason": reason,
                                "source": source, "date": date, "category": category})
            elif dir_ == "bearish" and score <= -1:   # 경미한 리스크(-1)도 포착 (발사 지연·규제 등)
                bearish.append({"title": title, "score": score, "reason": reason,
                                "source": source, "date": date, "category": category})
            elif dir_ == "neutral":                    # 보합(중립) — 씬1 3종 중 하나
                neutral.append({"title": title, "score": score, "reason": reason,
                                "source": source, "date": date, "category": category})

    # ── 중복 뉴스 제거 (여러 세션에 같은 뉴스가 반복 등장) ──
    def _dedup(items):
        seen, out = {}, []
        for it in items:
            key = re.sub(r"[\s\W]+", "", it["title"]).lower()[:24]
            prev = seen.get(key)
            if prev is None:
                seen[key] = it
                out.append(it)
            elif abs(it["score"]) > abs(prev["score"]):
                out[out.index(prev)] = it   # 같은 뉴스면 점수 큰 쪽 유지
                seen[key] = it
        return out

    bullish = _dedup(bullish)
    bearish = _dedup(bearish)
    neutral = _dedup(neutral)

    # 그날의 뉴스 기준 — 기사 날짜가 분석 윈도우(LOOKBACK_DAYS)보다 명백히 오래된 기사는 BEST에서 제외.
    # auto-analysis가 옛 기사(예: 2024·2025년)를 최근 세션에 재수집해도 영상엔 들어가지 않게 한다
    # (세션 날짜는 최근이어도 기사 날짜가 옛것일 수 있어, 세션 윈도우만으로는 못 거른다).
    news_cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    def _is_fresh(d):
        return (not d) or (d[:10] >= news_cutoff)   # 날짜 없으면 보수적으로 통과, 명시적 옛 기사만 제외
    bullish_full, bearish_full, neutral_full = list(bullish), list(bearish), list(neutral)  # 씬1 폴백용 전체 보관
    bullish_fresh = [b for b in bullish if _is_fresh(b.get("date", ""))]
    bearish_fresh = [r for r in bearish if _is_fresh(r.get("date", ""))]
    neutral_fresh = [x for x in neutral if _is_fresh(x.get("date", ""))]
    bullish = bullish_fresh or bullish   # 호재 심층은 비면 안 되므로 신선분 없으면 전체로 폴백
    bearish = bearish_fresh              # 악재는 비어도 프롬프트가 구조적 리스크로 처리(씬0 원래 동작 유지)
    neutral = neutral_fresh or neutral

    # ── 당일(최신 세션) 수집 뉴스 — 씬1 호재 심층 그라운딩용 ──
    # "당일 수집"이라도 기사 발행일이 옛것(예: 재수집된 2024·2022년 기사)이면 제외해 씬1이
    # 옛 뉴스로 작성되는 #2 회귀를 막는다. 검증기(GENERATE_NEWS_MAX_AGE=7일)와 같은 "게시 가능
    # 신선도" 창을 적용 — 며칠 전 실제 최근 뉴스는 살리고 명백한 옛 기사만 거른다(없으면 빈 리스트→
    # 프롬프트가 '주요 호재'로 폴백). LOOKBACK_DAYS(2)보다 약간 넓게 잡아 BEST가 비어도 맥락은 제공.
    today_news_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    today_news, _seen_today_titles = [], set()
    for s in sessions:
        if s.get("date") != today_str:
            continue
        for n in s.get("news", []):
            t, nd = n.get("title", ""), n.get("date", "")
            if t and t not in _seen_today_titles and ((not nd) or nd[:10] >= today_news_cutoff):
                _seen_today_titles.add(t)
                today_news.append({"title": t, "category": n.get("category", ""),
                                   "source": n.get("source", ""), "date": nd})
    today_news.sort(key=lambda n: n.get("date", ""), reverse=True)   # 신선한 순

    # 호재/악재 BEST 픽 — 최근 뉴스(RECENT_NEWS_DAYS 이내)를 점수와 무관하게 최우선 노출.
    # 한 번 고득점 받은 옛 뉴스(대형 계약·마일스톤 등)가 매주 신선한 뉴스를 밀어내고
    # 반복 노출되던 문제를 막는다. 최근 뉴스가 없는 주엔 자연히 점수 순으로 폴백된다.
    def _days_ago(date_s):
        try:
            from datetime import datetime as _dt2
            return (_dt2.now() - _dt2.strptime(date_s[:10], "%Y-%m-%d")).days
        except Exception:
            return 999

    # 같은 신선도(오늘/최근) 안에서는 출처 신뢰도가 높은 뉴스를 우선, '출처 미확인'은 하위로 밀어
    # 영상 BEST(특히 씬1 호재)가 저신뢰 출처로 선정되지 않게 한다(신선도 우선 원칙은 유지).
    def _bull_sort_key(n):
        is_today  = 1 if n.get("date", "")[:10] == today_str else 0
        is_recent = 1 if _days_ago(n.get("date", "")) <= RECENT_NEWS_DAYS else 0
        cred = source_credibility_rank(n.get("source", ""))
        return (is_today, is_recent, cred, n.get("score", 0))

    def _bear_sort_key(n):
        is_today  = 1 if n.get("date", "")[:10] == today_str else 0
        is_recent = 1 if _days_ago(n.get("date", "")) <= RECENT_NEWS_DAYS else 0
        cred = source_credibility_rank(n.get("source", ""))
        return (is_today, is_recent, cred, -n.get("score", 0))   # 악재는 점수가 낮을수록(더 부정적) 우선

    bullish.sort(key=_bull_sort_key, reverse=True)
    bearish.sort(key=_bear_sort_key, reverse=True)

    # ── 씬1 3카드(호재/악재/보합) — 신뢰도 우선 선정 ──
    # 사용자 요청: "신뢰도 높은 뉴스를 선정". 씬0(top_bullish/top_bearish)은 신선도 우선 그대로 두고,
    # 씬1 트리오만 신뢰도(cred)를 1순위로 뽑는다. 동순위면 신선도(오늘/최근)·점수 순.
    def _cred_first_key(n, neg=False):
        cred = source_credibility_rank(n.get("source", ""))
        is_today  = 1 if n.get("date", "")[:10] == today_str else 0
        is_recent = 1 if _days_ago(n.get("date", "")) <= RECENT_NEWS_DAYS else 0
        sc = -n.get("score", 0) if neg else n.get("score", 0)
        return (cred, is_today, is_recent, sc)
    # 씬1 3카드는 신선도 3일 창이 너무 좁아(신뢰 호재가 바로 밖으로 밀림) 10일 창으로 넓혀
    # 신뢰 출처를 우선 확보한다. 단 창을 벗어난 옛 기사로 폴백하지 않는다 — 창 내에 없으면 None을
    # 반환해 렌더가 "최근 뚜렷한 OO 없음"으로 정직하게 처리(옛 2025년 악재가 끌려오던 문제 방지).
    SCENE1_WINDOW_DAYS = 10
    def _pick_cred(full, neg=False):
        pool = [n for n in full if _days_ago(n.get("date", "")) <= SCENE1_WINDOW_DAYS]
        if not pool:
            return None
        return sorted(pool, key=lambda n: _cred_first_key(n, neg), reverse=True)[0]
    scene1_news = {
        "bullish": _pick_cred(bullish_full),
        "bearish": _pick_cred(bearish_full, neg=True),
        "neutral": _pick_cred(neutral_full),
    }

    # 최근 5일 (date, price) 쌍 수집
    seen_dates = {}
    for s in sessions:
        date = s.get("date", "")
        price = _price(s)
        if date and price and date not in seen_dates:
            seen_dates[date] = price
    # 날짜 내림차순 정렬 후 최근 5일
    sorted_dates = sorted(seen_dates.keys(), reverse=True)[:5]
    daily_prices = [(d, seen_dates[d]) for d in sorted_dates]

    latest = sessions[0]

    # ── 인트로용: 오늘 vs 전일 변동률 ──
    today_change_pct = None
    if len(daily_prices) >= 2:
        try:
            today_p = float(daily_prices[0][1])
            prev_p  = float(daily_prices[1][1])
            if prev_p > 0:
                today_change_pct = round((today_p - prev_p) / prev_p * 100, 2)
        except (ValueError, TypeError):
            pass

    # ── 브리핑용: 분석 윈도우(기간 시작) 대비 변동률 ──
    week_change_pct = None
    try:
        p_start = float(prices[-1]) if prices else None
        p_end   = float(prices[0]) if prices else None
        if p_start and p_end and p_start > 0:
            week_change_pct = round((p_end - p_start) / p_start * 100, 2)
    except (ValueError, TypeError):
        pass

    # ── 인트로용: 이번주 가장 큰 영향 사건 ──
    biggest_impact = None
    bull_top = bullish[0] if bullish else None
    bear_top = bearish[0] if bearish else None
    if bull_top and bear_top:
        if abs(bull_top["score"]) >= abs(bear_top["score"]):
            biggest_impact = {**bull_top, "direction_ko": "호재", "emoji": "🚀"}
        else:
            biggest_impact = {**bear_top, "direction_ko": "악재", "emoji": "⚠"}
    elif bull_top:
        biggest_impact = {**bull_top, "direction_ko": "호재", "emoji": "🚀"}
    elif bear_top:
        biggest_impact = {**bear_top, "direction_ko": "악재", "emoji": "⚠"}

    avg_bi = round(sum(buy_indices) / len(buy_indices)) if buy_indices else None
    overall_signal = ("긍정" if avg_bi >= 65 else "중립" if avg_bi >= 45 else "신중") if avg_bi is not None else None

    # ── 규칙 엔진 트리거 집계 (대시보드 'topRules' 배지와 동일 출처 — config/rules.json name_ko) ──
    rule_counts = {}
    for s in sessions:
        for rid in (s.get("topRules") or []):
            rule_counts[rid] = rule_counts.get(rid, 0) + 1
    triggered_rules = []
    for rid, _cnt in sorted(rule_counts.items(), key=lambda kv: -kv[1]):
        meta = RULES_MAP.get(rid)
        if meta:
            triggered_rules.append({"id": rid, "name_ko": meta.get("name_ko", rid), "direction": meta.get("direction", "")})

    return {
        "week_start":      sessions[-1].get("date", ""),
        "week_end":        sessions[0].get("date", ""),
        "session_count":   len(sessions),
        "total_analyzed":  total_analyzed,
        "bullish_count":   len(bullish),
        "bearish_count":   len(bearish),
        "buy_indices":     buy_indices,
        "avg_buy_index":   avg_bi,
        "latest_buy_index": buy_indices[0] if buy_indices else None,
        "price_start":     prices[-1] if prices else None,
        "price_end":       prices[0]  if prices else None,
        "latest_price":    _price(latest),
        "today_price":     _price(latest),
        "today_change_pct": today_change_pct,
        "week_change_pct": week_change_pct,
        "biggest_impact":  biggest_impact,
        "top_bullish":     bullish[:5],
        "top_bearish":     bearish[:5],
        "top_neutral":     neutral[:5],
        "neutral_count":   len(neutral),
        "scene1_news":     scene1_news,   # 씬1 3카드(호재/악재/보합) 신뢰도 우선 선정
        "forecasts":       latest.get("dailyForecasts", [])[:5],
        "daily_prices":    daily_prices,
        "overall_signal":  overall_signal,
        "trends":          None,        # fetch_google_trends()로 채움
        "next_events":     [],          # load_next_events()로 채움
        "triggered_rules": triggered_rules[:6],
        "scoring_layers":  latest.get("scoringLayers") or {},
        "macro_ctx":       latest.get("macroCtx") or {},
        "today_date":      today_str,
        "today_news":      today_news[:8],
    }


def search_movement_reason(summary):
    """Gemini + Google Search grounding으로 이번주 주가 변동 원인 검색."""
    if not GEMINI_API_KEY:
        return None
    try:
        from google import genai
        from google.genai import types
        tcp  = summary.get("today_change_pct")
        sign = "+" if tcp and tcp >= 0 else ""
        direction = "상승" if tcp and tcp >= 0 else "하락"
        week_start = summary.get("week_start", "")
        week_end   = summary.get("week_end", "")
        price      = summary.get("latest_price", "")
        q = (
            f"{COMPANY_KO} {TICKER} 주가 {week_start}~{week_end} 기간 {direction} 주요 원인 분석. "
            f"현재 주가 {fmt_price(price) or price}, 변동률 {sign}{tcp}%. "
            f"검색 결과를 바탕으로 핵심 원인 2~3가지를 각 15자 이내 한국어로 작성. "
            f"형식: '원인1 / 원인2 / 원인3'"
        )
        client   = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=q,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
        text = response.text.strip()
        print(f"   🔍 주가 변동 원인: {text[:80]}")
        return text
    except Exception as e:
        print(f"   ⚠ 주가 변동 원인 검색 실패: {e}", file=sys.stderr)
        return None


def search_company_direction():
    """Gemini + Google Search grounding으로 회사가 추구하는 방향·최근 투자를 검색.

    씬0 '회사가 추구하는 방향' 소재가 빈약하지 않게 구체 사실(투자 금액·신제품·로드맵)을
    확보한다(사용자 요청). 실패해도 파이프라인 계속 — 대본 프롬프트가 모델 지식으로 채움."""
    if not GEMINI_API_KEY:
        return None
    try:
        from google import genai
        from google.genai import types
        q = (
            f"{COMPANY_KO}({TICKER}, {INDUSTRY_KO}) 회사가 추구하는 방향을 조사해줘. "
            f"우선순위: ① 회사의 비전·전략 방향 ② 최근 1년 내 주요 투자·설비·인수(금액 등 구체 수치) "
            f"③ 핵심 신제품·신기술 로드맵 ④ 시장 내 위치·점유율. "
            f"검색 결과 기반 사실만, 각 20자 내외 한국어 구절 3~4개를 '/'로 구분해 한 줄로. "
            f"예: 'AI 메모리 1위 전략 / 20조 원 파운드리 투자 / HBM4 내년 양산'"
        )
        client   = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=q,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
        text = response.text.strip()
        print(f"   🧭 회사 방향·투자: {text[:80]}")
        return text
    except Exception as e:
        print(f"   ⚠ 회사 방향 검색 실패: {e}", file=sys.stderr)
        return None


def fetch_google_trends(keywords, days=7):
    """지난 7일 vs 직전 7일 검색량 비교 → 증감비율 + 최고 키워드."""
    if not keywords:
        return None
    try:
        from pytrends.request import TrendReq
    except ImportError:
        print("   ⚠ pytrends 미설치 — Google Trends 건너뜀", file=sys.stderr)
        return None
    try:
        py = TrendReq(hl='ko-KR', tz=540, timeout=(5, 15))
        py.build_payload(keywords[:5], timeframe=f'now {days*2}-d', geo='KR')
        df = py.interest_over_time()
        if df.empty:
            return None
        kw_cols = [k for k in keywords if k in df.columns]
        if not kw_cols:
            return None
        half = len(df) // 2
        if half < 1:
            return None
        recent = float(df.iloc[half:][kw_cols].mean().mean())
        prev   = float(df.iloc[:half][kw_cols].mean().mean())
        ratio  = round(recent / max(prev, 1), 1)
        top_kw = df[kw_cols].iloc[half:].mean().idxmax()
        return {
            "ratio": ratio,
            "top_keyword": str(top_kw),
            "recent_avg": round(recent),
        }
    except Exception as e:
        print(f"   ⚠ Google Trends 실패: {e}", file=sys.stderr)
        return None


def load_next_events(days=14, max_n=3):
    """calendar.json에서 향후 N일 내 high/medium importance 이벤트 추출."""
    if not CALENDAR_JSON.exists():
        return []
    try:
        with open(CALENDAR_JSON, encoding="utf-8") as f:
            raw = json.load(f)
        events = raw if isinstance(raw, list) else raw.get("events", [])
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cutoff = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
        upcoming = [
            e for e in events
            if today < e.get("date", "") <= cutoff
        ]
        # importance: high > medium > low, 그리고 빠른 날짜 우선
        importance_rank = {"high": 0, "medium": 1, "low": 2}
        upcoming.sort(key=lambda e: (
            importance_rank.get(e.get("importance", "low"), 3),
            e.get("date", ""),
        ))
        return upcoming[:max_n]
    except Exception as e:
        print(f"   ⚠ calendar.json 로드 실패: {e}", file=sys.stderr)
        return []


def build_next_week_outlook(forecasts):
    """dailyForecasts(일별 가격 예측)를 '다음주 전망' 한 단락으로 요약.

    YouTube 정책상 매수/매도 같은 신호 단어는 제외하고
    가격·변동률 추세만 참고용으로 정리한다.
    change_pct는 '현재가 대비' 누적 예측치(일별 증감 아님).
    """
    if not forecasts:
        return "예측 데이터 없음 — 다음주 일정·이벤트 중심으로 전망"

    def _pct(f):
        try:
            return float(f.get("change_pct"))
        except (TypeError, ValueError):
            return 0.0

    up   = sum(1 for f in forecasts if _pct(f) > 0)
    down = sum(1 for f in forecasts if _pct(f) < 0)

    cum = end = None
    try:
        base = float(forecasts[0].get("basePrice"))
        end  = float(forecasts[-1].get("predictedPrice"))
        if base > 0:
            cum = round((end - base) / base * 100, 1)
    except (TypeError, ValueError, AttributeError):
        pass

    parts = []
    if cum is not None:
        sign = "+" if cum >= 0 else ""
        parts.append(f"다음 주말 예상 변동률 {sign}{cum}% (현재가 대비)")
    if end:
        parts.append(f"예상 도달가 약 {fmt_price(end, 0)}")
    parts.append(f"현재가보다 높게 예측된 날 {up}일 / 낮은 날 {down}일")

    daily = " → ".join(
        f"{f.get('label') or f.get('date','')} {fmt_price(f.get('predictedPrice'), 0)}"
        for f in forecasts if f.get("predictedPrice")
    )
    if daily:
        return "; ".join(parts) + f"\n  일별 예측가: {daily}"
    return "; ".join(parts)


def fetch_prev_day_ohlc(ticker):
    """Yahoo 일봉에서 가장 최근 완료된 거래일의 시가·종가를 가져온다 (씬0 '전일 시가→종가'용).

    영상 생성 시각(KST 새벽 05:15/07:15)은 미국 정규장 마감 직후라 마지막 일봉이 곧
    '전일(간밤)' 장이다. 변동률은 그 직전 거래일 종가 대비(통상적 '전일 대비').
    세션 latestPrice(KST 03:00 = 미국 장중 스냅숏)와 달리 확정 시가·종가다.
    실패 시 None — 씬0에서 해당 표기를 생략하고 파이프라인은 계속 진행(선택적 데이터).
    """
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?range=7d&interval=1d"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r)
            res    = data["chart"]["result"][0]
            ts     = res.get("timestamp") or []
            quote  = res["indicators"]["quote"][0]
            opens  = quote.get("open") or []
            closes = quote.get("close") or []
            offset = res.get("meta", {}).get("gmtoffset", 0) or 0
            bars = []
            for i, t in enumerate(ts):
                o = opens[i] if i < len(opens) else None
                c = closes[i] if i < len(closes) else None
                if o is None or c is None:
                    continue
                d = datetime.fromtimestamp(t + offset, tz=timezone.utc).strftime("%Y-%m-%d")
                bars.append({"date": d, "open": round(float(o), 2), "close": round(float(c), 2)})
            if not bars:
                continue
            prev = bars[-1]
            pct = None
            if len(bars) >= 2 and bars[-2]["close"]:
                pct = round((prev["close"] - bars[-2]["close"]) / bars[-2]["close"] * 100, 2)
            return {**prev, "change_pct": pct}
        except Exception as e:
            print(f"   ⚠ Yahoo 일봉({host}) 실패: {e}")
    return None

# ── 대본 생성 ─────────────────────────────────────────────────────────────

SCRIPT_PROMPT_TEMPLATE = """아래 {ticker} 최근 데이터를 바탕으로 YouTube Shorts 나레이션 대본을 작성해줘.
**친근한 사람이 옆에서 다정하게 이야기해 주는 톤**으로, 구독자에게 말 걸듯 따뜻하고 자연스러운 구어체로 작성한다.

=== 종목 사실 (고정·반드시 준수) ===
• {grounding}

=== 톤 가이드 (반드시 준수) ===
• 친근한 구어체 어미 사용: "~예요", "~네요", "~더라고요", "~거든요", "~답니다", "~죠", "~봐요", "~해요"
• 다정하게 말 걸기: "여러분", "같이 볼까요?", "~한 점이 눈에 띄네요", "흥미롭죠?" 처럼 대화하듯 자연스럽게
• 딱딱한 분석체 어미 금지: "~로 분석된다", "~로 관측된다", "~할 전망이다", "~로 풀이된다" 같은 보고서 말투는 쓰지 않는다 — 사람이 말하듯 풀어 쓴다
• 과한 클릭베이트 추임새는 지양(충격!·헐!·대박!·소름! 금지)하되, 부드럽고 자연스러운 반응은 환영("좋은 소식이에요", "조금 아쉬운 부분이죠", "눈여겨볼 만해요")
• 단정적 권유 금지 (매수·매도·관망 직접 언급 금지)
• **내부 점수(+N점·-N점) 절대 표기 금지** — 시청자용 지표가 아니다. "좋은 소식"·"호재" / "걱정되는 부분"·"리스크"처럼 풀어 말하고, 점수 대신 구체적 수치·배경·맥락으로 왜 그런지 설명한다.
• 수치·근거는 그대로 살린다: 모든 핵심 줄에 %·$·대수 등 구체 수치를 자연스럽게 녹여 넣는다
• 씬 0: 4줄(각 30자 이내) / 씬 1: 3줄(호재·악재·보합 각 1줄, 40~55자, "호재:/악재:/보합:" 접두어 필수) / 씬 2: 6줄(각 45자 이내, 줄1~5는 헤드라인체로 딱 잘라 맺기·줄6 마무리만 다정하게)

=== 핵심 강조 표시 (반드시 준수) ===
• 각 줄에서 가장 중요한 핵심 글귀(수치·키워드) 1개를 *별표*로 감싼다. 예시: 최근 주가가 *12% 급등*했어요
• 한 줄에 강조는 최대 1~2개만. 문장 전체를 감싸지 말고 핵심 수치/단어만 감싼다
• 별표로 감싼 부분은 화면에서 강조색(골드)으로 표시되니, 정말 눈에 띄어야 할 수치·키워드에만 사용한다

=== 오프닝 훅 & 차별화 (반드시 준수) ===
• 오프닝(SCENE_0_TITLE·씬0 줄1)은 매 영상 달라야 한다. 이번 영상 오프닝 훅 스타일 → {hook_style}
  ※ "오늘의 뉴스"·"이번주 뉴스 N건 분석했어요" 같은 고정·상투적 오프닝 금지(분석 규모는 뒷줄에 자연스럽게 녹여도 됨).
• 차별화 관점 1줄(필수): 단순 뉴스 요약·낭독을 넘어, 시장 컨센서스·통념과 다른 분석가만의 시각을 한 줄 넣는다
  (예: "시장은 X를 우려하지만, 정작 중요한 건 Y예요"). 씬1 '향후 전망' 또는 씬2에 자연스럽게 배치.

=== 분석 데이터 ({week_start} ~ {week_end}) ===
- {ticker} 현재 주가: {price}
- 기간 대비 변동률: {week_change_pct_str}
- 분석 규모: {analysis_scale_str}
- 매수 참고지수 추이: {buy_index_trend_str}
- 다층 분석 모델 감지 신호 (구조적 트리거, 맥락 설명에만 참고·점수·점수표현 절대 금지): {rules_str}
- 점수 구성 요인 동향 (참고용, 점수·숫자 절대 금지·"~한 신호가 있었다" 식 맥락 설명에만 활용): {scoring_str}
- 경쟁사({competitor_ticker}) 비교: {competitor_str}
- 기술적 지표(참고용, 자연스럽게 수치 인용 가능): {tech_str}
- 주가 변동 원인: {movement_reason_str}
- 회사 방향·최근 투자 (검색 결과 — 씬0 줄2~5 소재로 우선 활용): {company_direction_str}
- 검색량 트렌드: {trends_str}
- 다음주 예정 이벤트: {next_events_str}
- 다음주 가격 예측(AI 모델, 참고용·매매신호 아님): {next_week_str}
{daily_prices_txt}
- 오늘({today_date}) 실제 수집된 뉴스 전체 목록 (씬1은 반드시 이 안에서만 작성 — 목록 밖 내용 추측 금지): {today_news_str}
- 주요 호재 (점수 표기 금지, 내용만 활용):
{b_txt}
- 주요 리스크 (점수 표기 금지, 내용만 활용):
{r_txt}
{risk_guidance}
- 씬1 선정 뉴스 (신뢰도 우선 선정 — 씬1은 반드시 이 3건만 사용, 출처가 이미 검증됨):
{scene1_str}

=== 씬 구성 (총 3씬) ===

【씬 0 — 회사 소개 & 간략한 주가 흐름】 (5줄, 한 줄 30자 이내, 핵심만 응축)
※ 이 씬은 '주가 분석'이 아니라 **회사가 어떤 방향을 추구하는지**를 알차게 소개한다. 주가는 줄1에서 간단히만 짚고, 변동 원인 심층 분석은 하지 않는다(뉴스 분석은 씬1 담당).
※ 줄2~5 소재는 아래 '회사 방향·최근 투자 (검색 결과)'를 우선 활용하고, 부족하면 ① 추구하는 방향·비전 ② 최근 투자·설비·인수(구체 금액) ③ 핵심 신제품·기술 로드맵 ④ 시장 내 위치 순으로 아는 사실을 채운다 — 4줄이 전부 비지 않게 반드시 채울 것.
- 줄1: 위 '오프닝 훅 스타일'로 시작 — 현재 주가와 기간 변동률을 **간결하게** 녹인 한 줄 (간략한 주가 흐름, 30자 이내, 수치 필수). 상세한 변동 원인은 설명하지 말 것
- 줄2: 이 회사가 **추구하는 방향·비전** 핵심 한 줄 — 무슨 사업으로 어디를 향하는지 ({industry_ko} 산업, 30자 이내). 주가 얘기 금지
- 줄3: **최근 투자·신사업** 한 줄 — 설비 투자·인수·증설 등 구체 수치 포함 (검색 결과 활용, 30자 이내). 주가 얘기 금지
- 줄4: **핵심 제품·기술** 한 줄 — 대표 제품/기술({future_tech} 참고)과 그 의미 (30자 이내). 주가 얘기 금지
- 줄5: **시장 내 위치·강점** 한 줄 — 점유율·순위·경쟁 우위 등 (30자 이내). 주가 얘기 금지

【씬 1 — 핵심 뉴스 3선 (호재·악재·보합)】 (정확히 3줄 — 호재 1줄, 악재 1줄, 보합 1줄)
※ 위 "씬1 선정 뉴스(신뢰도 우선)"로 제공된 3건(호재·악재·보합)을 그대로 사용해 각각 자연스러운 구어체 한 문장으로 전한다.
  제공된 3건 밖의 내용을 지어내지 말 것 — 출처가 신뢰할 만한 뉴스로 이미 선정돼 있다. 각 문장에 핵심 수치를 녹인다.
※ 각 줄은 반드시 "호재:"·"악재:"·"보합:" 접두어로 시작한다(자막·파서가 카드 구분에 사용 — 접두어 생략 금지).
- 줄1: "호재: ..." — 제공된 호재 뉴스를 한 문장으로 (40~55자, 수치 포함). 호재가 "없음"이면 "호재: 최근 뚜렷한 호재는 없었어요"
- 줄2: "악재: ..." — 제공된 악재 뉴스를 한 문장으로 (40~55자, 수치 포함). 악재가 "없음"이면 "악재: 최근 뚜렷한 악재는 없었어요"(안전하다고 단정하지 말 것)
- 줄3: "보합: ..." — 제공된 보합(중립) 뉴스를 한 문장으로 (40~55자, 수치 포함). 보합이 "없음"이면 "보합: 최근 뚜렷한 중립 이슈는 없었어요"

【씬 2 — 다음주 전망 (클로징)】 (6줄, 다음주 예측 중심·수치 의무 — 간결하고 "딱 잘라지는" 헤드라인 어투)
※ 마지막 씬. 줄1~5는 신문 헤드라인처럼 **짧고 단호하게 끊어** 쓴다 — "~예요/~니다/~돼요/~해요/~져요" 같은 긴 서술 어미를 쓰지 말고 **체언(명사)·명사형("전망/예상/관측/관건/변수/주목")으로 딱 잘라** 맺는다. 단 구체 수치·이름·근거는 그대로 넣어 알차게(줄당 30~45자, 화면 2줄까지). 줄6(마무리)만 예외로 따뜻하게.
- 줄1: 다음주 핵심 일정·이벤트 1건 — next_events 활용, 날짜/이름 명시 (예: "7월 2일 2분기 실적 발표 — 최대 분수령")
- 줄2: 그 이벤트 관전 포인트 — 수치·근거 (예: "매출 컨센서스 *1억 3천만 달러* 상회가 관건")
- 줄3: 다음주 가격 흐름 예측 요약 — 누적 변동률·도달가, 단정 금지하되 명사형으로("~돼요" 금지, "전망/예상"으로 맺기) (예: "누적 *+3% 안팎*, 완만한 상승 전망")
- 줄4: 상승/하락 예측 흐름 부연 — 수치 (예: "5거래일 중 *3일 상승·2일 하락* 예상")
- 줄5: 신중히 볼 변수 1건 — 근거와 함께 (예: "변수: 뉴트론 개발 일정 지연 우려")
- 줄6: 따뜻한 마무리 인사 한 문장 — 이 줄만 예외로 짧고 다정하게 ("또 봐요!", "다음에 또 만나요", "함께해 주셔서 감사해요" 등, "다음 주" 표현 금지, 20자 이내)

=== 출력 형식 (반드시 준수) ===
※ 핵심 수치·키워드는 *별표*로 감싸 강조한다 (각 줄 최대 1~2개).
SCENE_0_TITLE: [6자 이내, 친근한 단어 예: "회사소개" "어떤회사"]
SCENE_0:
[줄1 — 현재 주가·기간 변동률 간결 요약, 핵심 수치 *별표* 강조]
[줄2 — 회사가 추구하는 방향·비전, 핵심 키워드 *별표* 강조]
[줄3 — 최근 투자·신사업 (구체 수치), 핵심 *별표* 강조]
[줄4 — 핵심 제품·기술, 핵심 키워드 *별표* 강조]
[줄5 — 시장 내 위치·강점, 핵심 키워드 *별표* 강조]

SCENE_1_TITLE: [6자 이내, 예: "핵심뉴스" "3대뉴스"]
SCENE_1:
호재: 선정된 호재 뉴스 한 문장 (핵심 수치 *별표* 강조)
악재: 선정된 악재 뉴스 한 문장 (핵심 수치 *별표* 강조, 없으면 "최근 뚜렷한 악재는 없었어요")
보합: 선정된 보합 뉴스 한 문장 (핵심 수치 *별표* 강조)

SCENE_2_TITLE: [6자 이내, "다음주" "전망" 같은 단어]
SCENE_2:
[줄1 — 다음주 핵심 일정·이벤트, 핵심 *별표* 강조]
[줄2 — → 예상 시나리오·관전 포인트, *별표* 강조]
[줄3 — 다음주 가격 흐름 예측 요약, 누적 변동률·도달가 *별표* 강조]
[줄4 — → 상승/하락 예측 흐름 부연, 수치 *별표* 강조]
[줄5 — 신중히 볼 변수 1건]
[줄6 — 따뜻한 마무리 인사]

=== 배경 이미지 프롬프트 (Gemini Imagen용, 영어, 3개) ===
각 60단어 이상. 반드시 포함: "no text, no letters, no watermark, no logo", "ultra-high resolution".
{company_ko}·{industry_ko} 관련 시각 요소 포함. 씬별 색감 지정.
★ 각 이미지에 {company_ko}의 미래 기술·사업계획을 시각적으로 반영하라(핵심 제품/로드맵): {future_tech}.
※ 씬 0·1은 16:9 landscape (horizontal strip), 씬 2는 9:16 vertical (full screen) — 프롬프트에 비율 명시.

IMAGE_PROMPT_0: [씬0 — 16:9 landscape · {world_capital} 야경 배경 + {company_ko} {industry_ko}가 추구하는 방향성이 느껴지는 핵심 비주얼, {future_tech}, 글로벌 첨단 도시 스카이라인, 보라빛 미래적 분석 분위기, futuristic global city night skyline purple violet tech analytics, glowing city lights bokeh, ultra-high resolution, 16:9 landscape, no text, no letters, no watermark, no logo]
IMAGE_PROMPT_1: [씬1 — 16:9 landscape · {future_tech} 비전이 담긴 항공우주 발전 상상도, {company_ko} {industry_ko}의 방향성이 보여지는 역동적 비주얼, 로켓 발사·인공위성 궤도·우주 인프라가 어우러진 미래 지향적 장면, 초록빛 성장 상승 에너지 분위기, futuristic aerospace launch vivid green growth bullish energy, dynamic upward trajectory, glowing motion lines, ultra-high resolution, 16:9 landscape, no text, no letters, no watermark, no logo]
IMAGE_PROMPT_2: [씬2 — 9:16 vertical · {world_future_city} 비전 배경 + {company_ko} {industry_ko} 상징 비주얼 + 미래 비전({future_tech}), 각국이 꿈꾸는 미래도시 풍경, 골드빛 영감적인 미래 무드, 황금빛 태양·별빛·반짝임, ultra-high resolution, 9:16 vertical, no text, no letters, no watermark, no logo]"""


SCRIPT_REVIEW_PROMPT_TEMPLATE = """아래는 방금 생성한 {ticker}({company_ko}) 유튜브 쇼츠 나레이션 대본이다.
편집자 입장에서 비판적으로 재검토하고, 문제가 있는 줄만 직접 고쳐서 전체를 동일한 형식으로 다시 출력해라.

=== 점검 기준 (이 2가지를 줄 단위로 점검) ===
1. 어색한 문구: 번역체·문어체·딱딱한 보고서 말투·부자연스러운 어순·같은 표현 반복이 있으면
   다정한 구어체(~예요/~네요/~거든요/~죠)로 자연스럽게 다듬는다.
2. 미래 비전 전달력: {company_ko}의 미래 기술·사업계획({future_tech})이 씬1 '향후 전망' 줄과
   씬2 '관전 포인트' 줄에서 막연하거나 추상적이면, 구체적인 제품·로드맵 이미지가 떠오르게 보강한다.
   (수치·형식·글자 수 제한은 절대 깨지 않는 선에서 단어만 더 생동감 있게 교체)

=== 반드시 지킬 것 ===
• 출력 형식(SCENE_*_TITLE/SCENE_*/IMAGE_PROMPT_*)·씬별 줄 수·줄당 글자 수 제한·*별표* 강조 표기·
  내부 점수 미표기 규칙은 원본과 동일하게 유지한다.
• IMAGE_PROMPT_0~2는 원본 그대로 한 글자도 바꾸지 않고 그대로 옮긴다.
• 이미 자연스럽고 생동감 있는 줄은 건드리지 않는다 — 트집을 잡기 위한 불필요한 재작성 금지.
• 고친 내용이 없다면 원본을 그대로 출력해도 된다.

=== 원본 대본 ===
{raw_script}

=== 출력 ===
설명·코멘트 없이, 재검토를 마친 최종 대본 전체(IMAGE_PROMPT_0~2 포함)만 원본과 동일한 형식으로 출력해라."""


def _build_prompt(summary):
    # 내부 점수([+N])는 시청자용이 아니므로 프롬프트 데이터에서도 노출하지 않는다 (AI 에코 방지).
    b_txt = "\n".join(
        f"  - {n['title']} ({n.get('source','')}·{n.get('date','')}·{n.get('category','')}): {n['reason'][:70]}"
        for n in summary["top_bullish"]
    ) or "  없음"
    r_txt = "\n".join(f"  - {n['title']}: {n['reason'][:70]}" for n in summary["top_bearish"]) or "  없음"

    daily_prices = summary.get("daily_prices", [])
    if daily_prices:
        dp_lines = "\n".join(f"  {d}: {fmt_price(p)}" for d, p in daily_prices)
        daily_prices_txt = f"- 최근 주가 흐름:\n{dp_lines}"
    else:
        daily_prices_txt = ""
    # 전일 확정 시가·종가 — 씬0 이미지 스트립과 나레이션이 어긋나지 않게 프롬프트에도 제공
    prev_day = summary.get("prev_day")
    if prev_day:
        _pdp = prev_day.get("change_pct")
        _pdp_s = f" (전일 대비 {_pdp:+.2f}%)" if _pdp is not None else ""
        daily_prices_txt += (f"\n- 전일({prev_day['date']}) 확정: "
                             f"시가 {fmt_price(prev_day['open'])} → 종가 {fmt_price(prev_day['close'])}{_pdp_s}")

    # 브리핑용 변동률 문자열 (분석 기간 시작 대비)
    wcp = summary.get("week_change_pct")
    if wcp is not None:
        sign = "+" if wcp >= 0 else ""
        week_change_pct_str = f"{sign}{wcp}% (기간 대비)"
    else:
        week_change_pct_str = "변동 데이터 없음"

    # Google Trends
    trends = summary.get("trends")
    if trends:
        trends_str = f"검색량 {trends['ratio']}배 변화 (최고 키워드: {trends['top_keyword']})"
    else:
        trends_str = "데이터 없음"

    # 다음주 이벤트
    next_events = summary.get("next_events", [])
    if next_events:
        next_events_str = "; ".join(
            f"{e.get('date', '')} {e.get('title', '')}" for e in next_events
        )
    else:
        next_events_str = "예정 이벤트 없음 (실적 발표·신제품 발표 등 일반 모니터링)"

    movement_reason = summary.get("movement_reason")
    movement_reason_str = movement_reason if movement_reason else "데이터 수집 중"

    company_direction = summary.get("company_direction")
    company_direction_str = company_direction if company_direction else \
        "(검색 실패 — 아는 사실 기반으로 이 회사의 비전·최근 투자·핵심 제품을 채워 넣을 것)"

    # 다음주 가격 예측 요약 (dailyForecasts 기반, 매매신호 단어 제외)
    next_week_str = build_next_week_outlook(summary.get("forecasts", []))

    # ── 분석 규모 (영상에 "N건 분석" 신뢰감 부여) ──
    total = summary.get("total_analyzed", 0)
    bull_n = summary.get("bullish_count", 0)
    bear_n = summary.get("bearish_count", 0)
    if total:
        analysis_scale_str = (
            f"총 {total}건 뉴스 분석 (호재 {bull_n}건 · 리스크 {bear_n}건), "
            f"{summary.get('session_count', 0)}개 세션 종합"
        )
    else:
        analysis_scale_str = "분석 데이터 수집 중"

    # ── 매수 참고지수 추이 (예: 76 → 72 → 84) ──
    bis = summary.get("buy_indices") or []
    if bis:
        # buy_indices는 최신순이므로 시간순(과거→현재)으로 뒤집어 표시
        trend = " → ".join(str(b) for b in reversed(bis))
        buy_index_trend_str = (
            f"{trend} (평균 {summary.get('avg_buy_index')}, 시그널 {summary.get('overall_signal','')})"
        )
    else:
        buy_index_trend_str = "데이터 없음"

    # ── 다층 분석 모델 감지 신호 (대시보드 'topRules' 배지 — config/rules.json name_ko로 번역, 점수는 절대 노출 안 함) ──
    rules = summary.get("triggered_rules") or []
    if rules:
        rules_str = ", ".join(
            f"{r['name_ko']}({'호재' if r['direction']=='bullish' else '악재' if r['direction']=='bearish' else '중립'})"
            for r in rules
        )
    else:
        rules_str = "특이 트리거 없음"

    # ── 점수 구성 요인 (대시보드 '매수지수 분해' scoringLayers — 숫자 대신 강도·방향만 질적으로 표현) ──
    def _magnitude(v):
        av = abs(v)
        return "강하게" if av >= 8 else "다소" if av >= 3 else None

    sl = summary.get("scoring_layers") or {}
    sl_parts = []
    for k, v in sl.items():
        if k == "base" or not isinstance(v, (int, float)):
            continue
        mag = _magnitude(v)
        if not mag:
            continue
        label = SCORING_LAYER_LABELS.get(k, k)
        sl_parts.append(f"{label} {mag} {'긍정적' if v > 0 else '부정적'}으로 작용")
    scoring_str = ", ".join(sl_parts) if sl_parts else "특이 요인 없음"

    # ── 경쟁사 비교·기술적 지표 (대시보드 macroCtx — 씬1 '비교' 줄 그라운딩용) ──
    mc = summary.get("macro_ctx") or {}
    competitor_ticker = COMPETITOR_TICKER or "경쟁사"
    comp_chg, comp_rel = mc.get("competitorChg"), mc.get("competitorRelStrength")
    if isinstance(comp_chg, (int, float)) and isinstance(comp_rel, (int, float)):
        competitor_str = (
            f"{competitor_ticker} 등락률 {'+' if comp_chg >= 0 else ''}{comp_chg}%, "
            f"{TICKER} 상대강도 {'+' if comp_rel >= 0 else ''}{comp_rel}%p"
        )
    else:
        competitor_str = "데이터 없음"

    rsi = mc.get("rsi")
    tech_str = f"RSI {rsi:.1f}" if isinstance(rsi, (int, float)) else "데이터 없음"

    # ── 오늘 실제 수집된 뉴스 전체 목록 (씬1을 이 안에서만 작성하도록 그라운딩 — 옛 뉴스 재활용 방지) ──
    today_news = summary.get("today_news") or []
    today_news_str = (
        "; ".join(f"{(n.get('date','') or '')[:10]} {n['title']}({n.get('category','')})".strip() for n in today_news)
        if today_news else "최근(7일 내) 수집 뉴스 없음 — 아래 '주요 호재/리스크' 목록 활용"
    )
    today_date = summary.get("today_date", "")

    # ── 씬1 3종(호재/악재/보합) 그라운딩 — summarize()가 신뢰도 우선으로 선정한 3건 ──
    _s1 = summary.get("scene1_news") or {}
    def _s1line(key, ko):
        n = _s1.get(key)
        if not n:
            return f"  - {ko}: (최근 뚜렷한 {ko} 없음 — '없었어요'라고 정직하게, 단정적 표현은 피해 작성)"
        src = n.get("source", ""); cred = source_credibility_tag(src)
        return (f"  - {ko}: {n.get('title','')} "
                f"({src}·{cred}·{(n.get('date','') or '')[:10]}) — {n.get('reason','')[:60]}")
    scene1_str = "\n".join([_s1line("bullish", "호재"), _s1line("bearish", "악재"), _s1line("neutral", "보합")])

    # ── 리스크 가이던스: 뚜렷한 악재가 없을 때 구조적 리스크로 보완 ──
    if bear_n == 0:
        risk_guidance = (
            f"- 주의 깊게 볼 변수 (뚜렷한 악재가 없을 때 활용): "
            f"{INDUSTRY_KO} 산업 특유의 고변동성, 단기 급등에 따른 밸류에이션 부담, "
            f"발사·생산 일정 지연 가능성, 거시 금리·우주 섹터 투자심리 변화"
        )
    else:
        risk_guidance = ""

    # ── 종목 정체성 그라운딩 (AI가 상장사를 비상장으로 오인하는 것 방지) ──
    # 뉴스에 비상장 동종업체(IPO 임박 기업 등)가 섞여 있어 혼동 위험이 있으므로 사실을 고정한다.
    listing = f"{EXCHANGE} 상장 " if EXCHANGE else "증권거래소 상장 "
    grounding = (
        f"{COMPANY_KO}({COMPANY_EN}, 티커 {TICKER})는 {listing}{INDUSTRY_KO} 기업으로 "
        f"실제 주식시장에서 거래되는 상장사다. "
        f"절대 비상장·미상장·IPO 예정·스타트업으로 묘사하지 말 것 — "
        f"뉴스에 함께 등장하는 비상장 동종업체(IPO 임박 기업 등)와 혼동 금지."
    )

    _seed = summary.get("week_end") or summary.get("week_start") or ""
    hook_style = pick_hook(_seed)
    world_capital = pick_capital(_seed)
    world_future_city = pick_future_city(_seed)
    return SCRIPT_PROMPT_TEMPLATE.format(
        ticker=TICKER,
        company_ko=COMPANY_KO,
        industry_ko=INDUSTRY_KO,
        grounding=grounding,
        future_tech=FUTURE_TECH_EN,
        hook_style=hook_style,
        world_capital=world_capital,
        world_future_city=world_future_city,
        week_start=summary["week_start"],
        week_end=summary["week_end"],
        price=fmt_price(summary["latest_price"]) or summary["latest_price"],
        b_txt=b_txt, r_txt=r_txt,
        daily_prices_txt=daily_prices_txt,
        week_change_pct_str=week_change_pct_str,
        movement_reason_str=movement_reason_str,
        company_direction_str=company_direction_str,
        trends_str=trends_str,
        next_events_str=next_events_str,
        next_week_str=next_week_str,
        analysis_scale_str=analysis_scale_str,
        buy_index_trend_str=buy_index_trend_str,
        risk_guidance=risk_guidance,
        rules_str=rules_str,
        scoring_str=scoring_str,
        competitor_ticker=competitor_ticker,
        competitor_str=competitor_str,
        tech_str=tech_str,
        today_date=today_date,
        today_news_str=today_news_str,
        scene1_str=scene1_str,
    )


def generate_script_opus(prompt):
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=3072,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def generate_script_gemini(prompt):
    from google import genai
    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text


_last_model = "AI"

_REQUIRED_SCRIPT_MARKERS = (
    [f"SCENE_{i}_TITLE:" for i in range(3)]
    + [f"SCENE_{i}:" for i in range(3)]
    + [f"IMAGE_PROMPT_{i}:" for i in range(3)]
)


def _has_required_script_markers(raw):
    return bool(raw) and all(m in raw for m in _REQUIRED_SCRIPT_MARKERS)


def review_script(raw):
    """생성된 대본을 비판적으로 재검토해 어색한 문구·미래비전 전달력을 보강한다.
    실패 시(또는 형식이 깨지면) 호출 측에서 원본을 그대로 유지하도록 raw를 반환한다."""
    prompt = SCRIPT_REVIEW_PROMPT_TEMPLATE.format(
        ticker=TICKER, company_ko=COMPANY_KO, future_tech=FUTURE_TECH_EN, raw_script=raw,
    )
    if ANTHROPIC_API_KEY:
        try:
            return generate_script_opus(prompt)
        except Exception as e:
            print(f"   ⚠ 재검토(Opus) 실패 ({e}) — Gemini로 전환", file=sys.stderr)
    if GEMINI_API_KEY:
        return generate_script_gemini(prompt)
    return raw


def generate_script(summary):
    global _last_model
    prompt = _build_prompt(summary)
    result = None
    if ANTHROPIC_API_KEY:
        try:
            print("   🤖 Claude Opus 4로 대본 생성 중...")
            result = generate_script_opus(prompt)
            _last_model = "Claude Opus 4"
        except Exception as e:
            print(f"   ⚠ Opus 실패 ({e}) — Gemini로 전환", file=sys.stderr)
    if result is None:
        if not GEMINI_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY 또는 GEMINI_API_KEY 필요")
        print("   🤖 Gemini Flash로 대본 생성 중...")
        _last_model = "Gemini Flash"
        result = generate_script_gemini(prompt)

    # ── 자기 재검토·수정 반복 (어색한 문구 · 미래비전 전달력 보강) ──
    for n in range(1, SCRIPT_REVIEW_ROUNDS + 1):
        print(f"   🔍 대본 재검토 {n}/{SCRIPT_REVIEW_ROUNDS} 진행 중...")
        try:
            revised = review_script(result)
        except Exception as e:
            print(f"   ⚠ 재검토 {n}회차 실패 ({e}) — 이전 버전 유지", file=sys.stderr)
            break
        if not _has_required_script_markers(revised):
            print(f"   ⚠ 재검토 {n}회차 출력 형식 불완전 — 이전 버전 유지", file=sys.stderr)
            break
        if revised.strip() == result.strip():
            print(f"   ✓ 재검토 {n}회차: 수정할 어색한 문구 없음")
        else:
            print(f"   ✏ 재검토 {n}회차: 문구 다듬음")
        result = revised
    return result


def parse_script(raw):
    scenes = []
    SCENE_RANGE = range(0, 3)   # 씬 0(주간브리핑)~씬 2(미래비전) · 인트로·시장반응 씬 제거
    # 본문이 넘어가면 안 되는 경계 마커 (특히 마지막 씬이 이미지 프롬프트/섹션을 흡수하는 것 방지)
    BOUNDARY_MARKERS = ("IMAGE_PROMPT_", "=== 배경", "===")
    for i in SCENE_RANGE:
        tk = f"SCENE_{i}_TITLE:"
        bk = f"SCENE_{i}:"
        title = ""
        body  = ""
        if tk in raw:
            s = raw.index(tk) + len(tk)
            e = raw.find("\n", s)
            title = raw[s:e].strip() if e != -1 else raw[s:].strip()
        if bk in raw:
            s   = raw.index(bk) + len(bk)
            # 다음 씬 타이틀 또는 이미지 프롬프트/섹션 마커 중 가장 먼저 등장하는 곳에서 끊는다
            nxt = raw.find(f"SCENE_{i+1}_TITLE:", s)
            if nxt == -1:
                nxt = len(raw)
            for marker in BOUNDARY_MARKERS:
                m = raw.find(marker, s)
                if m != -1:
                    nxt = min(nxt, m)
            body = raw[s:nxt].strip()
        lines = [l.strip() for l in body.split("\n")]
        scenes.append({"index": i, "title": title, "lines": lines, "body": body})
    return scenes


def parse_image_prompts(raw):
    """대본에서 씬별 Imagen 프롬프트 추출 → {0: "...", 1: "...", ...}"""
    prompts = {}
    for i in range(0, 3):
        key = f"IMAGE_PROMPT_{i}:"
        if key in raw:
            s = raw.index(key) + len(key)
            e = raw.find("\n", s)
            val = (raw[s:e] if e != -1 else raw[s:]).strip()
            # 대괄호 설명 텍스트 제거 (AI가 그대로 반환하는 경우)
            if val.startswith("[") and val.endswith("]"):
                val = ""
            if val:
                prompts[i] = val
    return prompts

# ── 이미지 생성 ───────────────────────────────────────────────────────────

def find_font():
    """시스템 한글 폰트 경로 탐색"""
    reg_candidates = [
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    ]
    bold_candidates = [
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothicExtraBold.ttf",
        "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf",
    ]
    reg  = next((p for p in reg_candidates  if os.path.exists(p)), None)
    bold = next((p for p in bold_candidates if os.path.exists(p)), reg)
    return reg, bold


def find_soft_font():
    """둥근·친근한 폰트(나눔스퀘어라운드) 탐색 — 호재 심층 씬 등 부드러운 톤용.

    설치 안 됐으면 (None, None) 반환 → 호출부에서 기본 폰트로 폴백.
    """
    round_reg = [
        "/usr/share/fonts/truetype/nanum/NanumSquareRoundR.ttf",
        "/usr/share/fonts/truetype/nanum/NanumSquareR.ttf",
    ]
    round_bold = [
        "/usr/share/fonts/truetype/nanum/NanumSquareRoundB.ttf",
        "/usr/share/fonts/truetype/nanum/NanumSquareB.ttf",
    ]
    reg  = next((p for p in round_reg  if os.path.exists(p)), None)
    bold = next((p for p in round_bold if os.path.exists(p)), reg)
    return reg, bold


def wrap_text(draw, text, font, max_w):
    """Returns list of lines that fit within max_w."""
    lines = []
    for paragraph in text.split('\n'):
        words = paragraph.split(' ')
        current = ""
        for word in words:
            test = current + (" " if current else "") + word
            bb = draw.textbbox((0, 0), test, font=font)
            if bb[2] - bb[0] <= max_w:
                current = test
            else:
                if current:
                    lines.append(current)
                current = ""
                for char in word:
                    test2 = current + char
                    bb2 = draw.textbbox((0, 0), test2, font=font)
                    if bb2[2] - bb2[0] > max_w and current:
                        lines.append(current)
                        current = char
                    else:
                        current = test2
        if current:
            lines.append(current)
    return lines


def wrap_ellipsis(draw, text, font, max_w, max_lines):
    """wrap_text로 줄바꿈하되 max_lines를 넘으면 마지막 줄 끝에 '…'를 붙여 문장 중간 잘림을 방지."""
    lines = wrap_text(draw, text, font, max_w)
    if len(lines) <= max_lines:
        return lines
    shown = lines[:max_lines]
    last = (shown[-1] if shown else "").rstrip()
    def _w(s):
        b = draw.textbbox((0, 0), s, font=font)
        return b[2] - b[0]
    while last and _w(last + "…") > max_w:
        last = last[:-1].rstrip()
    shown[-1] = (last + "…") if last else "…"
    return shown


def render_lines(draw, text, x, y, font, fill, max_px, line_gap=8):
    """여러 줄 텍스트 렌더링 → 다음 y 반환"""
    for raw_line in text.split("\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            y += line_gap
            continue
        for line in wrap_text(draw, raw_line, font, max_px):
            draw.text((x, y), line, font=font, fill=fill)
            bbox = draw.textbbox((0, 0), line, font=font)
            y += (bbox[3] - bbox[1]) + line_gap
    return y


def _is_usable_photo(raw: bytes) -> bool:
    """배경으로 쓸 만한 사진인지 검사 — 아주 작은 아이콘/로고·극단적 슬리버만 거부.
    로켓처럼 세로로 긴 사진은 허용(씬 합성 시 cover-crop)."""
    from PIL import Image as _PILImg
    import io as _io
    try:
        pimg = _PILImg.open(_io.BytesIO(raw))
        pw, ph = pimg.size
        if min(pw, ph) < 150:                        # 너무 작은 아이콘/로고
            return False
        if max(pw, ph) / max(min(pw, ph), 1) > 4.0:  # 배너/슬리버 거부
            return False
        return True
    except Exception:
        return True   # 검증 불가 시 일단 허용


def fetch_wiki_image(article: str, out_path: Path) -> bool:
    """Wikipedia 기사 대표 이미지를 다운로드. (REST summary 우선 → pageimages 폴백)
    로켓 등 세로형 사진도 허용하고, 아주 작은 아이콘·슬리버만 거부한다."""
    headers = {"User-Agent": f"{TICKER}-Dashboard/2.0 (github.com/{REPO})"}
    candidates = []
    # 1) REST summary — 기사 대표(hero) 이미지. originalimage가 고화질
    try:
        title_enc = urllib.parse.quote(article.replace(" ", "_"))
        req = urllib.request.Request(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{title_enc}",
            headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            summ = json.loads(r.read())
        for key in ("originalimage", "thumbnail"):
            src = (summ.get(key) or {}).get("source", "")
            if src:
                candidates.append(src)
    except Exception:
        pass
    # 2) pageimages 폴백
    try:
        params = urllib.parse.urlencode({
            "action": "query", "titles": article,
            "prop": "pageimages", "pithumbsize": "1280", "format": "json",
        })
        req = urllib.request.Request(
            f"https://en.wikipedia.org/w/api.php?{params}", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        for p in data.get("query", {}).get("pages", {}).values():
            src = p.get("thumbnail", {}).get("source", "")
            if src:
                candidates.append(src)
    except Exception:
        pass
    # 후보를 순서대로 시도
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
        except Exception as e:
            print(f"   ⚠ 이미지 다운로드 실패 ({article}): {e}", file=sys.stderr)
            continue
        if _is_usable_photo(raw):
            out_path.write_bytes(raw)
            return True
        print(f"   ⚠ 부적합 이미지 skip ({article})", file=sys.stderr)
    return False


def fetch_wiki_image_with_fallback(articles, out_path: Path) -> bool:
    """후보 기사 목록 중 가로형 이미지를 찾을 때까지 순서대로 시도."""
    for article in (articles if isinstance(articles, list) else [articles]):
        if fetch_wiki_image(article, out_path):
            return True
    return False


_NANO_BANANA_MODELS = [
    "gemini-2.5-flash-image",          # Nano Banana  (500/일 무료)
    "gemini-3.1-flash-image-preview",  # Nano Banana 2 (100/일 무료, 폴백)
]

_NANO_QUOTA_EXHAUSTED = False   # 429가 대기 후에도 반복되면 세션 전체 스킵 (일일 쿼터 소진 판단)

def fetch_nano_banana_image(prompt: str, out_path: Path, aspect_ratio: str = "16:9") -> bool:
    """Nano Banana API로 씬 배경 이미지 생성. 실패 시 False 반환.
    aspect_ratio: '16:9' (씬 1~3 가로 strip) 또는 '9:16' (씬 0·4 풀스크린).

    429(RESOURCE_EXHAUSTED) 처리: 무료 티어는 분당·일일 한도가 함께 있어 —
    첫 429엔 65초 대기 후 1회 재시도(분당 한도면 해소), 그래도 429면 일일 쿼터
    소진으로 보고 남은 씬 전부 스킵(무의미한 재시도로 몇 분 낭비 방지)."""
    global _NANO_QUOTA_EXHAUSTED
    if not GEMINI_API_KEY or not prompt or _NANO_QUOTA_EXHAUSTED:
        return False
    import time
    waited_for_quota = False
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=GEMINI_API_KEY)
        for model_id in _NANO_BANANA_MODELS:
            for attempt in range(2):   # 모델당 최대 2회 — 일시적 네트워크/서버 오류 대비
                try:
                    response = client.models.generate_content(
                        model=model_id,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_modalities=["IMAGE"],
                            image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
                        ),
                    )
                    for part in response.parts:
                        if part.inline_data:
                            out_path.write_bytes(part.inline_data.data)
                            return True
                    break   # 응답은 왔으나 이미지 없음 → 재시도 무의미, 다음 모델로
                except Exception as e:
                    msg = str(e)
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                        if not waited_for_quota:
                            print("      ⏳ 이미지 쿼터 429 — 65초 대기 후 재시도 (분당 한도 해소 대기)",
                                  file=sys.stderr)
                            time.sleep(65)
                            waited_for_quota = True
                            continue
                        _NANO_QUOTA_EXHAUSTED = True
                        print("      🚫 대기 후에도 429 — Gemini 이미지 일일 쿼터 소진으로 판단, "
                              "이번 실행의 남은 씬 배경 생성을 건너뜁니다", file=sys.stderr)
                        return False
                    print(f"      ⚠ {model_id} 시도{attempt+1}/2 실패: {e}", file=sys.stderr)
                    continue
    except Exception as e:
        print(f"      ⚠ Nano Banana 초기화 실패: {e}", file=sys.stderr)
    return False


def make_canvas(accent):
    """다크 배경 캔버스 생성 (1080×1920 세로 포맷)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, 6], fill=accent)
    draw.rectangle([0, H - 100, W, H], fill=(24, 32, 54))
    return img, draw


def draw_photo_card(img, draw, accent, bg_path: Path | None, x, y, w, h):
    """Wikipedia 사진을 프레임에 삽입.
    비율이 안 맞으면 blurred-cover 배경 + contain-fit 전경으로 프레임 가득 채움.
    """
    from PIL import Image as PILImage, ImageFilter
    # 외곽 테두리
    draw.rounded_rectangle([x - 3, y - 3, x + w + 3, y + h + 3],
                           radius=8, outline=accent, width=2)
    if not bg_path or not bg_path.exists():
        # AI 배경 실패(쿼터 등) 폴백: 단색 대신 헤더와 톤을 맞춘 세로 그라데이션 +
        # 대각 스트라이프 — 이미지가 없어도 '빈 화면'으로 보이지 않게(사용자 피드백)
        from PIL import ImageDraw
        band = PILImage.new("RGB", (w, h), CARD_BG)
        bd = ImageDraw.Draw(band)
        top, bottom = (26, 34, 60), (13, 17, 32)
        for yy in range(h):
            t = yy / max(1, h - 1)
            bd.line([(0, yy), (w, yy)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
        stripe = tuple(min(255, c + 14) for c in top)
        for sx in range(-h, w + h, 120):
            bd.line([(sx, h), (sx + h, 0)], fill=stripe, width=26)
        # 은은한 악센트 글로우 라인
        bd.line([(0, h - 2), (w, h - 2)], fill=accent, width=2)
        img.paste(band, (x, y))
        draw.rounded_rectangle([x, y, x + w, y + h], radius=6, outline=None)
        return
    try:
        photo = PILImage.open(bg_path).convert("RGB")
        pw, ph = photo.size
        target_ratio = w / h
        img_ratio    = pw / ph

        # ── 배경 레이어: cover-crop + 블러 (비율 차이 영역을 가림) ──
        bg = photo.copy()
        if img_ratio > target_ratio:
            new_w = int(ph * target_ratio)
            left = (pw - new_w) // 2
            bg = bg.crop([left, 0, left + new_w, ph])
        else:
            new_h = int(pw / target_ratio)
            top = (ph - new_h) // 2
            bg = bg.crop([0, top, pw, top + new_h])
        bg = bg.resize((w, h), PILImage.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(radius=24))
        # 블러 배경 오버레이 — 밝게 (170→90)
        bg_ov = PILImage.new("RGBA", (w, h), (8, 10, 16, 90))
        bg = PILImage.alpha_composite(bg.convert("RGBA"), bg_ov).convert("RGB")

        # ── 전경 레이어: contain-fit (프레임 안에 사진 전체 표시) ──
        if img_ratio > target_ratio:
            fg_w = w
            fg_h = int(w / img_ratio)
        else:
            fg_h = h
            fg_w = int(h * img_ratio)
        fg = photo.resize((fg_w, fg_h), PILImage.LANCZOS)
        # 전경 오버레이 최소화 — 사진 밝게 표시 (80→20)
        fg_ov = PILImage.new("RGBA", (fg_w, fg_h), (8, 10, 16, 20))
        fg = PILImage.alpha_composite(fg.convert("RGBA"), fg_ov).convert("RGB")

        # ── 합성: 배경 위에 전경을 중앙 정렬 ──
        bg.paste(fg, ((w - fg_w) // 2, (h - fg_h) // 2))
        img.paste(bg, (x, y))

        # 외곽 테두리 재그리기 (paste 이후)
        from PIL import ImageDraw as ID
        d2 = ID.Draw(img)
        d2.rounded_rectangle([x - 3, y - 3, x + w + 3, y + h + 3],
                             radius=8, outline=accent, width=2)
    except Exception:
        draw.rounded_rectangle([x, y, x + w, y + h], radius=6, fill=CARD_BG)


def draw_mbc_header(draw, brand: str, title_main: str, title_sub: str, accent,
                     fnt_brand, fnt_main, fnt_sub):
    """리뉴얼 헤더 — 대각선 텍스처 + accent pill 배지 + 상하 accent 바."""
    # ── 배경 그라데이션 ──
    for yy in range(HEADER_H):
        t = yy / HEADER_H
        r = int(NAVY[0] * (1 - t * 0.35) + NAVY_DEEP[0] * (t * 0.35))
        g = int(NAVY[1] * (1 - t * 0.35) + NAVY_DEEP[1] * (t * 0.35))
        b = int(NAVY[2] * (1 - t * 0.35) + NAVY_DEEP[2] * (t * 0.35))
        draw.line([(0, yy), (W, yy)], fill=(r, g, b))

    # ── 대각선 스트라이프 텍스처 ──
    sc = (max(0, NAVY_DEEP[0] - 6), max(0, NAVY_DEEP[1] - 6), min(255, NAVY_DEEP[2] + 8))
    for xx in range(-HEADER_H, W + HEADER_H, 58):
        draw.line([(xx, 0), (xx + HEADER_H, HEADER_H)], fill=sc, width=22)

    # ── 상단 accent 바 ──
    draw.rectangle([0, 0, W, 10], fill=accent)

    # ── 브랜드 배지 — 채워진 pill (accent 배경 + 다크 텍스트) ──
    brand_y = 70
    bb_b = draw.textbbox((0, 0), brand, font=fnt_brand)
    bw = (bb_b[2] - bb_b[0]) + 64
    bx0 = (W - bw) // 2
    draw.rounded_rectangle([bx0, brand_y - 30, bx0 + bw, brand_y + 30],
                           radius=30, fill=accent)
    draw.text((W // 2, brand_y), brand, font=fnt_brand, fill=BG, anchor="mm")

    # 배지 아래 accent 하이라이트 라인
    draw.line([(bx0 + 24, brand_y + 38), (bx0 + bw - 24, brand_y + 38)],
              fill=accent, width=3)

    # ── 메인 헤드라인 (2줄 초과 시 … — 씬2처럼 긴 대본 줄도 중간 잘림 없이) ──
    main_y = 148
    main_lines = wrap_ellipsis(draw, title_main, fnt_main, W - 80, 2)
    for wl in main_lines[:2]:
        bb = draw.textbbox((0, 0), wl, font=fnt_main)
        tw = bb[2] - bb[0]
        draw.text(((W - tw) // 2, main_y), wl, font=fnt_main, fill=WHITE,
                  stroke_width=3, stroke_fill=STROKE)
        main_y += (bb[3] - bb[1]) + 14

    # ── 서브 타이틀 (accent 색상) ──
    if title_sub:
        sub_y = main_y + 10
        sub_lines = wrap_text(draw, title_sub, fnt_sub, W - 80)
        for wl in sub_lines[:1]:
            bb = draw.textbbox((0, 0), wl, font=fnt_sub)
            tw = bb[2] - bb[0]
            draw.text(((W - tw) // 2, sub_y), wl, font=fnt_sub, fill=accent,
                      stroke_width=2, stroke_fill=STROKE)

    # ── 하단 accent 바 ──
    draw.rectangle([0, HEADER_H - 10, W, HEADER_H], fill=accent)


def draw_buy_index_gauge(draw, cx, cy, r, bi, fnt_big, fnt_small):
    col = GREEN if bi >= 65 else AMBER if bi >= 45 else RED
    # 배경 반원 (회색)
    draw.arc([cx - r, cy - r, cx + r, cy + r], start=180, end=360, fill=(62, 68, 88), width=22)
    # 값 반원 (컬러)
    end_a = 180 + int(bi / 100 * 180)
    draw.arc([cx - r, cy - r, cx + r, cy + r], start=180, end=end_a, fill=col, width=22)
    # 중앙 숫자
    draw.text((cx, cy - 18), str(bi), font=fnt_big, fill=col, anchor="mm")
    draw.text((cx, cy + 22), "참고지수", font=fnt_small, fill=GRAY, anchor="mm")
    # 범례
    draw.text((cx - r + 8, cy + 14), "0", font=fnt_small, fill=GRAY)
    draw.text((cx + r - 22, cy + 14), "100", font=fnt_small, fill=GRAY)


def draw_news_card_portrait(draw, img, x, y, w, h, chapter, content, source, accent,
                             fnt_bold, fnt_content, fnt_source,
                             fnt_content_xl=None, fnt_content_sm=None):
    """세로 포맷 전용 뉴스카드 (헤더 + 내용 수직중앙 + 하단 출처)."""
    from PIL import ImageDraw

    HEADER_H = 90
    FOOTER_H = 60

    grade_map = {
        "호재": GREEN, "악재": RED, "주의": AMBER,
        "참고": CYAN, "고려": BLUE,
    }
    badge_col = GRAY
    badge_text = ""
    for grade, col in grade_map.items():
        if grade in source:
            badge_col = col
            badge_text = grade
            break

    # 카드 배경
    draw.rounded_rectangle([x, y, x + w, y + h], radius=14,
                            fill=CARD_BG, outline=accent, width=2)

    # 헤더 배경
    draw.rounded_rectangle([x, y, x + w, y + HEADER_H], radius=14, fill=accent)
    draw.rectangle([x, y + HEADER_H - 14, x + w, y + HEADER_H], fill=accent)

    # 챕터 이름 (헤더 왼쪽)
    draw.text((x + 22, y + HEADER_H // 2), chapter[:5],
              font=fnt_bold, fill=BADGE_BG, anchor="lm")

    # 등급 배지 (헤더 오른쪽)
    if badge_text:
        badge_w = 110
        badge_h = 52
        badge_x = x + w - badge_w - 16
        badge_y = y + (HEADER_H - badge_h) // 2
        draw.rounded_rectangle([badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
                               radius=10, fill=BADGE_BG)
        draw.text((badge_x + badge_w // 2, badge_y + badge_h // 2),
                  badge_text, font=fnt_bold, fill=badge_col, anchor="mm")

    # ── 적응형 폰트: 콘텐츠 길이에 따라 자동 선택 ──────────────────────────
    char_count = len(content)
    if fnt_content_xl and char_count < 60:
        adaptive_font = fnt_content_xl   # 48px — 짧은 콘텐츠는 크게
    elif fnt_content_sm and char_count >= 120:
        adaptive_font = fnt_content_sm   # 28px — 긴 콘텐츠는 작게
    else:
        adaptive_font = fnt_content      # 36px — 기본

    # 내용 영역
    content_x = x + 22
    content_y = y + HEADER_H + 16
    content_max_w = w - 44
    content_area_h = h - HEADER_H - FOOTER_H - 32

    content_lines = wrap_text(draw, content, adaptive_font, content_max_w)
    bb_test = draw.textbbox((0, 0), "가", font=adaptive_font)
    char_h = bb_test[3] - bb_test[1]
    line_h = char_h + 14
    max_lines = max(1, content_area_h // line_h)

    # 수직 중앙 정렬
    display_lines = content_lines[:max_lines]
    total_text_h = len(display_lines) * line_h
    cy = content_y + max(0, (content_area_h - total_text_h) // 2)

    for line in display_lines:
        if cy + char_h > y + h - FOOTER_H - 8:
            break
        draw.text((content_x, cy), line, font=adaptive_font, fill=WHITE,
                  stroke_width=1, stroke_fill=STROKE)
        cy += line_h

    # 하단 출처 바
    footer_y = y + h - FOOTER_H
    draw.rounded_rectangle([x, footer_y - 6, x + w, y + h], radius=14, fill=BADGE_BG)

    # 출처 텍스트 — KEY 노랑으로 강조
    src_display = source
    for grade in grade_map:
        src_display = src_display.replace("·" + grade, "").replace(grade + "·", "").replace(grade, "").strip("· ")
    draw.text((x + 18, footer_y + FOOTER_H // 2), src_display[:50],
              font=fnt_source, fill=KEY, anchor="lm",
              stroke_width=1, stroke_fill=STROKE)


_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # 감정/얼굴
    "\U0001F300-\U0001F5FF"  # 기호/사물
    "\U0001F680-\U0001F6FF"  # 교통/지도
    "\U0001F1E0-\U0001F1FF"  # 국기
    "\U00002700-\U000027BF"  # 기타
    "\U0001F900-\U0001F9FF"  # 보충 기호
    "\U00002600-\U000026FF"  # 잡기호
    "‍"                  # ZWJ
    "️"                  # 변형 선택자
    "]+",
    flags=re.UNICODE,
)

def strip_emoji(text: str) -> str:
    """PIL에서 렌더링 불가한 이모지를 제거한다."""
    return _EMOJI_RE.sub("", text).strip()


# ── 강조 마커(*...*) 색상 렌더링 ──────────────────────────────────────────────
# 대본에서 핵심 글귀를 *별표*로 감싸면 화면에서 강조색(기본 골드)으로 표시한다.
_HL_RE    = re.compile(r"\*(.+?)\*")
# 토큰화: 영문/숫자/통화 묶음은 한 덩어리, 공백은 그대로, 그 외(한글 등)는 글자 단위
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9$%.,+\-]*|\s+|[^\sA-Za-z0-9]")


def strip_markup(text: str) -> str:
    """강조 마커(*)를 제거한 순수 텍스트 — 마커를 해석하지 않는 렌더용."""
    return (text or "").replace("*", "")


def split_runs(text: str):
    """'*...*' 마커 기준으로 (조각, 강조여부) 런 리스트 반환. 마커는 제거된다."""
    runs, pos = [], 0
    text = text or ""
    for m in _HL_RE.finditer(text):
        if m.start() > pos:
            runs.append((text[pos:m.start()], False))
        runs.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], False))
    return [(seg.replace("*", ""), hl) for seg, hl in runs if seg.replace("*", "")]


def wrap_runs(draw, runs, font, max_w):
    """런 리스트를 max_w에 맞춰 여러 시각 줄로 래핑. 각 줄은 (조각, 강조여부) 런 리스트."""
    toks = []
    for seg, hl in runs:
        for t in _TOKEN_RE.findall(seg):
            toks.append((t, hl))

    def line_w(items):
        s = "".join(t for t, _ in items)
        return draw.textlength(s, font=font) if s else 0

    lines, cur = [], []
    for t, hl in toks:
        if t == "\n":
            lines.append(cur); cur = []; continue
        if not cur and t.isspace():
            continue
        if not cur or line_w(cur + [(t, hl)]) <= max_w:
            cur.append((t, hl))
        else:
            lines.append(cur)
            cur = [] if t.isspace() else [(t, hl)]
    if cur:
        lines.append(cur)

    out = []
    for line in lines:
        while line and line[0][0].isspace():
            line = line[1:]
        while line and line[-1][0].isspace():
            line = line[:-1]
        merged = []
        for t, hl in line:
            if merged and merged[-1][1] == hl:
                merged[-1] = (merged[-1][0] + t, hl)
            else:
                merged.append((t, hl))
        if merged:
            out.append(merged)
    return out


def draw_rich_line(draw, x, y, line_runs, font, base_fill, hl_fill,
                   stroke_width=1, stroke_fill=STROKE, center_w=None):
    """한 시각 줄(런 리스트)을 그린다. center_w 지정 시 그 폭 안에서 가운데 정렬."""
    total = sum(draw.textlength(seg, font=font) for seg, _ in line_runs)
    cx = x + max(0, (center_w - total)) / 2 if center_w is not None else x
    for seg, hl in line_runs:
        draw.text((cx, y), seg, font=font,
                  fill=(hl_fill if hl else base_fill),
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        cx += draw.textlength(seg, font=font)
    return total


def draw_rich_text(draw, text, x, y, font, base_fill, max_w, *, hl_fill=KEY,
                   max_lines=None, center=False, center_x=None, center_w=None,
                   stroke_width=1, stroke_fill=STROKE, line_h=None, line_gap=8):
    """마커 포함 텍스트를 래핑 + 색상 강조하여 그린다. 다음 y를 반환.

    center=True면 center_w(기본 max_w) 안에서 center_x(기본 x) 기준 가운데 정렬.
    """
    runs    = split_runs(strip_emoji(text))
    wrapped = wrap_runs(draw, runs, font, max_w)
    if max_lines:
        wrapped = wrapped[:max_lines]
    bb   = draw.textbbox((0, 0), "가", font=font)
    step = line_h if line_h else (bb[3] - bb[1]) + line_gap
    cw   = (center_w if center_w is not None else max_w) if center else None
    cx   = center_x if center_x is not None else x
    for line_runs in wrapped:
        draw_rich_line(draw, cx, y, line_runs, font, base_fill, hl_fill,
                       stroke_width=stroke_width, stroke_fill=stroke_fill,
                       center_w=cw)
        y += step
    return y


def draw_bell_icon(draw, cx, cy, size, color):
    """PIL 도형으로 그린 벨 아이콘 (🔔 이모지 대체)."""
    s = size
    # 돔 (반원 — 벨 상단)
    draw.pieslice([cx - s // 2, cy - s, cx + s // 2, cy], 180, 0, fill=color)
    # 몸통 (아래로 퍼지는 사다리꼴)
    body = [
        (cx - s // 2,       cy - s // 6),
        (cx + s // 2,       cy - s // 6),
        (cx + s // 2 + s // 5, cy + s // 2),
        (cx - s // 2 - s // 5, cy + s // 2),
    ]
    draw.polygon(body, fill=color)
    # 하단 챙 (가로 타원 아크)
    hw = s // 2 + s // 5 + 8
    draw.arc([cx - hw, cy + s // 3, cx + hw, cy + s // 2 + s // 4],
             0, 180, fill=color, width=max(s // 6, 5))
    # 손잡이 (상단 작은 아치)
    draw.arc([cx - s // 8, cy - s - s // 8, cx + s // 8, cy - s + s // 8],
             180, 0, fill=color, width=max(s // 10, 4))
    # 추 (하단 작은 원)
    cr = s // 8
    draw.ellipse([cx - cr, cy + s // 2, cx + cr, cy + s // 2 + cr * 2], fill=color)


def draw_bi_legend(draw, avg_bi, fnt_label, fnt_val):
    """하단 안전 영역에 매수지수 범례 + 현재 점수 표시 (y=1700~1870). 씬 4에만 사용."""
    LX  = PAD
    LY  = SAFE_BOTTOM + 20           # 1700
    LW  = W - PAD * 2                # 1000
    LH  = H - LY - 50                # ~170px

    # 배경 패널
    draw.rounded_rectangle([LX, LY, LX + LW, LY + LH],
                           radius=14, fill=CARD_BG, outline=(55, 65, 95), width=1)

    # 현재 매수지수 (왼쪽 강조)
    bi_col = GREEN if avg_bi >= 65 else AMBER if avg_bi >= 45 else RED
    bi_str = str(avg_bi) if avg_bi is not None else "?"
    draw.text((LX + 24, LY + LH // 2), f"{bi_str}점",
              font=fnt_val, fill=bi_col, anchor="lm",
              stroke_width=2, stroke_fill=STROKE)

    signal = "긍정" if avg_bi is not None and avg_bi >= 65 else \
             "중립" if avg_bi is not None and avg_bi >= 45 else "신중"
    draw.text((LX + 24, LY + LH // 2 + 38), signal,
              font=fnt_label, fill=bi_col, anchor="lm",
              stroke_width=1, stroke_fill=STROKE)

    # 구분선
    SEP_X = LX + 140
    draw.line([(SEP_X, LY + 16), (SEP_X, LY + LH - 16)], fill=(65, 75, 105), width=1)

    # 오른쪽: 3단계 범례
    ITEMS = [
        (GREEN, "65점↑", "긍정"),
        (AMBER, "45-64점", "중립"),
        (RED,   "44점↓", "신중"),
    ]
    slot_w = (LX + LW - SEP_X - 16) // 3
    for j, (col, range_lbl, sig_lbl) in enumerate(ITEMS):
        ix = SEP_X + 8 + j * slot_w
        iy = LY + LH // 2 - 28

        # 색상 원
        draw.ellipse([ix, iy, ix + 20, iy + 20], fill=col)
        draw.text((ix + 28, iy), range_lbl,
                  font=fnt_label, fill=LGRAY)
        draw.text((ix + 28, iy + 24), sig_lbl,
                  font=fnt_label, fill=col)

    # 면책 문구 + 참고 뉴스 강조 (우측)
    disclaimer = "※ 투자 권유 아님 · 참고 뉴스 · 투자 판단은 본인 책임"
    db = draw.textbbox((0, 0), disclaimer, font=fnt_label)
    dw = db[2] - db[0]
    draw.text((LX + LW - dw - 10, LY + LH - 26),
              disclaimer, font=fnt_label, fill=(200, 160, 80))


def draw_stat_box(draw, x, y, w, h, label, value, col, fnt_val, fnt_lbl):
    draw.rectangle([x, y, x + w, y + h], fill=CARD_BG, outline=(55, 65, 95), width=1)
    draw.text((x + w // 2, y + 18), label, font=fnt_lbl, fill=GRAY, anchor="mt")
    draw.text((x + w // 2, y + h - 22), value, font=fnt_val, fill=col, anchor="mb")


# index.html SOURCE_INFO와 동일한 매핑 — 영상 호재 카드에 출처 신뢰도 동반 표기용(부분 문자열 매칭).
SOURCE_CREDIBILITY = {
    'reuters': ('영국·글로벌통신', 'high'), 'bloomberg': ('미국', 'high'),
    'wall street journal': ('미국', 'high'), 'wsj': ('미국', 'high'),
    'cnbc': ('미국', 'high'), 'financial times': ('영국', 'high'),
    'associated press': ('미국·통신', 'high'), 'barron': ('미국', 'high'),
    'space news': ('미국·우주전문', 'high'), 'spacenews': ('미국·우주전문', 'high'),
    'ars technica': ('미국·기술전문', 'high'), 'nasa': ('미국·정부기관', 'high'),
    'sec filing': ('미국·규제기관', 'high'), '연합': ('한국·통신', 'high'),
    'rocket lab': ('기업공식(IR)', 'mid'), 'investor relations': ('기업공식(IR)', 'mid'),
    'forbes': ('미국', 'mid'), 'the verge': ('미국·기술', 'mid'),
    'marketwatch': ('미국', 'mid'), 'yahoo': ('미국·집계', 'mid'),
    'investing.com': ('글로벌·집계', 'mid'), 'business insider': ('미국', 'mid'),
    'benzinga': ('미국', 'mid'), 'seeking alpha': ('미국·기고', 'mid'),
    'tipranks': ('미국·집계', 'mid'), 'simply wall': ('미국·집계', 'mid'),
    'zacks': ('미국·리서치', 'mid'), '한국경제': ('한국', 'mid'), '매일경제': ('한국', 'mid'),
    'motley fool': ('미국·투자콘텐츠', 'low'), 'fool.com': ('미국·투자콘텐츠', 'low'),
    'investorplace': ('미국·투자콘텐츠', 'low'), 'stocktwits': ('미국·소셜', 'low'),
    # ── v1.0.22 보강: 실제 신뢰 매체인데 '미확인'으로 빠지던 출처들 ──
    # 항공우주·국방 전문지 (RKLB 관련성 높음)
    'payload': ('미국·우주전문', 'high'), 'nasaspaceflight': ('미국·우주전문', 'high'),
    'defense news': ('미국·국방전문', 'high'), 'breaking defense': ('미국·국방전문', 'high'),
    'space.com': ('미국·우주전문', 'mid'), 'via satellite': ('미국·우주전문', 'mid'),
    # 주요 방송·종합 매체
    'fox business': ('미국', 'mid'), 'cnn': ('미국', 'mid'),
    'techcrunch': ('미국·기술', 'mid'), 'electrek': ('미국·기술', 'mid'),
    'thestreet': ('미국', 'mid'), 'the street': ('미국', 'mid'),
    'quartz': ('미국', 'mid'),
    # 공식 PR 와이어 (기업 공식 발표 배포)
    'globenewswire': ('공식 PR와이어', 'mid'), 'business wire': ('공식 PR와이어', 'mid'),
    'businesswire': ('공식 PR와이어', 'mid'), 'pr newswire': ('공식 PR와이어', 'mid'),
    'prnewswire': ('공식 PR와이어', 'mid'),
    # 데이터 집계 사이트
    'stock analysis': ('미국·집계', 'low'), 'stockanalysis': ('미국·집계', 'low'),
}
CRED_TIER_LABEL = {'high': '신뢰 높음', 'mid': '신뢰 보통', 'low': '신뢰 낮음'}


def source_credibility_tag(source: str) -> str:
    """뉴스 출처 신뢰도 태그 (대시보드 SourceTag와 동일 판정 기준). 미매칭 시 '출처 미확인'.
    영상 카드 하단 바는 한 줄 폭이 좁아 국적 라벨은 생략하고 신뢰도만 짧게 표기."""
    if not source:
        return ""
    s = source.lower()
    for k, (_country, tier) in SOURCE_CREDIBILITY.items():
        if k in s:
            return CRED_TIER_LABEL[tier]
    return "출처 미확인"


_CRED_RANK = {'high': 3, 'mid': 2, 'low': 1}

def source_credibility_rank(source: str) -> int:
    """출처 신뢰도 정수 등급 — BEST 호재/악재 선정 정렬용.
    높음 3 > 보통 2 > 낮음 1 > 출처 미확인/빈값 0(하위). 신뢰 낮은 출처는 선정에서 후순위로 밀린다."""
    if not source:
        return 0
    s = source.lower()
    for k, (_country, tier) in SOURCE_CREDIBILITY.items():
        if k in s:
            return _CRED_RANK.get(tier, 0)
    return 0


def parse_news_line(line):
    """'카테고리: 내용 | 소스' 형식 분리. → (chapter, content, source)"""
    source = ""
    if "|" in line:
        main, source = line.split("|", 1)
        source = source.strip()
    else:
        main = line
    if ": " in main:
        ch, ct = main.split(": ", 1)
        return ch.strip()[:6], ct.strip(), source
    return "뉴스", main.strip(), source


def draw_check(draw, x, y, size, color, width=None):
    """체크표시 ✓ (두 선분) — (x, y)는 좌상단, size는 한 변 기준."""
    w = width or max(3, size // 7)
    p1 = (x + size * 0.08, y + size * 0.52)
    p2 = (x + size * 0.38, y + size * 0.82)
    p3 = (x + size * 0.92, y + size * 0.14)
    draw.line([p1, p2], fill=color, width=w)
    draw.line([p2, p3], fill=color, width=w)


def draw_bullish_hero_card(draw, img, x, y, w, h, headline, details, score,
                            source, date, accent, fnt_bold, fnt_content,
                            fnt_source, fnt_content_xl=None, fnt_content_sm=None,
                            category=""):
    """호재 심층 히어로 카드 — BEST 배지 + ↑ 화살표 + 카테고리 라벨 + 스토리텔링."""
    from PIL import ImageDraw

    HEADER_H = 90
    FOOTER_H = 64

    # 카드 배경
    draw.rounded_rectangle([x, y, x + w, y + h], radius=14,
                            fill=CARD_BG, outline=accent, width=2)

    # 헤더 배경 (GREEN 강조)
    draw.rounded_rectangle([x, y, x + w, y + HEADER_H], radius=14, fill=accent)
    draw.rectangle([x, y + HEADER_H - 14, x + w, y + HEADER_H], fill=accent)

    # 헤더 왼쪽: 카테고리 또는 소스 라벨 ("+4pt" 대신 — 시청자에게 의미 있는 정보)
    header_label = (category or source or "최근 HOT")[:14]
    draw.text((x + 22, y + HEADER_H // 2), header_label,
              font=fnt_bold, fill=BADGE_BG, anchor="lm",
              stroke_width=2, stroke_fill=(0, 60, 0))

    # 헤더 오른쪽: "BEST" 배지
    badge_w, badge_h = 110, 52
    bx = x + w - badge_w - 16
    by = y + (HEADER_H - badge_h) // 2
    draw.rounded_rectangle([bx, by, bx + badge_w, by + badge_h],
                           radius=10, fill=BADGE_BG)
    draw.text((bx + badge_w // 2, by + badge_h // 2),
              "BEST", font=fnt_bold, fill=KEY, anchor="mm",
              stroke_width=1, stroke_fill=STROKE)

    # 본문 영역 — 각 호재 줄 앞에 초록 체크(✓) 머리기호
    content_x    = x + 28
    content_y    = y + HEADER_H + 16
    content_max_w = w - 28 - 22
    content_area_h = h - HEADER_H - FOOTER_H - 32
    CHECK_W      = 44   # 체크 + 여백 폭

    all_lines = [headline] + [d for d in details if d.strip()]

    # 헤드라인은 xl, 본문은 content 폰트 — 일관된 크기 계층
    headline_font = fnt_content_xl if fnt_content_xl else fnt_bold
    body_font     = fnt_content

    bb = draw.textbbox((0, 0), "가", font=body_font)
    char_h = bb[3] - bb[1]
    bbh = draw.textbbox((0, 0), "가", font=headline_font)
    head_h = bbh[3] - bbh[1]
    line_h      = char_h + 16   # 본문 줄 간격 — 전달사항당 2줄씩 6줄이 들어가도록 압축
    line_h_head = head_h + 16   # 헤드라인 줄 간격 — 글씨가 커 본문보다 더 크게
    HEAD_GAP    = 16            # 헤드라인과 첫 본문 줄 사이 추가 여백(겹침 방지)

    cy = content_y
    for i, ln in enumerate(all_lines[:6]):   # 헤드라인+세부 항목
        if not ln.strip() or cy + char_h > y + h - FOOTER_H - 8:
            continue
        use_font   = headline_font if i == 0 else body_font
        use_col    = WHITE         if i == 0 else LGRAY
        sw         = 2             if i == 0 else 1
        use_line_h = line_h_head   if i == 0 else line_h
        is_detail  = i >= 1                       # 헤드라인 제외, 호재 항목에만 체크
        text_x     = content_x + (CHECK_W if is_detail else 0)
        wrap_w     = content_max_w - (CHECK_W if is_detail else 0)
        wrapped    = wrap_runs(draw, split_runs(strip_emoji(ln)), use_font, wrap_w)
        for j, line_runs in enumerate(wrapped[:2]):
            if cy + char_h > y + h - FOOTER_H - 8:
                break
            if is_detail and j == 0:             # 줄 첫 행에만 ✓
                draw_check(draw, content_x, cy + char_h * 0.12, char_h, GREEN)
            draw_rich_line(draw, text_x, cy, line_runs, use_font, use_col, KEY,
                           stroke_width=sw, stroke_fill=STROKE)
            cy += use_line_h
        if i == 0:
            cy += HEAD_GAP                        # 헤드라인 끝난 뒤 본문 시작 전 여백

    # 하단 출처 바 (source · date)
    footer_y = y + h - FOOTER_H
    draw.rounded_rectangle([x, footer_y - 6, x + w, y + h], radius=14, fill=BADGE_BG)
    footer_text = " · ".join(filter(None, [source, date])) or "출처 미상"
    draw.text((x + 18, footer_y + FOOTER_H // 2), footer_text[:50],
              font=fnt_source, fill=KEY, anchor="lm",
              stroke_width=1, stroke_fill=STROKE)


_FRAME_TEMPLATE_PATH = Path("data/frame-template.png")
_frame_overlay_cache = None
_frame_overlay_loaded = False

def _load_frame_overlay():
    """프레임 템플릿 이미지를 1회 로드 후 캐싱 (없으면 None)."""
    global _frame_overlay_cache, _frame_overlay_loaded
    if _frame_overlay_loaded:
        return _frame_overlay_cache
    _frame_overlay_loaded = True
    if not _FRAME_TEMPLATE_PATH.exists():
        return None
    try:
        from PIL import Image as PILImage
        ov = PILImage.open(_FRAME_TEMPLATE_PATH).convert("RGBA")
        if ov.size != (W, H):
            ov = ov.resize((W, H), PILImage.LANCZOS)
        _frame_overlay_cache = ov
    except Exception as e:
        print(f"   ⚠ frame-template.png 로드 실패: {e}", file=sys.stderr)
    return _frame_overlay_cache


def _apply_frame_overlay(img):
    """씬 이미지 위에 통일 브랜드 프레임 오버레이 합성 (있을 때만)."""
    ov = _load_frame_overlay()
    if ov is None:
        return img
    from PIL import Image as PILImage
    base = img.convert("RGBA")
    return PILImage.alpha_composite(base, ov).convert("RGB")


def build_scene_image(scene, summary, font_reg, font_bold, bg_path: Path | None = None):
    from PIL import ImageFont, ImageDraw
    idx    = scene["index"]
    title  = scene["title"] or f"씬 {idx}"
    lines  = scene.get("lines") or [l.strip() for l in (scene.get("body") or "").split("\n") if l.strip()]
    accent = SCENE_ACCENTS[idx]   # 0=브리핑, 1=호재 심층, 2=다음주 전망(클로징)

    img, draw = make_canvas(accent)

    def fnt(path, size):
        try:
            return ImageFont.truetype(path, size) if path else ImageFont.load_default()
        except Exception:
            return ImageFont.load_default()

    # ── 폰트 (1080px 세로 포맷 기준 충분히 큰 사이즈) ──
    f_xl    = fnt(font_bold, 72)
    f_lg    = fnt(font_bold, 54)   # 40→54
    f_md    = fnt(font_bold, 48)   # 32→48
    f_nm    = fnt(font_reg,  42)   # 내용(본문) — 씬 공통 통일 크기 (44→42)
    f_sm    = fnt(font_reg,  36)   # 22→36
    f_xs    = fnt(font_reg,  30)   # 18→30
    f_src   = fnt(font_reg,  30)   # 20→30
    f_ch    = fnt(font_bold, 48)   # 34→48
    f_ct    = fnt(font_reg,  50)   # 34→50
    f_ct_xl = fnt(font_reg,  62)   # 44→62
    f_ct_sm = fnt(font_reg,  42)   # 28→42
    # MBC 스타일 헤더 폰트 — 통일 위계: 대제목 74 > 소제목 58 > 내용 42 (한 치수=16px씩)
    f_brand = fnt(font_bold, 44)   # 32→44
    f_head_main = fnt(font_bold, 74)   # 대제목 (80→74)
    f_head_sub  = fnt(font_bold, 58)   # 소제목 (64→58)
    # 인트로 전용: 대형 % 숫자
    f_huge      = fnt(font_bold, 200)
    # (씬2 대제목 f_huge_sub는 v1.0.31에서 MBC 헤더 f_head_main으로 통일되어 제거)

    # ── 부드러운 라운드 폰트 (호재 심층 씬용 — 딱딱함 완화) ──
    soft_reg, soft_bold = find_soft_font()
    soft_reg  = soft_reg  or font_reg
    soft_bold = soft_bold or font_bold
    sf_ch        = fnt(soft_bold, 48)
    sf_ct        = fnt(soft_reg,  42)   # 호재 본문(내용) — 씬 공통 통일 크기 (46→42)
    sf_ct_xl     = fnt(soft_reg,  58)   # 호재 카드 헤드라인(소제목) — 통일 (62→58)
    sf_ct_sm     = fnt(soft_reg,  42)
    sf_src       = fnt(soft_reg,  30)
    sf_brand     = fnt(soft_bold, 44)
    sf_head_main = fnt(soft_bold, 74)   # 대제목 (80→74)
    sf_head_sub  = fnt(soft_bold, 58)   # 소제목 (64→58)

    news_lines = [l for l in lines if l.strip() and not l.startswith("SCENE")]

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║ 씬 2 — 다음주 전망 (클로징, custom layout)                         ║
    # ╚══════════════════════════════════════════════════════════════════╝
    if idx == 2:
        # ① AI 배경 이미지를 풀스크린으로 깔기 (미래 비전 이미지)
        if bg_path and bg_path.exists():
            try:
                from PIL import Image as PILImage
                bg = PILImage.open(bg_path).convert("RGB")
                bw, bh = bg.size
                ratio = max(W / bw, H / bh)
                nw, nh = int(bw * ratio), int(bh * ratio)
                bg = bg.resize((nw, nh), PILImage.LANCZOS)
                ox, oy = (nw - W) // 2, (nh - H) // 2
                img.paste(bg.crop((ox, oy, ox + W, oy + H)), (0, 0))
                # 마젠타 톤 오버레이 — 0.6→0.42로 밝게
                overlay = PILImage.new("RGB", (W, H), (38, 12, 65))
                img = PILImage.blend(img, overlay, 0.42)
                draw = ImageDraw.Draw(img)
            except Exception:
                pass
        else:
            # 폴백: 기존 검정→마젠타 그라데이션
            for yy in range(H):
                t = yy / H
                draw.line([(0, yy), (W, yy)], fill=(
                    int(35 + 60 * t), int(10 + 28 * t), int(58 + 90 * t)
                ))

        # ── 헤더: MBC 스타일 (씬0·1과 통일) — pill·핵심 문구·예측 수치 서브 ──
        # 핵심 문구 = 씬2 줄1(다음주 핵심 일정·이벤트), 서브 = dailyForecasts 누적 예측 수치
        head2 = strip_markup(strip_emoji(news_lines[0])).strip() if news_lines else "다음주 핵심 이벤트 미리보기"
        if not (head2.startswith('"') or head2.startswith("'")):
            head2 = f'"{head2}"'
        head2_sub = ""
        fx = summary.get("forecasts") or []
        try:
            base = float(fx[0].get("basePrice"))
            endp = float(fx[-1].get("predictedPrice"))
            if base > 0:
                cum = (endp - base) / base * 100
                arrow = "▲" if cum >= 0 else "▼"
                sign  = "+" if cum >= 0 else ""
                head2_sub = f"다음주 예상 {arrow} {sign}{cum:.1f}% · 약 {fmt_price(endp, 0)}"
        except (TypeError, ValueError, IndexError, AttributeError):
            pass
        head2_sub = head2_sub or "다음주 관전 포인트"
        draw_mbc_header(draw, "다음주 전망", head2, head2_sub, accent,
                        f_brand, f_head_main, f_head_sub)

        # ── 3개 메시지 카드 (관전 포인트·가격 전망·마무리) ─────────────
        # news_lines: [0]=핵심일정(헤더로 승격), [1]=→관전포인트, [2]=가격예측, [3]=→흐름부연, [4]=변수, [5]=마무리
        def _nl(i, fallback):
            return strip_emoji(news_lines[i]) if len(news_lines) > i else fallback

        # 카드별 (label, lines[], col, bgcol, hl_col, max_body_lines)
        # hl_col: 본문 *강조* 색 — 카드마다 다른 색으로 포인트 구분 (금색/라이트시안/라이트그린)
        MSG_CARDS = [
            ("관전 포인트",  [_nl(1, "다음 주 핵심 이벤트를 주목하세요")],
             KEY,    CARD_AMBER,  KEY,             1),
            ("가격 전망",    [_nl(2, "변동성 흐름을 지켜봐요"),
                              _nl(3, ""),
                              _nl(4, "")],
             accent, CARD_PURPLE, CYAN_LIGHT,      3),
            ("마무리",        [_nl(5, "다음에 또 만나요!")],
             GREEN,  CARD_GREEN,  (134, 239, 172), 1),
        ]
        LINE_H = 52      # 줄간 px
        LABEL_H = 56     # 라벨 영역 높이 (상단 여백 포함)
        BODY_PAD = 18    # 본문 하단 여백
        MSG_Y = HEADER_H + 8
        MSG_GAP = 14
        # 카드별 렌더 줄(내용 줄을 최대 2줄로 wrap)·높이·위치 사전 계산 — 긴 내용도 잘리지 않고
        # 카드가 그만큼 커진다(빈약해 보이던 하단 여백을 내용으로 채움).
        cards_calc = []
        cy = MSG_Y
        for label, body_lines, col, bgcol, hl_col, max_lines in MSG_CARDS:
            raw_lines = [l for l in body_lines if l.strip()][:max_lines]
            if not raw_lines:
                raw_lines = [body_lines[0]] if body_lines else [""]
            rendered = []
            for txt in raw_lines:
                for lr in wrap_runs(draw, split_runs(txt), f_nm, W - PAD * 2 - 44)[:2]:
                    rendered.append(lr)
            rendered = rendered or [[]]
            card_h = LABEL_H + len(rendered) * LINE_H + BODY_PAD
            cards_calc.append((label, rendered, col, bgcol, hl_col, cy, card_h))
            cy += card_h + MSG_GAP
        last_cy_bottom = (cards_calc[-1][5] + cards_calc[-1][6]) if cards_calc else MSG_Y

        # 반투명 카드 배경(씬1·씬0과 동일한 비침 효과) — 뒤 미래도시 AI 배경이 비침
        from PIL import Image as PILImage
        CARD_ALPHA = 190
        _layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        _ld = ImageDraw.Draw(_layer)
        for _lbl, _vis, _col, _bg, _hl, _cyy, _ch in cards_calc:
            _ld.rounded_rectangle([PAD, _cyy, W - PAD, _cyy + _ch], radius=18, fill=_bg + (CARD_ALPHA,))
        img = PILImage.alpha_composite(img.convert("RGBA"), _layer).convert("RGB")
        draw = ImageDraw.Draw(img)

        # 테두리 + 라벨 + 텍스트(합성 후 불투명하게) — 본문 강조색은 카드별 hl_col
        for label, rendered, col, bgcol, hl_col, cyy, card_h in cards_calc:
            draw.rounded_rectangle([PAD, cyy, W - PAD, cyy + card_h],
                                   radius=18, outline=col, width=3)
            draw.text((PAD + 22, cyy + 14), label, font=f_sm, fill=col, anchor="lt")
            ty = cyy + LABEL_H
            for line_runs in rendered:
                draw_rich_line(draw, PAD + 24, ty, line_runs, f_nm, WHITE, hl_col,
                               stroke_width=2, stroke_fill=STROKE)   # 왼쪽 정렬(라벨과 맞춤)
                ty += LINE_H

        # ── 다음주 이벤트 한 줄 (있을 때만, 슬림 띠) ─────────────────
        next_events = summary.get("next_events", []) or []
        SLIM_Y = last_cy_bottom + MSG_GAP + 6
        if next_events:
            ev = next_events[0]
            date_s = ev.get("date", "")
            title_s = strip_emoji(ev.get("title", "")[:26])
            SLIM_H = 118
            draw.rounded_rectangle([PAD, SLIM_Y, W - PAD, SLIM_Y + SLIM_H],
                                   radius=14, fill=(38, 22, 62), outline=AMBER, width=2)
            # 날짜(윗줄)·이벤트 제목(아랫줄)을 분리 — 한 줄에 좌우로 붙어 겹치던 문제 해결
            draw.text((PAD + 24, SLIM_Y + 38), f"▶ {date_s}",
                      font=f_sm, fill=AMBER, anchor="lm")
            draw.text((PAD + 24, SLIM_Y + 82), title_s,
                      font=f_sm, fill=WHITE, anchor="lm",
                      stroke_width=1, stroke_fill=STROKE)
        else:
            # 폴백 자리 비움 (다음 단계 좌표 보존)
            SLIM_H = 0

        # CTA 텍스트 없음 (나레이션으로 대체)

        # ── AI 생성 고지 밴드 (최하단) ─────────────────────────────────
        # 이미지에만 그린다 — script.json lines에 넣으면 TTS가 낭독하므로 금지
        from PIL import Image as PILImage
        BAND_H = 118
        band = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        bd = ImageDraw.Draw(band)
        bd.rectangle([0, H - BAND_H, W, H], fill=(10, 14, 26, 205))
        notice_col = (170, 180, 202)
        bd.text((W // 2, H - BAND_H + 38), "본 영상은 AI 분석 툴로 수집한 뉴스 자료를 분석해",
                font=f_xs, fill=notice_col, anchor="mm")
        bd.text((W // 2, H - BAND_H + 80), "핵심 내용을 요약·정리한 영상물입니다",
                font=f_xs, fill=notice_col, anchor="mm")
        img = PILImage.alpha_composite(img.convert("RGBA"), band).convert("RGB")

        return _apply_frame_overlay(img)

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║ 씬 1 — 핵심 뉴스 3선 (호재·악재·보합, 신뢰도 우선, custom layout)   ║
    # ╚══════════════════════════════════════════════════════════════════╝
    if idx == 1:
        # ① AI 배경 풀스크린(연하게) 또는 그라데이션 폴백 — 사진 배너 없이 3카드용
        if bg_path and bg_path.exists():
            try:
                from PIL import Image as PILImage
                bg = PILImage.open(bg_path).convert("RGB")
                bw, bh = bg.size
                ratio = max(W / bw, H / bh)
                nw, nh = int(bw * ratio), int(bh * ratio)
                bg = bg.resize((nw, nh), PILImage.LANCZOS)
                ox, oy = (nw - W) // 2, (nh - H) // 2
                img.paste(bg.crop((ox, oy, ox + W, oy + H)), (0, 0))
                overlay = PILImage.new("RGB", (W, H), (12, 20, 40))
                img = PILImage.blend(img, overlay, 0.38)   # AI 이미지가 비치도록 연하게(0.55→0.38)
                draw = ImageDraw.Draw(img)
            except Exception:
                pass
        else:
            for yy in range(H):
                t = yy / H
                draw.line([(0, yy), (W, yy)], fill=(
                    int(18 + 22 * t), int(26 + 26 * t), int(48 + 40 * t)))

        # ② 헤더
        draw_mbc_header(draw, HEADER_BRAND, '"핵심 뉴스 3선"', "호재 · 악재 · 보합",
                        accent, sf_brand, sf_head_main, sf_head_sub)

        # ③ 호재/악재/보합 3카드 — summarize()가 신뢰도 우선으로 뽑은 scene1_news 사용
        s1 = summary.get("scene1_news", {}) or {}
        CARDS = [
            ("▲ 호재", GREEN, CARD_GREEN, s1.get("bullish"), "최근 뚜렷한 호재가 없어요"),
            ("▼ 악재", RED,   CARD_RED,   s1.get("bearish"), "최근 뚜렷한 악재가 없어요"),
            ("◆ 보합", AMBER, CARD_AMBER, s1.get("neutral"), "최근 뚜렷한 보합 뉴스가 없어요"),
        ]
        c_top = PHOTO_Y + 8
        c_gap = 20
        cx, cw = PAD, COL_W - PAD
        bh_b = (lambda b: b[3] - b[1])(draw.textbbox((0, 0), "가", font=f_nm))
        bh_d = (lambda b: b[3] - b[1])(draw.textbbox((0, 0), "가", font=f_sm))
        # 카드 가변 높이 — 내용 없는 카드(예: 악재 없음)는 작게, 내용 있는 카드는 남은 공간을 나눠 크게
        avail_h = SAFE_BOTTOM - c_top - c_gap * 2
        EMPTY_H = 190
        n_full  = sum(1 for c in CARDS if c[3]) or 1
        full_h  = (avail_h - (len(CARDS) - n_full) * EMPTY_H) // n_full
        card_hs = [full_h if c[3] else EMPTY_H for c in CARDS]
        # 카드 배경을 반투명(약 25% 투명)으로 먼저 깔아 뒤 AI 이미지가 비치게 한다
        from PIL import Image as PILImage
        CARD_ALPHA = 190   # 0~255 (190 ≈ 25% 투명). 이미지 비침 ↔ 텍스트 가독성 균형
        _card_layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        _cd = ImageDraw.Draw(_card_layer)
        cy = c_top
        for (_l, _col, _bgc, _n, _e), ch in zip(CARDS, card_hs):
            _cd.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius=16, fill=_bgc + (CARD_ALPHA,))
            cy += ch + c_gap
        img = PILImage.alpha_composite(img.convert("RGBA"), _card_layer).convert("RGB")
        draw = ImageDraw.Draw(img)

        cy = c_top
        for (label, col, bgc, news, empty_msg), ch in zip(CARDS, card_hs):
            draw.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius=16,
                                   outline=col, width=3)
            draw.text((cx + 22, cy + 16), label, font=f_md, fill=col, anchor="lt",
                      stroke_width=1, stroke_fill=STROKE)
            if news:
                src  = news.get("source", "")
                cred = source_credibility_tag(src)
                src_disp = (f"{src} · {cred}" if cred else src)[:34]
                if src_disp:
                    draw.text((cx + cw - 20, cy + 30), src_disp, font=f_xs,
                              fill=(190, 198, 216), anchor="rm")
                title = strip_markup(strip_emoji(news.get("title", "")))
                title = re.sub(r'^(로켓랩|Rocket\s*Lab)[,\s·]*', '', title).strip()
                ty = cy + 84
                # 헤드라인 최대 2줄(넘으면 …)
                for wl in wrap_ellipsis(draw, title, f_nm, cw - 44, 2):
                    draw.text((cx + 22, ty), wl, font=f_nm, fill=WHITE,
                              stroke_width=1, stroke_fill=STROKE)
                    ty += bh_b + 12
                detail = strip_markup(strip_emoji(news.get("reason", "")))
                ty += 6
                # 디테일 줄 수를 카드 잔여 높이로 동적 계산(큰 카드면 더 많이, 넘으면 마지막 줄 …)
                avail = (cy + ch - 14) - ty
                fit_lines = max(1, min(6, (avail + 10) // (bh_d + 10)))
                for wl in wrap_ellipsis(draw, detail, f_sm, cw - 44, fit_lines):
                    if ty + bh_d > cy + ch - 14:
                        break
                    draw.text((cx + 24, ty), wl, font=f_sm, fill=(206, 212, 228),
                              stroke_width=1, stroke_fill=STROKE)
                    ty += bh_d + 10
            else:
                draw.text((cx + cw // 2, cy + ch // 2), empty_msg, font=f_nm,
                          fill=(150, 158, 180), anchor="mm")
            cy += ch + c_gap

        return _apply_frame_overlay(img)

    # ── 씬별 헤드라인 텍스트 결정 (MBC 스타일) ──────────────────────────
    if idx == 0:
        # 메인: 대본 첫 줄 그대로. 큰따옴표 추가.
        first = strip_markup(news_lines[0] if news_lines else f"최근 {COMPANY_KO}").strip()
        if not (first.startswith('"') or first.startswith("'")):
            first = f'"{first}"'
        head_main = first
        # 부제: 현재 주가 + 전일 변동 + 윈도우(2일) 누적 변동률 — 기준 시점을 명시해 오독 방지
        price = summary.get("latest_price")
        wc    = summary.get("week_change_pct")
        price_s = fmt_price(price)
        # 전일 변동률: Yahoo 일봉(확정 종가) 우선, 실패 시 세션 스냅숏(today_change_pct) 폴백
        pd_pct = (summary.get("prev_day") or {}).get("change_pct")
        if pd_pct is None:
            pd_pct = summary.get("today_change_pct")
        # 서브 한 줄(폭 W-80)에 확실히 들어가게 실측하며 단계적 압축(1소수점 → 화살표 생략 → 공백 축소)
        arrow = ("▲" if wc >= 0 else "▼") if wc is not None else ""
        for fmt in ("full", "no_arrow", "tight"):
            sub_parts = [p for p in [price_s] if p]
            if pd_pct is not None:
                sub_parts.append(f"전일{' ' if fmt != 'tight' else ''}{pd_pct:+.1f}%")
            if wc is not None:
                a = arrow if fmt == "full" else ""
                sub_parts.append(f"{LOOKBACK_DAYS}일{' ' if fmt != 'tight' else ''}{a}{wc:+.1f}%")
            head_sub = " · ".join(sub_parts) or "시황 브리핑"
            if draw.textlength(head_sub, font=f_head_sub) <= W - 80:
                break

    # ── 상단 헤더 (Y=0~500) — 네이비 박스 + 브랜드 + 두줄 헤드라인 ──────
    # 여기는 씬0만 도달(씬1·2는 위에서 커스텀 레이아웃으로 조기 return)
    draw_mbc_header(draw, HEADER_BRAND, head_main, head_sub, accent,
                    f_brand, f_head_main, f_head_sub)

    # ── 사진 배너 (Y=500~1000, 500px) ────────────────────────────────────
    draw_photo_card(img, draw, accent, bg_path, x=0, y=PHOTO_Y, w=W, h=PHOTO_H)
    draw = ImageDraw.Draw(img)

    # ── 전일 시가→종가 스트립 (사진 배너 하단, Yahoo 일봉 확보 시에만) ──
    prev_day = summary.get("prev_day") or {}
    if prev_day.get("open") and prev_day.get("close"):
        from PIL import Image as PILImage
        STRIP_H = 64
        _strip = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        _sd = ImageDraw.Draw(_strip)
        _sd.rectangle([0, PHOTO_Y + PHOTO_H - STRIP_H, W, PHOTO_Y + PHOTO_H],
                      fill=(10, 14, 26, 205))
        img = PILImage.alpha_composite(img.convert("RGBA"), _strip).convert("RGB")
        draw = ImageDraw.Draw(img)
        _pdp = prev_day.get("change_pct")
        _pct_s = f" ({'+' if _pdp >= 0 else ''}{_pdp}%)" if _pdp is not None else ""
        _pct_col = GREEN if (_pdp or 0) >= 0 else RED
        _txt = f"전일 시가 {fmt_price(prev_day['open'])} → 종가 {fmt_price(prev_day['close'])}"
        _tw = draw.textlength(_txt + _pct_s, font=f_sm)
        _tx = (W - _tw) // 2
        draw.text((_tx, PHOTO_Y + PHOTO_H - STRIP_H // 2), _txt,
                  font=f_sm, fill=(220, 226, 240), anchor="lm")
        if _pct_s:
            draw.text((_tx + draw.textlength(_txt, font=f_sm),
                       PHOTO_Y + PHOTO_H - STRIP_H // 2), _pct_s,
                      font=f_sm, fill=_pct_col, anchor="lm")

    # 푸터 텍스트는 자막+UI에 가려지므로 제거

    # ── 씬 0: 주간 브리핑 — 본문 영역 (4줄 대본 → 3카드 레이아웃) ──────────
    CONTENT_Y = START_Y + 40   # 사진 하단과 본문 사이 40px 여백
    if idx == 0:
        FC_W = COL_W - PAD
        CARD_GAP = 16
        TOTAL_H  = SAFE_BOTTOM - CONTENT_Y   # 약 640px

        # ─ 회사가 추구하는 방향: 씬0은 '주가 분석'이 아니라 회사 방향 소개 중심(사용자 요청).
        #   간략한 주가 흐름은 상단 헤더(현재가·변동률)·전일 스트립이 담당하고,
        #   본문 카드는 대본 줄2~(회사 방향·비전)를 보여준다 ─
        dir_lines = [strip_markup(strip_emoji(l)).strip()
                     for l in news_lines[1:] if strip_markup(strip_emoji(l)).strip()]
        if not dir_lines:
            dir_lines = [f"{COMPANY_KO}는 {INDUSTRY_KO} 분야의 성장을 추구하고 있어요" if INDUSTRY_KO
                         else f"{COMPANY_KO}의 사업 방향을 살펴볼게요"]

        CARD_Y = CONTENT_Y
        CARD_H = TOTAL_H

        # ─ 반투명 카드 배경 레이어(씬1과 동일한 비침 효과) ─
        from PIL import Image as PILImage
        CARD_ALPHA = 190
        _layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        _ld = ImageDraw.Draw(_layer)
        _ld.rounded_rectangle([PAD, CARD_Y, PAD + FC_W, CARD_Y + CARD_H], radius=14, fill=CARD_BG + (CARD_ALPHA,))
        img = PILImage.alpha_composite(img.convert("RGBA"), _layer).convert("RGB")
        draw = ImageDraw.Draw(img)

        draw.rounded_rectangle([PAD, CARD_Y, PAD + FC_W, CARD_Y + CARD_H],
                               radius=14, outline=accent, width=3)
        draw.text((PAD + 20, CARD_Y + 14), "회사가 추구하는 방향", font=f_sm, fill=accent, anchor="lt")

        # 본문: 방향 줄들을 공통 42px(f_nm, 줄간 58)로 렌더, 폭 초과 시 랩(넘치면 …), 세로 중앙 정렬
        LINE_H = 58
        avail = max(1, (CARD_H - 66 - 16) // LINE_H)
        wrapped = []
        for dl in dir_lines:
            for wl in wrap_ellipsis(draw, dl, f_nm, FC_W - 40, 3):
                wrapped.append(wl)
        wrapped = wrapped[:avail]
        block_h = len(wrapped) * LINE_H
        ky = CARD_Y + 66 + max(0, (CARD_H - 66 - 16 - block_h) // 2)
        for wl in wrapped:
            bb = draw.textbbox((0, 0), wl, font=f_nm)
            draw.text(((W - (bb[2] - bb[0])) // 2, ky), wl,
                      font=f_nm, fill=WHITE, stroke_width=1, stroke_fill=STROKE)
            ky += LINE_H

    # (씬 1은 위 "핵심 뉴스 3선" 커스텀 블록에서 처리하고 조기 return)

    return _apply_frame_overlay(img)


def build_images(scenes, summary, out_dir, img_prompts=None):
    try:
        from PIL import ImageFont
    except ImportError:
        print("   ⚠ Pillow 없음 — 이미지 건너뜀", file=sys.stderr)
        return

    font_reg, font_bold = find_font()
    if not font_reg:
        print("   ⚠ 한글 폰트 없음 — 이미지 건너뜀", file=sys.stderr)
        return

    if img_prompts is None:
        img_prompts = {}

    # 모든 씬에 AI 배경 이미지 생성
    BG_SCENES = {0, 1, 2}
    # 씬별 aspect ratio — 0·1은 가로 strip(16:9), 2(미래비전)는 풀스크린(9:16)
    BG_ASPECTS = {0: "16:9", 1: "16:9", 2: "9:16"}

    print("   🖼 배경 이미지 준비 중...")
    bg_paths = {}
    for scene in scenes:
        idx      = scene["index"]
        bg_path  = out_dir / f"bg_{idx:02d}.jpg"

        if idx not in BG_SCENES:
            bg_paths[idx] = None
            continue

        # 1순위: Nano Banana AI 이미지 (GEMINI_API_KEY 필요)
        prompt = img_prompts.get(idx, "")
        aspect = BG_ASPECTS.get(idx, "16:9")
        if prompt:
            ok = fetch_nano_banana_image(prompt, bg_path, aspect_ratio=aspect)
            if ok:
                bg_paths[idx] = bg_path
                print(f"      씬{idx} [Nano Banana AI · {aspect}] ✅")
                continue
            print(f"      씬{idx} Nano Banana 실패 → 정적 배경/그라데이션 폴백", file=sys.stderr)

        # 2순위: 로컬 정적 배경 (data/scene-backgrounds/ — config scene_static_bg_files)
        # ※ Wikipedia 폴백은 제거 — 대표이미지가 회사 로고라 풀스크린 배경으로 부적합(영상이 조잡해짐).
        #   AI 실패 시엔 정적 배경(있으면) 또는 깔끔한 그라데이션/다크 카드로 자연스럽게 떨어진다.
        static = SCENE_STATIC_BG[idx] if idx < len(SCENE_STATIC_BG) else None
        if static and Path(static).exists():
            import shutil
            shutil.copyfile(static, bg_path)
            bg_paths[idx] = bg_path
            print(f"      씬{idx} [정적 배경: {Path(static).name}] ✅")
            continue

        # 최종: 기본 배경 (씬0·1 다크 카드 / 씬2 그라데이션) — bg 없음으로 처리
        bg_paths[idx] = None
        print(f"      씬{idx} AI·정적 모두 없음 → 기본 배경(그라데이션/다크 카드)", file=sys.stderr)

    for scene in scenes:
        idx  = scene["index"]
        img  = build_scene_image(scene, summary, font_reg, font_bold, bg_paths.get(idx))
        path = out_dir / f"scene_{idx:02d}.png"
        img.save(path, "PNG")
        print(f"   ✅ scene_{idx:02d}.png 저장")

# ── 메인 ──────────────────────────────────────────────────────────────────

def main():
    KST = timezone(timedelta(hours=9))
    today   = datetime.now(KST).strftime("%Y-%m-%d")   # KST 기준 날짜
    out_dir = OUTPUT_BASE / today
    out_dir.mkdir(parents=True, exist_ok=True)

    # 양산형 탈피: 생성일 시드로 인트로/클로징(썸네일) 색상 테마 로테이션 (씬1 호재는 초록 유지)
    global SCENE_ACCENTS
    SCENE_ACCENTS = ACCENT_THEMES[_theme_idx(today)]
    print(f"   🎨 색상 테마 #{_theme_idx(today)} 적용 (격일 생성마다 변형)")

    print("📊 주간 세션 로드...")
    sessions = load_week_sessions()
    if not sessions:
        print("⚠ 최근 7일 세션 없음 — 종료", file=sys.stderr)
        sys.exit(0)

    summary = summarize(sessions)
    print(f"   {summary['week_start']} ~ {summary['week_end']} / {summary['session_count']}개 세션")
    print(f"   평균 매수지수: {summary['avg_buy_index']} / 현재가: ${summary['latest_price']}")
    if summary.get("today_change_pct") is not None:
        print(f"   오늘 변동: {summary['today_change_pct']:+.2f}%")

    # ── 전일(간밤) 시가·종가 (Yahoo 일봉, 선택적) ──
    summary["prev_day"] = fetch_prev_day_ohlc(TICKER)
    if summary["prev_day"]:
        pd_ = summary["prev_day"]
        pct_ = f" ({pd_['change_pct']:+.2f}%)" if pd_.get("change_pct") is not None else ""
        print(f"   전일({pd_['date']}) 시가 ${pd_['open']} → 종가 ${pd_['close']}{pct_}")

    # ── Google Trends 수집 ──
    print("📈 Google Trends 수집 중...")
    summary["trends"] = fetch_google_trends(GOOGLE_TRENDS_KEYWORDS)
    if summary["trends"]:
        print(f"   검색량 {summary['trends']['ratio']}배 변화 (최고: {summary['trends']['top_keyword']})")

    # ── Calendar 이벤트 ──
    summary["next_events"] = load_next_events()
    if summary["next_events"]:
        print(f"   다음주 이벤트 {len(summary['next_events'])}건 발견")

    # ── 주가 변동 원인 (Google Search grounding) ──
    print("🔍 주가 변동 원인 검색 중...")
    summary["movement_reason"] = search_movement_reason(summary)

    # ── 회사가 추구하는 방향·최근 투자 (Google Search grounding — 씬0 소재) ──
    print("🧭 회사 방향·최근 투자 검색 중...")
    summary["company_direction"] = search_company_direction()

    # ── 대본 ──
    img_prompts = {}  # Nano Banana 이미지 생성에 사용 (대본 생성 시 채워짐)
    if not ANTHROPIC_API_KEY and not GEMINI_API_KEY:
        print("⚠ API 키 없음 — 대본 생성 건너뜀", file=sys.stderr)
        scenes = [{"index": i, "title": f"씬 {i}", "lines": [], "body": ""} for i in range(0, 3)]
    else:
        print("✍ 대본 생성 중...")
        raw    = generate_script(summary)
        scenes = parse_script(raw)
        img_prompts = parse_image_prompts(raw)

        # 대시보드용 title/subtitle — 씬0(주간브리핑) 첫 줄에서 추출
        script_title = ""
        script_subtitle = f"{summary['week_start']} ~ {summary['week_end']}"
        scene1 = next((s for s in scenes if s["index"] == 0), None)
        if scene1 and scene1.get("lines"):
            first_line = scene1["lines"][0] if scene1["lines"] else ""
            script_title = strip_emoji(first_line).strip('"').strip("'").strip()
        if summary.get("biggest_impact"):
            bi_title = summary["biggest_impact"].get("title", "")
            if bi_title:
                script_subtitle += f" · {bi_title[:30]}"

        with open(out_dir / "script.txt", "w", encoding="utf-8") as f:
            f.write(raw)
        with open(out_dir / "script.json", "w", encoding="utf-8") as f:
            json.dump({
                "generated_at": today,
                "generated_by": _last_model,
                "title": script_title,
                "subtitle": script_subtitle,
                "summary": summary,
                "scenes": scenes,
                "image_prompts": img_prompts,
            }, f, ensure_ascii=False, indent=2)

        # ── 이미지 프롬프트 별도 저장 (Imagen 복붙용) ──
        if img_prompts:
            lines = [f"# {TICKER} 주간 배경 이미지 프롬프트 — {today}",
                     "# Gemini Imagen에 씬별로 붙여넣기 하세요.\n"]
            scene_names = {0: "씬0 주간브리핑", 1: "씬1 호재심층", 2: "씬2 다음주전망"}
            for i in range(0, 3):
                if i in img_prompts:
                    lines.append(f"## {scene_names[i]}")
                    lines.append(img_prompts[i])
                    lines.append("")
            with open(out_dir / "image_prompts.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            print(f"   🎨 image_prompts.txt 저장 완료 ({len(img_prompts)}개 씬)")
        print(f"   ✅ 대본 저장 완료")

    # ── 이미지 ──
    print("🖼 카드 이미지 생성 중...")
    build_images(scenes, summary, out_dir, img_prompts)

    # ── 메타 ──
    with open(out_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump({
            "generated_at":    today,
            "week_start":      summary["week_start"],
            "week_end":        summary["week_end"],
            "avg_buy_index":   summary["avg_buy_index"],
            "latest_price":    summary["latest_price"],
            "session_count":   summary["session_count"],
            "today_change_pct": summary.get("today_change_pct"),
            "trends":          summary.get("trends"),
        }, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 완료: {out_dir}/")
    print(f"   📄 script.txt  — 영상 대본 (5씬, 인트로+클로징 포함)")
    print(f"   🖼 scene_00~04.png — 씬별 배경 카드 이미지 (1080×1920, YouTube Shorts 세로 포맷)")


if __name__ == "__main__":
    main()
