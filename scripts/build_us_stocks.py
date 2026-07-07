#!/usr/bin/env python3
"""미국 상장 종목(회사명·티커) 리스트를 내려받아 data/us-stocks.json 으로 저장.

on-demand.html 의 '미국 종목 코드 찾기'가 이 파일을 fetch 해서 로컬 검색한다.
한국(build_kr_stocks.py)과 동일한 방식 — 목록은 자주 안 바뀌므로 한 번 받아 커밋한다.

    python3 scripts/build_us_stocks.py            # 최신 목록 받아 data/us-stocks.json 갱신

데이터 소스: FinanceDataReader — `fdr.StockListing('NASDAQ'/'NYSE'/'AMEX')`.
  워크플로(update-kr-stocks.yml)에서 pip 설치 후 실행. 러너/로컬에서 실행.

출력 형식(온디맨드 페이지·한국 목록과 동일): [["Apple Inc.","AAPL"], ["Tesla, Inc.","TSLA"], ...]
  · 미국 티커는 접미사 없음(Yahoo 규칙). 클래스주 등 '.'·'/'는 '-'로 정규화(BRK.B→BRK-B).
  · 티커 가나다 정렬, 티커 중복 제거
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "data" / "us-stocks.json"

US_MARKETS = ["NASDAQ", "NYSE", "AMEX"]
_TICKER_RE = re.compile(r"^[A-Z0-9\-]{1,7}$")


def _norm_symbol(sym):
    """Yahoo Finance 티커로 정규화: 대문자, 클래스주 구분자('.'/'/')→'-'."""
    s = str(sym).strip().upper().replace(".", "-").replace("/", "-")
    return s if _TICKER_RE.match(s) else None


def fetch_via_fdr():
    import FinanceDataReader as fdr   # 미설치 시 ImportError
    out = []
    for market in US_MARKETS:
        try:
            df = fdr.StockListing(market)
        except Exception as e:
            print(f"  ⚠ {market} 실패: {e}", file=sys.stderr)
            continue
        cols = {c.lower(): c for c in df.columns}
        sym_c  = cols.get("symbol") or cols.get("code") or cols.get("ticker")
        name_c = cols.get("name")
        if not (sym_c and name_c):
            print(f"  ⚠ {market} 예상 컬럼 없음 (컬럼: {list(df.columns)})", file=sys.stderr)
            continue
        n = 0
        for _, row in df.iterrows():
            sym = _norm_symbol(row[sym_c])
            name = str(row[name_c]).strip()
            if sym and name and name.lower() != "nan":
                out.append((name, sym))
                n += 1
        print(f"  {market}: {n}종목")
    return out


def main():
    try:
        rows = fetch_via_fdr()
    except ImportError:
        print("❌ FinanceDataReader 미설치 — `pip install finance-datareader` 필요", file=sys.stderr)
        sys.exit(1)

    if not rows:
        print("❌ 미국 종목을 하나도 받지 못했습니다 "
              "(네트워크 차단 가능 — 러너/로컬에서 재실행).", file=sys.stderr)
        sys.exit(1)

    # 티커 기준 중복 제거 후 티커 알파벳 정렬
    dedup = {}
    for name, sym in rows:
        dedup.setdefault(sym, name)
    merged = sorted(((n, s) for s, n in dedup.items()), key=lambda x: x[1])

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps([[n, s] for n, s in merged], ensure_ascii=False, indent=0) + "\n",
        encoding="utf-8")
    print(f"✅ {OUT_FILE.relative_to(ROOT)} — {len(merged)}종목 저장")


if __name__ == "__main__":
    main()
