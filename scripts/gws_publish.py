"""
GWS 게시 스크립트 — YouTube 업로드 + Sheets 기록 + Gmail 다이제스트

매주 GitHub Actions에서 weekly_video_make.py 이후 호출됨.
시크릿이 없는 통합은 조용히 건너뜀 — 기존 파이프라인에 영향 없음.

필요 환경변수 (선택):
  GWS_YOUTUBE_TOKEN  — YouTube OAuth2 token.json 내용 (JSON 문자열)
  GWS_SA_CREDENTIALS — Google Service Account JSON (Sheets 쓰기용)
  GOOGLE_SHEET_ID    — 기록할 스프레드시트 ID
  GMAIL_USER         — 발신 Gmail 주소
  GMAIL_APP_PASSWORD — Gmail 앱 비밀번호 (2FA 활성화 후 생성)
  GMAIL_TO           — 수신자 이메일 주소

필요 패키지:
  pip install google-api-python-client google-auth google-auth-httplib2 gspread
"""

import base64
import json
import os
import smtplib
import sys
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT_DIR           = Path(__file__).parent.parent
# 온디맨드 멀티 종목 모드: env로 config·리포트 경로 오버라이드 (기본값 = 기존 RKLB 경로)
TICKER_CONFIG      = json.loads(Path(os.environ.get("TICKER_CONFIG")
                                     or (ROOT_DIR / "config" / "ticker.json")).read_text(encoding="utf-8"))
TICKER             = TICKER_CONFIG["ticker"]
COMPANY_KO         = TICKER_CONFIG["company_ko"]
BRAND_LABEL        = TICKER_CONFIG.get("brand_label", f"{TICKER} BRIEF")
REPO               = TICKER_CONFIG.get("repo", os.environ.get("GITHUB_REPOSITORY", ""))
VIDEO_TAGS         = TICKER_CONFIG.get("video_tags", [TICKER, "주식", "Shorts"])

# ── 통화 표기: 한국거래소(.KS/.KQ) 종목은 원화·정수, 그 외는 $·2소수점 (prep.py와 동일 규칙) ──
IS_KRW = TICKER.upper().endswith((".KS", ".KQ")) or "korea" in TICKER_CONFIG.get("exchange", "").lower()
MARKET_KW = "한국주식" if IS_KRW else "미국주식"

def fmt_price(value, empty=""):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return empty
    return f"{v:,.0f}원" if IS_KRW else f"${v:,.2f}"

REPORT_BASE        = Path(os.environ.get("REPORT_BASE") or (ROOT_DIR / "data" / "weekly-report"))

GWS_YOUTUBE_TOKEN  = os.environ.get("GWS_YOUTUBE_TOKEN", "")
GWS_SA_CREDENTIALS = os.environ.get("GWS_SA_CREDENTIALS", "")
GOOGLE_SHEET_ID    = os.environ.get("GOOGLE_SHEET_ID", "")
GMAIL_USER         = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_TO           = os.environ.get("GMAIL_TO", "")


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def find_latest_report() -> Path | None:
    """data/weekly-report/ 에서 meta.json이 있는 가장 최근 디렉토리 반환."""
    if not REPORT_BASE.exists():
        return None
    dirs = sorted(
        (d for d in REPORT_BASE.iterdir() if d.is_dir() and (d / "meta.json").exists()),
        reverse=True,
    )
    return dirs[0] if dirs else None


def load_meta(report_dir: Path) -> dict:
    return json.loads((report_dir / "meta.json").read_text(encoding="utf-8"))


def find_video(report_dir: Path) -> Path | None:
    """영상 파일 탐색 — {종목명}_YYYYMMDD.mp4(한글, v1.2) / {TICKER}_*.mp4(구) 모두 매칭,
    구 video.mp4 폴백. 이름 규칙이 바뀌어도 리포트 디렉토리의 최신 mp4를 집는다."""
    cands = sorted(p for p in report_dir.glob("*.mp4") if p.name != "video.mp4")
    if cands:
        return cands[-1]
    legacy = report_dir / "video.mp4"
    return legacy if legacy.exists() else None


def get_signal_label(avg_buy_index: int | None) -> str:
    if avg_buy_index is None:
        return "정보없음"
    if avg_buy_index >= 65:
        return "긍정"
    if avg_buy_index >= 45:
        return "중립"
    return "신중"


