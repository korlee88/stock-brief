#!/usr/bin/env python3
"""DART(전자공시) 고유번호 목록을 내려받아 data/dart-corp-codes.json 으로 저장.

enrich_signals.py의 fetch_dart_financials()가 이 파일로 6자리 종목코드→DART
8자리 고유번호(corp_code)를 조회한다. DART 재무제표 API는 종목코드가 아니라
고유번호를 요구하는데, 고유번호 전체 목록은 매번 조회하기엔 무겁고(전 상장·
비상장 법인 ~10만 건 ZIP) 자주 바뀌지 않으므로 이 스크립트로 한 번 받아 커밋한다
(data/kr-stocks.json과 동일한 패턴).

    DART_API_KEY=... python3 scripts/build_dart_corp_codes.py

DART_API_KEY는 https://opendart.fss.or.kr (무료 가입) 에서 발급 — 채팅이 아니라
GitHub 저장소 Settings → Secrets → Actions에 DART_API_KEY로 직접 등록할 것.

출력 형식: {"005930": "00126380", "000660": "00164779", ...} (종목코드 → 고유번호)
"""
import io
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "data" / "dart-corp-codes.json"
API_KEY = os.environ.get("DART_API_KEY", "")


def main():
    if not API_KEY:
        print("ℹ DART_API_KEY 없음 — 건너뜀 (재무제표 신호는 선택 기능, 파이프라인은 계속 동작)",
              file=sys.stderr)
        sys.exit(0)

    url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={API_KEY}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml_bytes = zf.read(zf.namelist()[0])
    except zipfile.BadZipFile:
        # 인증 실패 등은 ZIP이 아니라 에러 XML/JSON을 그대로 반환함
        print(f"❌ DART corpCode 응답이 ZIP이 아님 (키 오류 가능): {raw[:200]!r}", file=sys.stderr)
        sys.exit(1)

    root = ET.fromstring(xml_bytes)
    mapping = {}
    for item in root.findall("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        if stock_code and corp_code:   # 비상장 법인은 stock_code가 빈 문자열
            mapping[stock_code] = corp_code

    if not mapping:
        print("❌ 매핑 0건 — 저장하지 않음", file=sys.stderr)
        sys.exit(1)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(mapping, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    print(f"✅ {OUT_FILE.relative_to(ROOT)} — {len(mapping)}개 상장사 고유번호 저장")


if __name__ == "__main__":
    main()
