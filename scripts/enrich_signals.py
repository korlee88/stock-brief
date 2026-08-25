#!/usr/bin/env python3
"""온디맨드 세션에 정량 신호 보강 — 뉴스만으로 부족한 예측 근거를 채운다(사용자 요청).

on-demand-collect.js(뉴스+현재가) 이후, weekly_video_prep.py 이전에 실행.
session.json 의 세션 객체에 `signals` 필드를 추가한다. 전부 **선택적 데이터**로,
어떤 수집이 실패해도 파이프라인은 계속된다(부분 신호라도 담고 exit 0).

담는 신호:
  · technicals (전 종목, Yahoo 일봉+numpy): 20/60일 이동평균 대비 위치·추세(골든/데드),
    RSI14(과열/과매도), MACD 모멘텀, 20일 변동성, 52주 위치, 5·20일 모멘텀
  · supply_demand (한국 .KS/.KQ, pykrx): 최근 5거래일 외국인·기관 순매수(수급)
  · valuation (한국 pykrx PER/PBR/EPS · 미국 yfinance PER/목표주가 컨센서스 범위·투자의견)
  · analyst_reports (한국만, 네이버 금융 리서치): 최근 증권사 리포트 — 증권사명·날짜·
    제목, 목표주가는 제목에 명시된 경우만 파싱(추측 금지). 대형 IB 개별 목표주가는
    미국은 무료로 구하기 어려워 컨센서스 범위로 대체, 한국은 증권사 리포트로 보강.
  · dart_financials (한국만, DART 오픈API 주요계정): 최근 확정 분기·연간 재무제표의
    매출액·영업이익·당기순이익·자산총계·부채총계·자본총계(연결 우선, 없으면 별도).
    DART_API_KEY 필요(무료 발급) — 없으면 조용히 생략(선택 신호).

env: SESSIONS_FILE(세션 json 경로), TICKER_CONFIG(회사 config). 둘 다 필수.
    DART_API_KEY(선택 — 없으면 재무제표 신호만 생략).
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

CFG_PATH = os.environ.get("TICKER_CONFIG")
SESS_PATH = os.environ.get("SESSIONS_FILE")
if not CFG_PATH or not SESS_PATH or not os.path.exists(CFG_PATH) or not os.path.exists(SESS_PATH):
    print(f"⚠ enrich_signals: 경로 없음 (TICKER_CONFIG={CFG_PATH}, SESSIONS_FILE={SESS_PATH}) — 건너뜀",
          file=sys.stderr)
    sys.exit(0)

cfg = json.loads(open(CFG_PATH, encoding="utf-8").read())
TICKER = cfg["ticker"]
COMPANY_KO = cfg.get("company_ko", TICKER)
IS_KR = TICKER.upper().endswith((".KS", ".KQ"))
KR_CODE = TICKER.split(".")[0] if IS_KR else None


# ── 가격 히스토리 (Yahoo 일봉 — 한·미 공통) ──────────────────────────────────
def fetch_daily_closes(ticker, rng="1y"):
    for host in ("query1", "query2"):
        try:
            url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}"
                   f"?range={rng}&interval=1d")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.loads(r.read())
            res = (j.get("chart", {}).get("result") or [None])[0]
            if not res:
                continue
            q = res["indicators"]["quote"][0]
            closes = [c for c in q.get("close", []) if c is not None]
            highs = [c for c in q.get("high", []) if c is not None]
            lows = [c for c in q.get("low", []) if c is not None]
            if len(closes) >= 30:
                return closes, highs, lows
        except Exception as e:
            print(f"   ⚠ Yahoo 히스토리 실패({host}): {e}", file=sys.stderr)
    return None, None, None


def compute_technicals(closes, highs, lows):
    import numpy as np
    c = np.array(closes, dtype=float)
    price = float(c[-1])
    out = {"price": round(price, 2)}

    def ma(n):
        return float(np.mean(c[-n:])) if len(c) >= n else None
    ma20, ma60 = ma(20), ma(60)
    if ma20:
        out["ma20"] = round(ma20, 2)
        out["vs_ma20_pct"] = round((price - ma20) / ma20 * 100, 1)
    if ma60:
        out["ma60"] = round(ma60, 2)
    if ma20 and ma60:
        out["trend"] = "상승추세(20일선>60일선)" if ma20 >= ma60 else "하락추세(20일선<60일선)"

    # RSI(14) — Wilder
    if len(c) >= 15:
        d = np.diff(c[-15:])
        up = d[d > 0].sum()
        down = -d[d < 0].sum()
        rs = up / down if down > 0 else float("inf")
        rsi = 100 - 100 / (1 + rs) if down > 0 else 100.0
        out["rsi14"] = round(rsi, 1)
        out["rsi_state"] = ("과매수(과열)" if rsi >= 70 else
                            "과매도(침체)" if rsi <= 30 else "중립")

    # MACD(12,26,9) 모멘텀 방향
    if len(c) >= 35:
        def ema(arr, n):
            k = 2 / (n + 1)
            e = arr[0]
            for x in arr[1:]:
                e = x * k + e * (1 - k)
            return e
        macd_series = []
        for i in range(26, len(c) + 1):
            w = c[:i]
            macd_series.append(ema(w[-26:], 12) - ema(w[-26:], 26))
        if len(macd_series) >= 9:
            macd = macd_series[-1]
            signal = ema(macd_series[-9:], 9)
            out["macd_momentum"] = "상승 모멘텀" if macd >= signal else "하락 모멘텀"

    # 20일 변동성(일간수익률 표준편차 %)
    if len(c) >= 21:
        rets = np.diff(c[-21:]) / c[-21:-1]
        out["volatility_20d_pct"] = round(float(np.std(rets) * 100), 1)

    # 52주 위치
    if highs and lows and len(highs) >= 60:
        hi, lo = max(highs), min(lows)
        if hi > lo:
            out["pos_52w_pct"] = round((price - lo) / (hi - lo) * 100, 0)
            out["high_52w"] = round(hi, 2)
            out["low_52w"] = round(lo, 2)

    # 모멘텀
    def mom(n):
        return round((price - c[-n - 1]) / c[-n - 1] * 100, 1) if len(c) > n else None
    if mom(5) is not None:
        out["mom_5d_pct"] = mom(5)
    if mom(20) is not None:
        out["mom_20d_pct"] = mom(20)
    return out


# ── 한국: pykrx 수급(외국인·기관)·밸류 ───────────────────────────────────────
def fetch_kr_signals(code):
    sd, val = {}, {}
    try:
        from pykrx import stock
        today = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y%m%d")
        frm = (datetime.utcnow() + timedelta(hours=9) - timedelta(days=40)).strftime("%Y%m%d")
        # 투자자별 순매수 거래대금 (최근 ~5거래일 합)
        try:
            df = stock.get_market_trading_value_by_date(frm, today, code)
            if df is not None and len(df):
                recent = df.tail(5)
                for label, key in (("외국인", "외국인"), ("기관", "기관")):
                    col = next((c for c in df.columns if key in str(c)), None)
                    if col is not None:
                        net = float(recent[col].sum())
                        sd[label + "_순매수_5일_억"] = round(net / 1e8, 1)  # 원 → 억원
        except Exception as e:
            print(f"   ⚠ pykrx 수급 실패: {e}", file=sys.stderr)
        # 밸류에이션
        try:
            fdf = stock.get_market_fundamental(frm, today, code)
            if fdf is not None and len(fdf):
                last = fdf.iloc[-1]
                for k in ("PER", "PBR", "EPS", "DIV"):
                    if k in fdf.columns:
                        v = float(last[k])
                        if v == v:  # not NaN
                            val[k] = round(v, 2)
        except Exception as e:
            print(f"   ⚠ pykrx 밸류 실패: {e}", file=sys.stderr)
    except ImportError:
        print("   ⚠ pykrx 미설치 — 한국 수급/밸류 건너뜀", file=sys.stderr)
    return sd, val


# ── 한국: 네이버 금융 리서치 — 대형 증권사 개별 리포트(목표주가는 명시된 경우만) ──
_NAVER_RESEARCH_URL = ("https://finance.naver.com/research/company_list.naver"
                        "?searchType=itemCode&itemCode={code}")
# "목표주가 95,000원", "목표가: 95000원" 처럼 명시적으로 적힌 경우만 인정 —
# 제목의 다른 숫자를 목표가로 오인하지 않기 위해 키워드 근접 매칭만 사용.
_TARGET_PRICE_RE = re.compile(r'목표\s?주?가\s*[:：]?\s*([0-9][0-9,]{3,})\s*원')


class _NaverResearchParser(HTMLParser):
    """네이버 금융 개별 종목 리서치 리스트 — <tr>당 <td> 5개(종목명·제목·증권사·첨부·작성일) 추출."""
    def __init__(self):
        super().__init__()
        self.rows = []
        self._cells = None
        self._in_td = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._cells = []
        elif tag == "td" and self._cells is not None:
            self._in_td, self._buf = True, []

    def handle_data(self, data):
        if self._in_td:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self._in_td:
            self._in_td = False
            self._cells.append("".join(self._buf).strip())
        elif tag == "tr" and self._cells is not None:
            if len(self._cells) >= 5:
                self.rows.append(self._cells[:5])
            self._cells = None


def fetch_kr_analyst_reports(code, max_items=3):
    """네이버 금융 리서치에서 최근 대형 증권사 리포트(증권사명·날짜·제목) 조회.
    목표주가는 제목에 명시된 경우만 파싱해 담는다(없으면 숫자 없이 맥락만 제공 —
    추측한 숫자를 목표주가로 오인시키지 않기 위함). 페이지 구조 변경 등 실패해도
    조용히 빈 리스트 반환(비차단, 실호출 검증은 CI 러너에서만 가능 — 샌드박스 차단)."""
    try:
        url = _NAVER_RESEARCH_URL.format(code=code)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("euc-kr", errors="replace")
        parser = _NaverResearchParser()
        parser.feed(html)
        out = []
        for cells in parser.rows:
            _name, title, firm, _attach, date = cells
            # 헤더행·빈행 제거: 증권사·작성일 칸이 실제 데이터여야 함
            if not firm or firm in ("증권사", "종목명") or not date or "." not in date:
                continue
            item = {"firm": firm, "date": date, "title": title}
            m = _TARGET_PRICE_RE.search(title)
            if m:
                item["target_price"] = int(m.group(1).replace(",", ""))
            out.append(item)
            if len(out) >= max_items:
                break
        return out
    except Exception as e:
        print(f"   ⚠ 네이버 리서치 조회 실패: {e}", file=sys.stderr)
        return []


# ── 한국: DART(전자공시) 재무제표 핵심 계정(매출액·영업이익·당기순이익 등) ──────────
# 종목코드 대신 8자리 고유번호(corp_code)가 필요 — build_dart_corp_codes.py가 미리
# 받아둔 data/dart-corp-codes.json에서 조회(사용자 요청 — DART 재무제표 요약 추가).
DART_API_KEY = os.environ.get("DART_API_KEY", "")
_DART_CORP_CODES_PATH = Path(__file__).resolve().parent.parent / "data" / "dart-corp-codes.json"
_DART_REPRT_LABEL = {"11013": "1분기", "11012": "반기", "11014": "3분기", "11011": "사업보고서(연간)"}
_DART_KEY_ACCOUNTS = ["매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계"]


def _dart_corp_code(code):
    if not _DART_CORP_CODES_PATH.exists():
        return None
    try:
        mapping = json.loads(_DART_CORP_CODES_PATH.read_text(encoding="utf-8"))
        return mapping.get(code)
    except Exception:
        return None


def _dart_report_period_candidates():
    """최근 발표분부터 역순으로 (연도, 보고서코드) 후보 나열 — 분기 발표 지연을
    감안해 최근 ~1.5년 범위를 넉넉히 훑는다(첫 성공에서 멈춤)."""
    y = datetime.utcnow().year
    quarters = ["11014", "11012", "11013"]   # 3분기 → 반기 → 1분기(당해 연도)
    return ([(y, c) for c in quarters]
            + [(y - 1, "11011")]
            + [(y - 1, c) for c in quarters]
            + [(y - 2, "11011")])


def fetch_dart_financials(code):
    """DART 오픈API(fnlttSinglAcnt, 주요계정)로 최근 확정 재무제표 핵심 계정 조회.
    DART_API_KEY 없거나 종목의 corp_code 매핑이 없으면 None(비차단, 선택 신호).
    연결재무제표(CFS) 우선, 없으면 별도(OFS)."""
    if not DART_API_KEY:
        return None
    corp_code = _dart_corp_code(code)
    if not corp_code:
        return None
    for year, reprt_code in _dart_report_period_candidates():
        try:
            params = urllib.parse.urlencode({
                "crtfc_key": DART_API_KEY, "corp_code": corp_code,
                "bsns_year": str(year), "reprt_code": reprt_code,
            })
            url = f"https://opendart.fss.or.kr/api/fnlttSinglAcnt.json?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            if data.get("status") != "000":
                continue   # 해당 분기 미공시 — 다음 후보로
            rows = data.get("list") or []
            fs_rows = [row for row in rows if row.get("fs_div") == "CFS"] or rows
            picked = {}
            for row in fs_rows:
                name = row.get("account_nm", "")
                if name in _DART_KEY_ACCOUNTS and name not in picked:
                    try:
                        picked[name] = int(str(row.get("thstrm_amount", "")).replace(",", ""))
                    except (ValueError, TypeError):
                        continue
            if picked:
                picked["period"] = f"{year}년 {_DART_REPRT_LABEL.get(reprt_code, reprt_code)}"
                return picked
        except Exception as e:
            print(f"   ⚠ DART 재무제표 조회 실패({year}/{reprt_code}): {e}", file=sys.stderr)
            continue
    return None


# ── 미국: yfinance 목표주가 컨센서스(평균·최고·최저·참여 인원)·투자의견·PER ──────────
# 개별 대형 IB(골드만삭스 등) 명의 목표주가는 무료로 구할 신뢰 가능한 소스가 없어
# (TipRanks·Benzinga 등은 유료) 컨센서스 범위로 대체(사용자 확인 사항).
def fetch_us_signals(ticker):
    val = {}
    try:
        import yfinance as yf
        info = {}
        try:
            info = yf.Ticker(ticker).get_info() or {}
        except Exception:
            info = getattr(yf.Ticker(ticker), "info", {}) or {}
        tmp = info.get("targetMeanPrice")
        if tmp:
            val["목표주가_평균"] = round(float(tmp), 2)
        for src, dst in (("targetHighPrice", "목표주가_최고"), ("targetLowPrice", "목표주가_최저")):
            if info.get(src):
                try:
                    val[dst] = round(float(info[src]), 2)
                except (TypeError, ValueError):
                    pass
        noo = info.get("numberOfAnalystOpinions")
        if noo:
            val["애널리스트_수"] = int(noo)
        rk = info.get("recommendationKey")
        if rk and rk != "none":
            val["투자의견"] = {"strong_buy": "적극매수", "buy": "매수", "hold": "중립",
                            "sell": "매도", "strong_sell": "적극매도"}.get(rk, rk)
        for src, dst in (("forwardPE", "PER_선행"), ("trailingPE", "PER")):
            if info.get(src):
                try:
                    val[dst] = round(float(info[src]), 1)
                except (TypeError, ValueError):
                    pass
    except ImportError:
        print("   ⚠ yfinance 미설치 — 미국 밸류/목표가 건너뜀", file=sys.stderr)
    except Exception as e:
        print(f"   ⚠ yfinance 실패: {e}", file=sys.stderr)
    return val


def main():
    print(f"📊 {COMPANY_KO}({TICKER}) 정량 신호 보강 중 (기술지표·수급·밸류)...")
    signals = {}

    closes, highs, lows = fetch_daily_closes(TICKER)
    if closes:
        try:
            signals["technicals"] = compute_technicals(closes, highs, lows)
            t = signals["technicals"]
            print(f"   ✅ 기술지표: RSI {t.get('rsi14')} · {t.get('trend','')} · {t.get('macd_momentum','')}")
        except Exception as e:
            print(f"   ⚠ 기술지표 계산 실패: {e}", file=sys.stderr)
    else:
        print("   ⚠ 가격 히스토리 없음 — 기술지표 생략", file=sys.stderr)

    if IS_KR:
        sd, val = fetch_kr_signals(KR_CODE)
        if sd:
            signals["supply_demand"] = sd
            print(f"   ✅ 수급(5일): {sd}")
        if val:
            signals["valuation"] = val
            print(f"   ✅ 밸류: {val}")
        reports = fetch_kr_analyst_reports(KR_CODE)
        if reports:
            signals["analyst_reports"] = reports
            print(f"   ✅ 증권사 리포트 {len(reports)}건: "
                  + ", ".join(f"{r['firm']}({r['date']})" for r in reports))
        dart = fetch_dart_financials(KR_CODE)
        if dart:
            signals["dart_financials"] = dart
            print(f"   ✅ DART 재무제표({dart.get('period')}): "
                  + ", ".join(f"{k} {v:,}" for k, v in dart.items() if k != "period"))
    else:
        val = fetch_us_signals(TICKER)
        if val:
            signals["valuation"] = val
            print(f"   ✅ 밸류/목표가: {val}")

    if not signals:
        print("   ℹ 보강할 신호 없음(전부 실패) — 뉴스만으로 진행", file=sys.stderr)
        sys.exit(0)

    sessions = json.loads(open(SESS_PATH, encoding="utf-8").read())
    if isinstance(sessions, list) and sessions:
        sessions[0]["signals"] = signals
        open(SESS_PATH, "w", encoding="utf-8").write(
            json.dumps(sessions, ensure_ascii=False, indent=2) + "\n")
        print(f"   📌 session.json 에 signals 보강 완료 ({', '.join(signals)})")


if __name__ == "__main__":
    main()