def build_youtube_copy(meta: dict, report_dir: Path) -> tuple[str, str]:
    """YouTube 업로드용 제목·설명(연관검색어 포함) — 자동 업로드와 메일 안내에 공통 사용.

    config(ticker.json) 값만으로 조립해 종목 교체 시 손댈 곳이 없다.
    제목 훅은 씬0 헤드라인(대본 줄1), 없으면 기존 제목 템플릿 폴백.
    """
    date   = meta.get("generated_at", "")
    bi     = meta.get("avg_buy_index")   # 온디맨드(뉴스 전용) 종목은 None
    price  = meta.get("latest_price")
    signal = get_signal_label(bi)
    price_str = fmt_price(price)

    # 제목: 씬0 헤드라인(대본 줄1) 훅
    headline = ""
    sj = report_dir / "script.json"
    if sj.exists():
        try:
            scenes = json.loads(sj.read_text(encoding="utf-8")).get("scenes") or []
            if scenes and scenes[0].get("lines"):
                headline = scenes[0]["lines"][0].replace("*", "").strip().strip('"')
        except Exception:
            pass
    try:
        m, d = date.split("-")[1:3]
        md = f"{int(m)}/{int(d)}"
    except (ValueError, IndexError):
        md = date
    if headline:
        title = f"{headline} | {COMPANY_KO}({TICKER}) 주가 브리핑 {md} #Shorts"
    elif bi is not None:
        title = TICKER_CONFIG.get(
            "video_title_template", "{ticker} 주가 분석 {date} | 참고지수 {bi}점 {signal}"
        ).format(ticker=TICKER, date=date, bi=bi, signal=signal) + " #Shorts"
    else:
        title = f"{COMPANY_KO}({TICKER}) 주가 브리핑 {md} #Shorts"
    title = title[:100]

    # 연관검색어·해시태그: config 기반 조립 (중복 제거·순서 보존)
    kw = []
    for k in ([COMPANY_KO, TICKER, f"{COMPANY_KO} 주가", f"{TICKER} 주가", MARKET_KW]
              + TICKER_CONFIG.get("google_trends_keywords", [])
              + [t for t in VIDEO_TAGS if t.lower() != "shorts"]
              + [TICKER_CONFIG.get("industry_ko", ""), TICKER_CONFIG.get("competitor_ticker", "")]):
        k = (k or "").strip()
        if k and k not in kw:
            kw.append(k)
    hashtags = " ".join(dict.fromkeys(
        "#" + t.replace(" ", "").replace("·", "")
        for t in [TICKER, COMPANY_KO, MARKET_KW] + [v for v in VIDEO_TAGS if v.lower() != "shorts"]
    )) + " #Shorts"

    bi_s = f"참고지수 {bi}점({signal})" if bi is not None else "뉴스 브리핑"
    stat = f"현재가 {price_str} · {bi_s}" if price_str else bi_s
    description = (
        f"{COMPANY_KO}({TICKER}) {date} 주가 브리핑 — {stat}\n"
        f"핵심 뉴스(호재·악재)와 다음주 전망을 1분으로 정리했습니다.\n\n"
        f"※ 본 영상은 AI 분석 툴로 수집한 뉴스 자료를 요약·정리한 정보 콘텐츠이며, 투자 권유가 아닙니다.\n\n"
        f"연관검색어: {', '.join(kw)}\n"
        f"{hashtags}"
    )
    return title, description[:5000]


def get_youtube_credentials():
    """GWS_YOUTUBE_TOKEN JSON → google.oauth2.credentials.Credentials (자동 갱신)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_data = json.loads(GWS_YOUTUBE_TOKEN)
    creds = Credentials.from_authorized_user_info(token_data)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def get_sheets_credentials():
    """GWS_SA_CREDENTIALS JSON → google.oauth2.service_account.Credentials."""
    from google.oauth2.service_account import Credentials

    sa_data = json.loads(GWS_SA_CREDENTIALS)
    return Credentials.from_service_account_info(
        sa_data,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )


# ── YouTube 업로드 ─────────────────────────────────────────────────────────────

def upload_to_youtube(report_dir: Path, meta: dict) -> str | None:
    """video.mp4 를 YouTube Shorts (비공개) 로 업로드. 성공 시 youtu.be URL 반환."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    video_path = find_video(report_dir)
    if not video_path:
        print("   [SKIP] 영상 파일 없음 — YouTube 업로드 건너뜀", file=sys.stderr)
        return None

    creds   = get_youtube_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    # 제목·설명(연관검색어 포함) — 메일 안내와 동일 문구 (build_youtube_copy)
    title, description = build_youtube_copy(meta, report_dir)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": VIDEO_TAGS,
            "categoryId": "25",
            "defaultLanguage": "ko",
        },
        "status": {
            "privacyStatus": "unlisted", # 업로드 후 직접 공개 전환 가능
            "selfDeclaredMadeForKids": False,
            "madeForKids": False,
        },
    }

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True),
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            print(f"   YouTube 업로드 진행: {pct}%")

    video_id = response.get("id", "")
    return f"https://youtu.be/{video_id}" if video_id else None


