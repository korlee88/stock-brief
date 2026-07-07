#!/usr/bin/env python3
"""한국거래소(KRX) 상장 종목 코드 리스트를 내려받아 data/kr-stocks.json 으로 저장.

on-demand.html 의 '한국 종목 코드 찾기'가 이 파일을 fetch 해서 로컬 검색한다.
종목 리스트는 자주 바뀌지 않으므로 실시간 조회 대신 이 스크립트로 한 번 받아 커밋한다.

    python3 scripts/build_kr_stocks.py            # KRX에서 최신 목록 받아 data/kr-stocks.json 갱신

네트워크 제약 안내:
    KRX(kind.krx.co.kr)는 일부 샌드박스/프록시에서 차단될 수 있다. GitHub Actions 러너나
    로컬 PC 등 KRX 접근이 되는 환경에서 실행하면 전 종목(코스피+코스닥 ~2,800)이 채워진다.
    (update-kr-stocks.yml 워크플로로 러너에서 실행·커밋 가능.)

출력 형식(온디맨드 페이지와 호환): [["삼성전자","005930.KS"], ["SK하이닉스","000660.KS"], ...]
  · 코스피=.KS, 코스닥=.KQ (Yahoo Finance 티커 규칙)
  · 종목명 가나다 정렬, 코드 중복 제거
"""
import json
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "data" / "kr-stocks.json"

# KRX 상장법인목록 다운로드 (xls 형태이나 실제 본문은 EUC-KR HTML <table>)
MARKETS = {
    "stockMkt":  ".KS",   # 유가증권(코스피)
    "kosdaqMkt": ".KQ",   # 코스닥
}
BASE = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&marketType="


class _TableParser(HTMLParser):
    """KRX corpList HTML의 각 행에서 (회사명, 종목코드)만 뽑는다.

    열 순서: 회사명 · 종목코드 · 업종 · 주요제품 · 상장일 · 결산월 · 대표자명 · 홈페이지 · 지역
    → 각 <tr>의 첫 번째·두 번째 <td> 텍스트만 사용.
    """
    def __init__(self):
        super().__init__()
        self.rows = []
        self._in_td = False
        self._cells = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._cells = []
        elif tag == "td" and self._cells is not None:
            self._in_td = True
            self._buf = []

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


def parse_krx_html(html: str, suffix: str) -> list[tuple[str, str]]:
    """KRX corpList HTML 문자열 → [(회사명, '005930.KS'), ...]. 헤더행·빈행은 버린다."""
    p = _TableParser()
    p.feed(html)
    out = []
    for name, code in p.rows:
        code = code.strip()
        if not name or not code.isdigit():   # 헤더('종목코드' 등)·빈칸 제거
            continue
        out.append((name.strip(), f"{int(code):06d}{suffix}"))
    return out


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; kr-stocks-builder/1.0)"
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("euc-kr", errors="replace")


def main():
    all_rows: list[tuple[str, str]] = []
    for market, suffix in MARKETS.items():
        try:
            html = fetch(BASE + market)
            rows = parse_krx_html(html, suffix)
            print(f"  {market}: {len(rows)}종목")
            all_rows.extend(rows)
        except Exception as e:
            print(f"  ⚠ {market} 다운로드 실패: {e}", file=sys.stderr)

    if not all_rows:
        print("❌ KRX에서 종목을 하나도 받지 못했습니다 "
              "(이 환경이 KRX를 차단할 수 있음 — 러너/로컬에서 재실행).", file=sys.stderr)
        sys.exit(1)

    # 코드 기준 중복 제거 후 종목명 가나다 정렬
    dedup = {}
    for name, code in all_rows:
        dedup.setdefault(code, name)
    merged = sorted(((n, c) for c, n in dedup.items()), key=lambda x: x[0])

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps([[n, c] for n, c in merged], ensure_ascii=False, indent=0) + "\n",
        encoding="utf-8")
    print(f"✅ {OUT_FILE.relative_to(ROOT)} — {len(merged)}종목 저장")


if __name__ == "__main__":
    main()
