#!/usr/bin/env python3
"""한국거래소(KRX) 상장 종목 코드 리스트를 내려받아 data/kr-stocks.json 으로 저장.

on-demand.html 의 '한국 종목 코드 찾기'가 이 파일을 fetch 해서 로컬 검색한다.
종목 리스트는 자주 바뀌지 않으므로 실시간 조회 대신 이 스크립트로 한 번 받아 커밋한다.

    python3 scripts/build_kr_stocks.py            # 최신 목록 받아 data/kr-stocks.json 갱신

데이터 소스(우선순위):
  1) FinanceDataReader — `fdr.StockListing('KRX')`. KRX 전 종목(Code·Name·Market)을
     안정적으로 반환하는 표준 라이브러리. 워크플로에서 pip 설치(update-kr-stocks.yml).
  2) (폴백) KRX corpList HTML 직접 파싱 — FDR 미설치/실패 시.

네트워크 제약:
  KRX/네이버는 일부 샌드박스·프록시에서 차단될 수 있다. GitHub Actions 러너나
  KRX 접근이 되는 로컬에서 실행하면 전 종목(코스피+코스닥 ~2,800)이 채워진다.

출력 형식(온디맨드 페이지와 호환): [["삼성전자","005930.KS"], ["SK하이닉스","000660.KS"], ...]
  · 코스피=.KS, 코스닥=.KQ (Yahoo Finance 티커 규칙), 코넥스는 제외
  · 종목명 가나다 정렬, 코드 중복 제거
"""
import json
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "data" / "kr-stocks.json"

# 시장 → Yahoo 접미사 (코넥스는 Yahoo 미지원이 많아 제외)
MARKET_SUFFIX = {"KOSPI": ".KS", "KOSDAQ": ".KQ"}


def _norm_code(code, suffix):
    """6자리 종목코드로 정규화 후 접미사 부착. 이미 접미사가 있으면 그대로."""
    code = str(code).strip().upper()
    if code.endswith((".KS", ".KQ")):
        return code
    digits = "".join(ch for ch in code if ch.isdigit())
    if not digits:
        return None
    return f"{int(digits):06d}{suffix}"


# ── 1) FinanceDataReader (권장) ────────────────────────────────────────────────
def fetch_via_fdr():
    import FinanceDataReader as fdr   # 미설치 시 ImportError → 폴백
    df = fdr.StockListing("KRX")
    # 버전별 컬럼명 드리프트 방어: 코드=Code/Symbol, 이름=Name, 시장=Market
    cols = {c.lower(): c for c in df.columns}
    code_c = cols.get("code") or cols.get("symbol")
    name_c = cols.get("name")
    mkt_c  = cols.get("market")
    if not (code_c and name_c and mkt_c):
        raise RuntimeError(f"예상 컬럼 없음 (컬럼: {list(df.columns)})")
    out = []
    for _, row in df.iterrows():
        market = str(row[mkt_c]).strip().upper()
        # 'KOSPI GLOBAL' 등 변형 대비 부분 매칭
        suffix = next((s for m, s in MARKET_SUFFIX.items() if m in market), None)
        if not suffix:
            continue   # 코넥스 등 제외
        code = _norm_code(row[code_c], suffix)
        name = str(row[name_c]).strip()
        if code and name and name.lower() != "nan":
            out.append((name, code))
    return out


# ── 2) 폴백: KRX corpList HTML 직접 파싱 ───────────────────────────────────────
_KRX_BASE = "http://kind.krx.co.kr/corpgeneral/corpList.do?method=download&marketType="
_KRX_MARKETS = {"stockMkt": ".KS", "kosdaqMkt": ".KQ"}


class _TableParser(HTMLParser):
    """corpList HTML의 각 <tr>에서 첫·둘째 <td>(회사명·종목코드)만 추출."""
    def __init__(self):
        super().__init__()
        self.rows, self._cells, self._in_td, self._buf = [], None, False, []

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
            if len(self._cells) >= 2:
                self.rows.append((self._cells[0], self._cells[1]))
            self._cells = None


def parse_krx_html(html: str, suffix: str):
    p = _TableParser()
    p.feed(html)
    out = []
    for name, code in p.rows:
        if name and code.strip().isdigit():
            out.append((name.strip(), f"{int(code):06d}{suffix}"))
    return out


def fetch_via_krx():
    out = []
    for market, suffix in _KRX_MARKETS.items():
        req = urllib.request.Request(_KRX_BASE + market, headers={
            "User-Agent": "Mozilla/5.0", "Accept": "text/html,*/*",
            "Referer": "http://kind.krx.co.kr/corpgeneral/corpList.do",
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
        # 응답이 HTML 테이블이 아니면(=바이너리/빈 응답) 파싱 0건 → 스킵
        html = raw.decode("euc-kr", errors="replace")
        rows = parse_krx_html(html, suffix)
        print(f"  (폴백) {market}: {len(rows)}종목", file=sys.stderr)
        out.extend(rows)
    return out


def main():
    rows = []
    try:
        rows = fetch_via_fdr()
        print(f"✔ FinanceDataReader로 {len(rows)}종목 수신")
    except ImportError:
        print("ℹ FinanceDataReader 미설치 — KRX 직접 파싱 폴백", file=sys.stderr)
    except Exception as e:
        print(f"⚠ FinanceDataReader 실패({e}) — KRX 직접 파싱 폴백", file=sys.stderr)

    if not rows:
        try:
            rows = fetch_via_krx()
        except Exception as e:
            print(f"⚠ KRX 직접 파싱도 실패: {e}", file=sys.stderr)

    if not rows:
        print("❌ 어떤 소스에서도 종목을 받지 못했습니다 "
              "(네트워크 차단 가능 — 러너/로컬에서 재실행).", file=sys.stderr)
        sys.exit(1)

    # 코드 기준 중복 제거 후 종목명 가나다 정렬
    dedup = {}
    for name, code in rows:
        dedup.setdefault(code, name)
    merged = sorted(((n, c) for c, n in dedup.items()), key=lambda x: x[0])

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps([[n, c] for n, c in merged], ensure_ascii=False, indent=0) + "\n",
        encoding="utf-8")
    print(f"✅ {OUT_FILE.relative_to(ROOT)} — {len(merged)}종목 저장")


if __name__ == "__main__":
    main()