# ── Google Sheets 기록 ────────────────────────────────────────────────────────

SHEET_HEADER = ["날짜", "매수지수", f"주가({'원' if IS_KRW else '$'})", "매수신호", "세션수", "YouTube링크"]


def log_to_sheets(meta: dict, youtube_url: str | None) -> bool:
    """Sheets에 주간 분석 결과 1행 추가. 첫 실행 시 헤더 자동 생성."""
    import gspread
    from google.auth.transport.requests import Request

    creds = get_sheets_credentials()

    gc   = gspread.authorize(creds)
    sh   = gc.open_by_key(GOOGLE_SHEET_ID)
    ws   = sh.sheet1

    # 헤더 없으면 첫 행에 자동 추가
    try:
        first_cell = ws.cell(1, 1).value
    except Exception:
        first_cell = None
    if not first_cell:
        ws.append_row(SHEET_HEADER, value_input_option="USER_ENTERED")

    bi     = meta.get("avg_buy_index")
    price  = meta.get("latest_price")
    signal = get_signal_label(bi)

    row = [
        meta.get("generated_at", ""),
        bi if bi is not None else "",
        float(price) if price else "",
        signal,
        meta.get("session_count", ""),
        youtube_url or "",
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return True


# ── Gmail 다이제스트 ───────────────────────────────────────────────────────────

def _build_html(meta: dict, youtube_url: str | None, scene_count: int,
                yt_title: str = "", yt_desc: str = "", video_note: str = "") -> str:
    bi     = meta.get("avg_buy_index")   # 온디맨드(뉴스 전용) 종목은 None → "—" 표기
    price  = meta.get("latest_price")
    date   = meta.get("generated_at", "")
    signal = get_signal_label(bi)
    bi_disp = f"{bi}점" if bi is not None else "—"

    price_str = fmt_price(price, empty="N/A")

    signal_color = {"매수": "#22c55e", "관망": "#f59e0b", "매도": "#ef4444"}.get(signal, "#6b7280")

    yt_section = ""
    if youtube_url:
        yt_section = f"""
        <p style="text-align:center;margin:24px 0;">
          <a href="{youtube_url}"
             style="background:#ef4444;color:#fff;padding:12px 32px;border-radius:8px;
                    text-decoration:none;font-weight:bold;font-size:16px;">
            ▶ YouTube에서 시청
          </a>
        </p>"""

    video_note_section = ""
    if video_note:
        import html as _h
        video_note_section = f"""
    <p style="color:#9ca3af;font-size:13px;background:#141720;border-radius:8px;
              padding:10px 14px;margin:16px 0;">📎 {_h.escape(video_note)}</p>"""

    # YouTube 수동 업로드용 제목·설명 복사 블록 (연관검색어 포함)
    yt_copy_section = ""
    if yt_title or yt_desc:
        import html as _html
        yt_copy_section = f"""
    <div style="background:#141720;border-radius:12px;padding:18px;margin:20px 0;">
      <h2 style="color:#87dcff;font-size:16px;margin:0 0 12px;">YouTube 업로드용 제목·설명</h2>
      <p style="color:#9ca3af;font-size:12px;margin:0 0 4px;">제목</p>
      <pre style="white-space:pre-wrap;word-break:break-all;background:#1c1f26;border-radius:8px;
                  padding:10px;margin:0 0 12px;color:#f9fafb;font-size:13px;font-family:inherit;">{_html.escape(yt_title)}</pre>
      <p style="color:#9ca3af;font-size:12px;margin:0 0 4px;">설명 (연관검색어 포함)</p>
      <pre style="white-space:pre-wrap;word-break:break-all;background:#1c1f26;border-radius:8px;
                  padding:10px;margin:0;color:#d1d5db;font-size:13px;font-family:inherit;">{_html.escape(yt_desc)}</pre>
    </div>"""

    scene_imgs = ""
    scene_labels = ["주간 브리핑", "핵심 뉴스 3선", "다음주 전망"]
    for i in range(0, scene_count):
        label = scene_labels[i] if i < len(scene_labels) else f"씬 {i}"
        scene_imgs += f"""
        <div style="margin:20px 0;">
          <p style="color:#9ca3af;font-size:13px;margin:0 0 6px;">씬 {i} — {label}</p>
          <img src="cid:scene_{i:02d}" style="width:100%;max-width:480px;border-radius:12px;display:block;">
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<body style="margin:0;padding:0;background:#0e1117;font-family:'Apple SD Gothic Neo',sans-serif;color:#f9fafb;">
  <div style="max-width:600px;margin:0 auto;padding:24px;">
    <div style="background:#0f2046;border-radius:16px;padding:24px;margin-bottom:20px;">
      <p style="color:#87dcff;font-size:13px;margin:0 0 8px;letter-spacing:2px;">{BRAND_LABEL}</p>
      <h1 style="color:#fff;font-size:24px;margin:0 0 4px;">{date} 주가 분석</h1>
    </div>

    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
      <tr style="background:#1c1f26;">
        <td style="padding:16px;border-radius:10px 0 0 10px;">
          <p style="color:#9ca3af;font-size:12px;margin:0 0 4px;">참고지수</p>
          <p style="color:{signal_color};font-size:28px;font-weight:bold;margin:0;">{bi_disp}</p>
        </td>
        <td style="padding:16px;background:#141720;">
          <p style="color:#9ca3af;font-size:12px;margin:0 0 4px;">현재가</p>
          <p style="color:#ffd700;font-size:28px;font-weight:bold;margin:0;">{price_str}</p>
        </td>
        <td style="padding:16px;background:#1c1f26;border-radius:0 10px 10px 0;">
          <p style="color:#9ca3af;font-size:12px;margin:0 0 4px;">시그널</p>
          <p style="color:{signal_color};font-size:28px;font-weight:bold;margin:0;">{signal}</p>
        </td>
      </tr>
    </table>

    {yt_section}
    {video_note_section}
    {yt_copy_section}

    <div style="margin-top:28px;">
      <h2 style="color:#87dcff;font-size:16px;margin:0 0 16px;">씬 이미지</h2>
      {scene_imgs}
    </div>

    <p style="color:#374151;font-size:12px;text-align:center;margin-top:32px;">
      자동 생성 — {TICKER} Dashboard · {REPO}
    </p>
  </div>
</body>
</html>"""


def send_gmail_digest(report_dir: Path, meta: dict, youtube_url: str | None) -> bool:
    """씬 4장 인라인 첨부 HTML 이메일을 Gmail SMTP로 발송."""
    date   = meta.get("generated_at", "이번 주")
    bi     = meta.get("avg_buy_index")   # 온디맨드(뉴스 전용) 종목은 None
    signal = get_signal_label(bi)

    if bi is not None:
        subject = TICKER_CONFIG.get(
            "email_subject_template", "{ticker} 분석 브리핑 {date} | 참고지수 {bi}점 ({signal})"
        ).format(ticker=TICKER, date=date, bi=bi, signal=signal)
    else:
        subject = f"{TICKER} 뉴스 브리핑 {date}"

    # 씬 이미지 수집 — 신규 {YYMMDD}_{회사명}_씬N.png 우선, 구 scene_NN.png 폴백 (과거 리포트 호환)
    scene_paths = []
    for i in range(0, 6):
        cands = sorted(report_dir.glob(f"*_씬{i}.png"))
        p = cands[-1] if cands else report_dir / f"scene_{i:02d}.png"
        if p.exists():
            scene_paths.append((i, p))

    # 영상 파일 첨부 (Gmail 25MB 제한 — base64 팽창 감안 raw 18MB까지만, 초과 시 안내만)
    video_path = find_video(report_dir)
    video_note = ""
    attach_video = False
    if video_path:
        size_mb = video_path.stat().st_size / 1_000_000
        if size_mb <= 18:
            attach_video = True
            video_note = f"영상 파일({video_path.name}, {size_mb:.1f}MB)을 첨부했습니다."
        else:
            run_url = ""
            if os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_RUN_ID"):
                run_url = (f"https://github.com/{os.environ['GITHUB_REPOSITORY']}"
                           f"/actions/runs/{os.environ['GITHUB_RUN_ID']}")
            video_note = (f"영상({video_path.name}, {size_mb:.1f}MB)이 메일 첨부 한도(18MB)를 넘어 "
                          f"GitHub Actions 아티팩트에서 받아야 합니다."
                          + (f" → {run_url}" if run_url else ""))

    yt_title, yt_desc = build_youtube_copy(meta, report_dir)
    html_body = _build_html(meta, youtube_url, len(scene_paths), yt_title, yt_desc, video_note)

    # 메시지 조립
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = GMAIL_TO

    alt = MIMEMultipart("alternative")
    plain = (f"{TICKER} 주가 분석 {date}\n참고지수: {f'{bi}점 ({signal})' if bi is not None else '—'}\n{youtube_url or ''}\n"
             f"{video_note}\n\n"
             f"[YouTube 업로드용 제목]\n{yt_title}\n\n[YouTube 업로드용 설명]\n{yt_desc}")
    alt.attach(MIMEText(plain, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    # 씬 이미지 인라인 첨부
    for i, path in scene_paths:
        with open(path, "rb") as f:
            img = MIMEImage(f.read(), _subtype="png")
        img.add_header("Content-ID", f"<scene_{i:02d}>")
        img.add_header("Content-Disposition", "inline", filename=path.name)
        msg.attach(img)

    # 영상 파일 첨부 (18MB 이하일 때만)
    if attach_video:
        from email.mime.application import MIMEApplication
        with open(video_path, "rb") as f:
            part = MIMEApplication(f.read(), _subtype="mp4")
        part.add_header("Content-Disposition", "attachment", filename=video_path.name)
        msg.attach(part)

    # SMTP 발송
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)

    return True


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    report_dir = find_latest_report()
    if not report_dir:
        print("⚠ weekly-report 디렉토리 없음", file=sys.stderr)
        sys.exit(0)

    meta = load_meta(report_dir)

    print(f"\n📤 GWS 게시 시작 — {meta.get('generated_at', '?')}")

    youtube_url = None

    # ── 1. YouTube 업로드 ────────────────────────────────────────────────────
    if GWS_YOUTUBE_TOKEN:
        try:
            print("  🎬 YouTube 업로드 중...")
            youtube_url = upload_to_youtube(report_dir, meta)
            if youtube_url:
                print(f"  ✅ YouTube: {youtube_url}")
            else:
                print("  ⚠ YouTube 업로드 완료됐으나 ID 없음", file=sys.stderr)
        except Exception as e:
            print(f"  ⚠ YouTube 실패: {e}", file=sys.stderr)
    else:
        print("  [SKIP] YouTube: GWS_YOUTUBE_TOKEN 없음")

    # ── 2. Google Sheets 기록 ────────────────────────────────────────────────
    if GWS_SA_CREDENTIALS and GOOGLE_SHEET_ID:
        try:
            print("  📊 Sheets 기록 중...")
            log_to_sheets(meta, youtube_url)
            print("  ✅ Sheets 기록 완료")
        except Exception as e:
            print(f"  ⚠ Sheets 실패: {e}", file=sys.stderr)
    else:
        missing = []
        if not GWS_SA_CREDENTIALS:
            missing.append("GWS_SA_CREDENTIALS")
        if not GOOGLE_SHEET_ID:
            missing.append("GOOGLE_SHEET_ID")
        print(f"  [SKIP] Sheets: {', '.join(missing)} 없음")

    # ── 3. Gmail 다이제스트 ──────────────────────────────────────────────────
    if GMAIL_USER and GMAIL_APP_PASSWORD and GMAIL_TO:
        try:
            print("  📧 Gmail 발송 중...")
            send_gmail_digest(report_dir, meta, youtube_url)
            print(f"  ✅ Gmail 발송 완료")
        except Exception as e:
            print(f"  ⚠ Gmail 실패: {e}", file=sys.stderr)
    else:
        missing = [s for s, v in [
            ("GMAIL_USER", GMAIL_USER),
            ("GMAIL_APP_PASSWORD", GMAIL_APP_PASSWORD),
            ("GMAIL_TO", GMAIL_TO),
        ] if not v]
        print(f"  [SKIP] Gmail: {', '.join(missing)} 없음")

    print("\n✅ GWS 게시 완료\n")


if __name__ == "__main__":
    main()
