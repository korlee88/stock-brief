#!/usr/bin/env python3
"""온디맨드 세션에 정량 신호 보강 — 뉴스만으로 부족한 예측 근거를 채운다(사용자 요청).

on-demand-collect.js(뉴스+현재가) 이후, weekly_video_prep.py 이전에 실행.
session.json 의 세션 객체에 `signals` 필드를 추가한다. 전부 **선택적 데이터**로,
어떤 수집이 실패해도 파이프라인은 계속된다(부분 신호라도 담고 exit 0).

담는 신호:
  · technicals (전 종목, Yahoo 일봉+numpy): 20/60일 이동평균 대비 위치·추세(골든/데드),
    RSI14(과열/과매도), MACD 모멘텀, 20일 변동성, 52주 위치, 5·20일 모멘텀
  · supply_demand (한국 .KS/.KQ, pykrx): 최근 5거래일 외국인·기관 순매수(수급)
  · valuation (한국 pykrx PER/PBR/EPS · 미국 yfinance PER/목표주가/투자의견)

env: SESSIONS_FILE(세션 json 경로), TICKER_CONFIG(회사 config). 둘 다 필수.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta

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


# ── 미국: yfinance 목표주가·투자의견·PER ──────────────────────────────────────
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
