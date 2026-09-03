"""
주간 나레이션 영상 생성 (moviepy 2.x + 애니메이션)
weekly_video_prep.py 실행 후 사용.
script.json + scene_XX.png → edge-tts MP3 → 애니메이션 MP4
출력: 1080×1920 (YouTube Shorts 세로 포맷)

종목 설정: config/ticker.json
필요 패키지: pip install -r requirements.txt
"""

import json, sys, asyncio, math, os
from pathlib import Path

ROOT_DIR      = Path(__file__).parent.parent
# 온디맨드 멀티 종목 모드: env로 config·리포트 경로 오버라이드 (기본값 = 기존 RKLB 경로)
TICKER_CONFIG = json.loads(Path(os.environ.get("TICKER_CONFIG")
                                or (ROOT_DIR / "config" / "ticker.json")).read_text(encoding="utf-8"))
TICKER        = TICKER_CONFIG["ticker"]
COMPANY_KO    = TICKER_CONFIG.get("company_ko", "") or TICKER

def safe_filename(name: str) -> str:
    """파일명용 정리 — 한글 유지, 윈도우 금지 문자(\\/:*?"<>|)·공백 제거. 비면 티커 폴백."""
    import re as _re
    s = _re.sub(r'[\\/:*?"<>|\s]+', '', str(name)).strip('.')
    return s or _re.sub(r'[\\/:*?"<>|\s]+', '', TICKER) or "video"

REPORT_BASE   = Path(os.environ.get("REPORT_BASE") or (ROOT_DIR / "data" / "weekly-report"))
# 영상 포맷 모드 — weekly_video_prep.py와 동일한 env(MODE)를 읽어 화면비율을 맞춘다.
MODE          = os.environ.get("MODE", "short")
VOICE         = "ko-KR-SunHiNeural"    # 밝은 여성 — 친근 튜닝 (edge-tts 지원 검증 음성)
RATE          = "+13%"                  # 대화하듯 자연스러운 속도
PITCH         = "+6Hz"                  # 살짝 올려 밝고 친근한 톤
LINE_PAUSE_MS = 600                     # 대본 줄(세그먼트) 사이 무음 휴지 (ms)
TRIM_DB       = -42.0                   # 세그먼트 가장자리 무음 판정 임계 (dBFS)
TRIM_KEEP_MS  = 60                      # 트리밍 후 가장자리에 남길 무음 (ms)
SCENE_LEAD_MS = 500                     # 씬 시작~첫 나레이션 사이 여유 무음 (씬 전환 딜레이)
SCENE_TAIL_MS = 300                     # 씬 끝 여유 무음 (ms)
FPS           = 24
W, H          = (1920, 1080) if MODE == "long" else (1080, 1920)   # long=가로 16:9, short=세로 9:16
PHOTO_Y       = 500                     # 헤더 아래 사진 시작 Y (prep.py의 HEADER_H와 동일)
PHOTO_H       = 500                     # 사진 영역 높이 (prep.py의 PHOTO_H와 동일)
MIN_SCENE_SEC = 5.0

ACCENT_COLORS = [
    (167, 139, 250),  # scene 0 purple  - 주간 브리핑
    (34,  197,  94),  # scene 1 green   - 호재 심층
    (14,  165, 233),  # scene 2 cyan    - 정량 지표
    (236,  72, 153),  # scene 3 magenta - 미래 비전 (클로징)
]

# 색상 테마 로테이션 (prep.py와 동일 — 생성일 시드로 동기화). 씬1(호재)은 항상 초록.
ACCENT_THEMES = [
    [(167, 139, 250), (34, 197, 94), (14, 165, 233), (236, 72, 153)],  # A 보라·초록·시안·마젠타 (기존)
    [(56, 189, 248),  (34, 197, 94), (99, 102, 241), (251, 146, 60)],  # B 시안·초록·인디고·오렌지
    [(129, 140, 248), (34, 197, 94), (45, 212, 191), (250, 204, 21)],  # C 인디고·초록·틸·골드
]

def _theme_idx(date_str):
    """생성일 문자열로 결정적 테마 인덱스 (prep.py와 동일 함수 → 색상 동기화)."""
    return sum(ord(c) for c in (date_str or "")) % len(ACCENT_THEMES)

# ── BGM 설정 (원본 합성 · CC0/로열티프리) ────────────────────────────────────
# 배경음악은 저장소에 커밋된 data/bgm/*.mp3 를 사용한다 → 빌드 시 네트워크 의존 0.
# 이 파일들은 scripts/make_bgm.py 가 생성한 '원본' 앰비언트 패드라 저작권·출처 표기 의무가 없다.
# (외부 CC0 사이트는 빌드 환경에서 불안정: FreePD는 JS 렌더링이라 스크래핑 불가,
#  archive.org는 CC0 검색이 비고, yt-dlp+YouTube는 러너 IP 봇 차단 → 직접 합성·커밋으로 확정.)
# 여러 트랙 중 하나를 매번 다르게 골라 씀(사용자 요청 — 항상 같은 곡이라 단조롭다는 피드백).
BGM_VOLUME = 0.10                            # 나레이션 아래 배경음 (10%)
BGM_DIR    = ROOT_DIR / "data" / "bgm"

# ── 유틸 ──────────────────────────────────────────────────────────────────────

def find_latest_report():
    if not REPORT_BASE.exists():
        return None
    dirs = sorted(
        [d for d in REPORT_BASE.iterdir()
         if d.is_dir() and (d / "script.json").exists()],
        reverse=True,
    )
    return dirs[0] if dirs else None


def download_bgm(seed: str = "") -> "Path | None":
    """data/bgm/ 안의 여러 트랙 중 하나를 골라 반환. 없으면 None → 음악 없이 진행.

    음원은 scripts/make_bgm.py 로 미리 생성·커밋한다(원본·로열티프리). 빌드 중 네트워크 0.
    seed(티커+날짜)로 결정적 선택 — 같은 종목·같은 날 재실행하면 동일 트랙, 다른
    종목/날짜는 자동으로 달라진다(prep.py의 _theme_idx 색상 로테이션과 동일한 패턴).
    트랙을 추가·교체하려면 data/bgm/ 에 mp3만 더 넣으면(또는 바꾸면) 자동으로 로테이션된다.
    """
    files = sorted(BGM_DIR.glob("*.mp3")) if BGM_DIR.exists() else []
    if not files:
        print("   ⚠ data/bgm/ 에 트랙 없음 — 음악 없이 진행", file=sys.stderr)
        return None
    idx = sum(ord(c) for c in (seed or "")) % len(files)
    chosen = files[idx]
    print(f"   🎵 BGM 사용: {chosen.name} ({idx + 1}/{len(files)})")
    return chosen


def clean_for_tts(lines):
    table = {
        '【': '', '】': '', '①': '첫째,', '②': '둘째,', '③': '셋째,',
        '④': '넷째,', '⑤': '다섯째,', '$': '달러 ', '%': '퍼센트',
        '比': ' 대비',
        '+': '플러스 ', '─': '', '▲': '', '▼': '', '*': '',
        '🟢': '', '🔴': '', '📊': '', '📈': '', '✓': '', '⚡': '',
    }
    result = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if '|' in line:
            line = line.split('|')[0].strip()
        for k, v in table.items():
            line = line.replace(k, v)
        result.append(line)
    return ' '.join(result)


def _clean_line(line: str) -> str:
    """단일 줄 TTS 정제 — 카테고리 태그·특수기호 제거."""
    table = {
        '【': '', '】': '', '①': '첫째,', '②': '둘째,', '③': '셋째,',
        '④': '넷째,', '⑤': '다섯째,', '$': '달러 ', '%': '퍼센트',
        '比': ' 대비',   # '전일比' 등 한자 — TTS가 정확히 읽게 한글로
        '+': '플러스 ', '─': '', '▲': '상승 ', '▼': '하락 ',
        '▶': '', '↳': '', '↑': '상승 ', '*': '', '🟢': '', '🔴': '', '📊': '', '📈': '',
        '✓': '', '⚡': '', '"': '', '"': '', '"': '',
    }
    line = line.strip()
    if not line:
        return ""
    # 이미지 프롬프트/섹션 마커가 대본에 섞여 들어온 경우 방어적으로 제거 (영어 프롬프트 낭독 방지)
    if (line.startswith("IMAGE_PROMPT_") or line.startswith("===")
            or "no text" in line.lower() or "ultra-high resolution" in line.lower()):
        return ""
    if '|' in line:
        line = line.split('|')[0].strip()
    # [분위기], [거래량] 같은 카테고리 태그 제거
    if line.startswith('[') and ']' in line:
        line = line[line.index(']') + 1:].strip()
    for k, v in table.items():
        line = line.replace(k, v)
    return line.strip()


def build_scene_tts_segments(idx: int, lines: list) -> list:
    """씬별 대본을 '줄 단위 세그먼트' 리스트로 구성.

    옆에서 다정하게 이야기해 주는 톤 — 각 세그먼트는 개별 TTS로 합성되고
    세그먼트 사이에 ~1초 무음이 삽입되어 줄과 줄 사이가 자연스럽게 끊긴다.
    """
    cleaned = [c for c in (_clean_line(l) for l in lines) if c]
    if not cleaned:
        return []

    if idx == 0:
        # 회사 소개 & 간략한 주가 흐름 — 줄1(간략한 주가 흐름) + 줄2~(주력사업·방향·투자·제품·시장 지위)
        head = cleaned[0] if cleaned else ""
        rest = cleaned[1:]
        segments = []
        if head: segments.append(head)
        for i, ln in enumerate(rest):
            # 첫 소개 줄에만 다정한 브리지, 나머지는 자연스럽게 이어 읽기
            segments.append(("어떤 회사인지 먼저 볼게요. " + ln) if i == 0 else ln)

    elif idx == 1:
        # 핵심 뉴스 3선 — 호재/악재/보합 각 줄("호재:/악재:/보합:" 접두어)에 다정한 브리지를 붙여 narration
        import re as _re
        bridges = {
            "호재": "먼저 좋은 소식이에요. ",
            "악재": "반대로 짚어볼 점은요. ",
            "보합": "끝으로 중립적인 소식이에요. ",
            "중립": "끝으로 중립적인 소식이에요. ",
            "전망": "끝으로 지켜볼 흐름이에요. ",   # AI가 보합 대신 쓰는 접두어 드리프트 대응
        }
        segments = []
        for ln in cleaned:
            m = _re.match(r'^\s*(호재|악재|보합|중립|전망)\s*[:：]\s*(.*)$', ln)
            if m:
                key, body = m.group(1), m.group(2).strip()
                segments.append((bridges.get(key, "") + body).strip() if body else "")
            elif ln.strip():
                segments.append(ln)   # 접두어 없는 예외 줄도 살림

    elif idx == 2:
        # 정량 지표 — "추세:/밸류:/수급:/재무:/총평:" 접두어 줄에 다정한 브리지를 붙여 narration
        import re as _re
        bridges = {
            "추세": "숫자로도 한번 볼게요. 최근 추세는요, ",
            "밸류": "밸류에이션 짚어보면요, ",
            "수급": "수급 쪽도 볼까요? ",
            "재무": "최근 실적도 확인해봤어요. ",
            "총평": "정리하면요, ",
        }
        segments = []
        for ln in cleaned:
            m = _re.match(r'^\s*(추세|밸류|수급|재무|총평)\s*[:：]\s*(.*)$', ln)
            if m:
                key, body = m.group(1), m.group(2).strip()
                segments.append((bridges.get(key, "") + body).strip() if body else "")
            elif ln.strip():
                segments.append(ln)   # 접두어 없는 예외 줄도 살림

    elif idx == 3:
        # 클로징 — short: 다음주 전망(일정·시나리오·가격예측·흐름·변수·마무리)
        #        long: 메이저 투자사 전망(목표주가·근거·관전포인트·마무리) — "다음 주" 프레이밍은 안 맞음
        head = cleaned[0] if cleaned else ""
        rest = cleaned[1:]
        segments = []
        if head:
            bridge = "그럼 전문가들은 어떻게 보고 있을까요? " if MODE == "long" else "자, 다음 주는 어떨까요? "
            segments.append(bridge + head)
        segments.extend(rest)

    else:
        segments = cleaned

    return [s for s in segments if s and s.strip()]

# ── TTS ───────────────────────────────────────────────────────────────────────

async def _tts(text, path):
    """단일 텍스트 → edge-tts MP3."""
    import edge_tts
    comm = edge_tts.Communicate(text, VOICE, rate=RATE, pitch=PITCH)
    await comm.save(str(path))


def _trim_edge_silence(piece):
    """edge-tts가 세그먼트 앞뒤에 자체로 붙이는 무음(특히 꼬리 ~0.5초+)을 잘라낸다.

    안 자르면 삽입 무음(LINE_PAUSE_MS)과 겹쳐 줄 사이 간격이 의도보다 훨씬 길어진다.
    """
    try:
        from pydub.silence import detect_leading_silence
        lead = detect_leading_silence(piece, silence_threshold=TRIM_DB)
        tail = detect_leading_silence(piece.reverse(), silence_threshold=TRIM_DB)
        start = max(0, lead - TRIM_KEEP_MS)
        end   = len(piece) - max(0, tail - TRIM_KEEP_MS)
        if end - start >= 100:  # 전체가 무음 판정되는 등 과도 트리밍 방지
            return piece[start:end]
    except Exception:
        pass
    return piece


async def gen_audio(segments, path):
    """세그먼트(줄)별로 TTS한 뒤 LINE_PAUSE_MS 무음을 끼워 하나의 mp3로 합성.

    세그먼트 가장자리 무음을 트리밍하므로 줄 사이 간격은
    TRIM_KEEP_MS + LINE_PAUSE_MS + TRIM_KEEP_MS 로 일정하게 유지된다.
    씬 맨 앞에는 SCENE_LEAD_MS 무음을 둬, 씬 전환(크로스페이드) 직후
    나레이션이 곧바로 시작되지 않고 ~0.5초 쉬어 가게 한다.
    pydub/ffmpeg 미가용 등 실패 시 공백으로 이어붙인 단일 TTS로 폴백.

    반환값: [(start_sec, end_sec, text), ...] — 세그먼트별 실제 발화 구간(씬 시작 기준
    초 단위). 롱폼 모드에서 자막을 나레이션 타이밍에 맞춰 한 줄씩 동적으로 표시하는 데
    쓰인다(make_anime_frame). 합성 실패로 단일 TTS 폴백된 경우엔 타이밍을 알 수 없어
    빈 리스트를 반환 — 이 경우 자막은 화면에 표시되지 않는다(에러 아님, 안전한 성능 저하).
    """
    segments = [s for s in segments if s and s.strip()]
    if not segments:
        return []
    try:
        from pydub import AudioSegment
        line_gap = AudioSegment.silent(duration=LINE_PAUSE_MS)
        combined = None
        timeline = []
        cursor_ms = SCENE_LEAD_MS
        for i, seg in enumerate(segments):
            tmp = path.with_name(f"{path.stem}__seg{i}.mp3")
            await _tts(seg, tmp)
            piece = _trim_edge_silence(AudioSegment.from_file(tmp))
            dur_ms = len(piece)
            timeline.append((cursor_ms / 1000, (cursor_ms + dur_ms) / 1000, seg))
            cursor_ms += dur_ms + LINE_PAUSE_MS
            combined = piece if combined is None else (combined + line_gap + piece)
            try: tmp.unlink()
            except Exception: pass
        # 씬 시작 SCENE_LEAD_MS·끝 SCENE_TAIL_MS 여유 무음 — 씬 전환 직후 나레이션이 바로 붙지 않게
        combined = (AudioSegment.silent(duration=SCENE_LEAD_MS)
                    + combined + AudioSegment.silent(duration=SCENE_TAIL_MS))
        combined.export(str(path), format="mp3")
        return timeline
    except Exception as e:
        print(f"   ⚠ 세그먼트 합성 실패({e}) → 단일 TTS 폴백", file=sys.stderr)
        await _tts(" ".join(segments), path)
        return []


def _caption_windows(timeline, total_dur):
    """타임라인을 자막 표시 구간으로 변환 — 각 줄이 다음 줄 시작까지(줄 사이 무음 포함)
    화면에 남아 있게 해 무음 구간마다 자막이 깜빡 사라지는 걸 방지한다."""
    out = []
    for i, (start, _end, text) in enumerate(timeline):
        nxt = timeline[i + 1][0] if i + 1 < len(timeline) else total_dur
        out.append((start, nxt, text))
    return out

# ── 마스코트 ─────────────────────────────────────────────────────────────────
# PIL로 직접 그리던 이전 버전들(로봇→곰→단순 얼굴)이 매번 별로라는 피드백 —
# 사용자가 직접 고른 이미지(data/mascot.png, 빨강·파랑 반반의 불꽃 스파크
# 캐릭터 — 미국·한국 두 시장 + "지금 화제(hot)" 컨셉)를 그대로 합성한다.
# 표정 변화는 없음(단일 정적 이미지, "당분간은 이걸로" — 사용자 확정).
MASCOT_SIZE = 130   # 렌더 폭(px) — 높이는 원본 비율 유지
_mascot_cache = None

def _load_mascot():
    global _mascot_cache
    if _mascot_cache is None:
        from PIL import Image
        im = Image.open(ROOT_DIR / "data" / "mascot.png").convert("RGBA")
        h = int(MASCOT_SIZE * im.height / im.width)
        _mascot_cache = im.resize((MASCOT_SIZE, h), Image.LANCZOS)
    return _mascot_cache


def draw_mascot_pil(img, rx, ry):
    """data/mascot.png를 (rx, ry) 위치에 합성."""
    from PIL import Image
    mascot = _load_mascot()
    base = img.convert("RGBA")
    base.paste(mascot, (rx, ry), mascot)
    return base.convert("RGB")

# ── 애니메이션 이펙트 ─────────────────────────────────────────────────────────

def fx_fade_in(img, t, dur=0.30):
    if t >= dur:
        return img
    from PIL import Image
    a = int((1 - t / dur) * 230)
    ov = Image.new("RGBA", img.size, (0, 0, 0, a))
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def fx_fade_out(img, t, total, dur=0.25):
    if t < total - dur:
        return img
    from PIL import Image
    a = int(((t - (total - dur)) / dur) * 230)
    ov = Image.new("RGBA", img.size, (0, 0, 0, min(a, 230)))
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def fx_speed_lines(img, t, accent, intense=False):
    """씬 시작 속도선 (만화 액션씬 느낌). intense=True면 인트로용 강화."""
    if t >= 0.55:
        return img
    from PIL import Image, ImageDraw
    a  = int((0.55 - t) / 0.55 * (130 if intense else 95))
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d  = ImageDraw.Draw(ov)
    cx, cy = W // 2, H // 2
    n_lines = 40 if intense else 22
    width   = 4  if intense else 2
    for i in range(n_lines):
        angle = (i / n_lines) * 2 * math.pi
        x1 = cx + int(math.cos(angle) * 85)
        y1 = cy + int(math.sin(angle) * 55)
        x2 = cx + int(math.cos(angle) * 1100)
        y2 = cy + int(math.sin(angle) * 1100)
        la = a if i % 3 != 1 else a // 3
        d.line([x1, y1, x2, y2], fill=(*accent, la), width=width)
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def fx_white_flash(img, t, dur=0.15):
    """인트로 첫 순간 흰색 플래시 — 충격 효과."""
    if t >= dur:
        return img
    from PIL import Image
    a = int((1 - t / dur) * 200)
    ov = Image.new("RGBA", img.size, (255, 255, 255, a))
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def fx_scanline(img, t):
    """CRT 스캔라인 + 이동 글로우 라인."""
    from PIL import Image, ImageDraw
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d  = ImageDraw.Draw(ov)
    for y in range(0, H, 4):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, 14), width=1)
    sy = int((t * 110) % H)
    d.line([(0, sy), (W, sy)], fill=(255, 255, 255, 22), width=2)
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def fx_pulse_glow(img, t, accent):
    """상단·하단 바 박동 글로우."""
    pulse = (math.sin(t * 4.5) + 1) / 2
    a     = int(18 + pulse * 52)
    from PIL import Image, ImageDraw
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d  = ImageDraw.Draw(ov)
    d.rectangle([0, 0, W, 9], fill=(*accent, a))
    d.rectangle([0, H-9, W, H], fill=(*accent, a // 2))
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def fx_ken_burns(img, t: float, dur: float, scene_idx: int):
    """배경 이미지 느린 줌 + 패닝 (Ken Burns 효과). 씬마다 방향이 다름."""
    from PIL import Image
    progress = t / max(dur, 0.001)

    # 3씬 줌/패닝 패턴 — 1.00~1.06 범위 (차분 톤에 맞춰 완만하게)
    CONFIGS = [
        # (zoom_start, zoom_end, pan_x_start, pan_x_end, pan_y_start, pan_y_end)
        (1.00, 1.05,  0.00,  0.02,  0.00,  0.01),  # scene 0 브리핑: 약한 줌인 + 우하
        (1.05, 1.00,  0.02,  0.00,  0.01,  0.00),  # scene 1 호재: 줌아웃 + 좌상
        (1.00, 1.05,  0.00,  0.00,  0.00,  0.00),  # scene 2 미래비전: 정적 줌인
    ]
    zoom_s, zoom_e, px_s, px_e, py_s, py_e = CONFIGS[scene_idx % len(CONFIGS)]

    zoom  = zoom_s + (zoom_e - zoom_s) * progress
    pan_x = px_s  + (px_e  - px_s)   * progress
    pan_y = py_s  + (py_e  - py_s)   * progress

    ow, oh = img.size  # 1080, 1920
    nw = max(int(ow * zoom), ow)
    nh = max(int(oh * zoom), oh)
    zoomed = img.resize((nw, nh), Image.LANCZOS)

    # 중앙 기준으로 패닝 오프셋 적용 후 경계 클램핑
    cx = (nw - ow) // 2 + int(pan_x * ow)
    cy = (nh - oh) // 2 + int(pan_y * oh)
    cx = max(0, min(cx, nw - ow))
    cy = max(0, min(cy, nh - oh))

    return zoomed.crop((cx, cy, cx + ow, cy + oh))

# ── 롱폼 동적 자막 (draw_scene_landscape가 더는 대본을 PNG에 굽지 않으므로, 여기서
# TTS 세그먼트 타이밍에 맞춰 한 줄씩 그린다 — 배경 이미지가 자막에 가려 안 보인다는
# 피드백 대응. 실제 자막처럼 한 번에 한 줄만 화면 하단에 표시) ──────────────────
_CAPTION_HL_RE = None

def _caption_runs(text):
    """'*...*' 마커 기준 (조각, 강조여부) 런 리스트 — prep.py의 split_runs와 동일 규칙."""
    import re
    global _CAPTION_HL_RE
    if _CAPTION_HL_RE is None:
        _CAPTION_HL_RE = re.compile(r"\*(.+?)\*")
    runs, pos = [], 0
    for m in _CAPTION_HL_RE.finditer(text):
        if m.start() > pos:
            runs.append((text[pos:m.start()], False))
        runs.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], False))
    return [(s, hl) for s, hl in runs if s]


def _find_caption(t, windows):
    for start, end, text in windows:
        if start <= t < end:
            return text
    return None


_caption_font_cache = {}

def _caption_font(size):
    from PIL import ImageFont
    f = _caption_font_cache.get(size)
    if f is None:
        try:
            from pathlib import Path as _P
            cands = [
                "/usr/share/fonts/truetype/nanum/NanumSquareRoundB.ttf",
                "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
            ]
            path = next((c for c in cands if _P(c).exists()), None)
            f = ImageFont.truetype(path, size) if path else ImageFont.load_default()
        except Exception:
            f = ImageFont.load_default()
        _caption_font_cache[size] = f
    return f


def draw_dynamic_caption(img, text):
    """자막처럼 화면 하단 중앙에 최대 2줄만 표시 — 배경을 가리지 않도록 텍스트
    바로 뒤에만 살짝 어두운 바를 깔고, 나머지 배경은 그대로 드러낸다."""
    import re
    from PIL import Image, ImageDraw
    if not text:
        return img

    img = img.convert("RGBA")
    draw = ImageDraw.Draw(img)
    font = _caption_font(48)
    max_w = int(W * 0.82)

    runs = _caption_runs(text)
    tokens = []
    for seg, hl in runs:
        for tok in re.findall(r"\S+|\s+", seg):
            tokens.append((tok, hl))

    def tok_w(s):
        return draw.textlength(s, font=font)

    lines, cur, cur_w = [], [], 0
    for tok, hl in tokens:
        w = tok_w(tok)
        if cur and cur_w + w > max_w:
            lines.append(cur); cur, cur_w = [], 0
        if cur or not tok.isspace():
            cur.append((tok, hl)); cur_w += w
    if cur:
        lines.append(cur)
    lines = lines[-2:] or [[("", False)]]   # 최대 2줄(길면 뒷부분 우선 — 앞은 이미 지나간 맥락)

    bb = draw.textbbox((0, 0), "가", font=font)
    line_h = (bb[3] - bb[1]) + 16
    pad_x, pad_y = 28, 16
    block_h = line_h * len(lines) + pad_y * 2
    y0 = H - 70 - block_h

    max_line_w = max((sum(tok_w(t) for t, _ in ln) for ln in lines), default=0)
    bx0 = (W - max_line_w) / 2 - pad_x
    bx1 = (W + max_line_w) / 2 + pad_x
    draw.rounded_rectangle([bx0, y0, bx1, y0 + block_h], radius=14, fill=(8, 12, 24, 175))

    y = y0 + pad_y
    for line in lines:
        total = sum(tok_w(t) for t, _ in line)
        x = (W - total) / 2
        for tok, hl in line:
            draw.text((x, y), tok, font=font,
                      fill=((255, 215, 0, 255) if hl else (255, 255, 255, 255)),
                      stroke_width=2, stroke_fill=(8, 12, 30, 255))
            x += tok_w(tok)
        y += line_h
    return img.convert("RGB")

# ── 애니메이션 프레임 합성 ────────────────────────────────────────────────────

def make_anime_frame(t, base_arr, accent, dur, scene_idx, caption_windows=None):
    import numpy as np
    from PIL import Image
    img = Image.fromarray(base_arr).copy()

    is_intro   = (scene_idx == 0)   # 주간 브리핑(첫 씬) — 부드러운 페이드인
    is_closing = (scene_idx == 3)   # 미래 비전(마지막 씬) — 페이드아웃

    if MODE == "long":
        # 롱폼은 배경 사진이 주인공이므로 은은한 팬/줌(Ken Burns)으로 "정적 이미지"
        # 느낌을 줄인다 — 자막·마스코트 등 오버레이보다 먼저 적용해 그것들은 고정.
        img = fx_ken_burns(img, t, dur, scene_idx)
    # Ken Burns 효과 제거 — 정적 이미지 유지 (쇼츠는 기존 그대로 무변경)

    # 차분한 분석체 톤 — 자극적 효과 제거 (속도선·플래시 약화)
    img = fx_scanline(img, t)
    img = fx_pulse_glow(img, t, accent)

    mascot_dy  = int(math.sin(t * 3.5) * 3)
    img = draw_mascot_pil(img, W - MASCOT_SIZE - 30, 26 + mascot_dy)

    # 영상 시작/종료에만 부드러운 페이드 (그 외 씬 전환은 CrossFadeIn 처리)
    if is_intro:
        img = fx_fade_in(img, t, 0.30)
    if is_closing:
        img = fx_fade_out(img, t, dur, 0.40)

    if MODE == "long":
        # 현재 나레이션 줄만 자막처럼 하단에 표시(전체 대본을 한 번에 굽지 않음 —
        # 배경이 자막에 가려 안 보인다는 피드백 대응).
        caption = _find_caption(t, caption_windows or [])
        img = draw_dynamic_caption(img, caption)
        # 롱폼(16:9)은 이미 풀프레임 이미지 — 쇼츠처럼 "핸드폰 화면 여백" 확보용
        # 90% 축소·중앙 레터박싱을 할 필요가 없다(캔버스 자체가 최종 프레임).
        return np.array(img)

    # 90% 축소 — 핸드폰 화면 여백 확보
    cw = int(W * 0.90)
    ch = int(H * 0.90)
    scaled = img.resize((cw, ch), Image.LANCZOS)
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    canvas.paste(scaled, ((W - cw) // 2, (H - ch) // 2))

    return np.array(canvas)

# ── 씬 처리 ───────────────────────────────────────────────────────────────────

async def process_scene(scene, report_dir):
    from moviepy import VideoClip, AudioFileClip
    import numpy as np
    from PIL import Image

    idx      = scene["index"]
    lines    = [l for l in scene.get("lines", []) if l.strip()]
    accent   = ACCENT_COLORS[idx]   # 0-based: 0=주간브리핑, 1=호재심층, 2=정량지표, 3=미래비전
    title    = scene.get("title", f"씬 {idx}")
    # 씬 이미지: 신규 {YYMMDD}_{회사명}_씬N.png 우선, 구 scene_NN.png 폴백 (과거 리포트 호환)
    _cands = sorted(report_dir.glob(f"*_씬{idx}.png"))
    img_path = _cands[-1] if _cands else report_dir / f"scene_{idx:02d}.png"

    # 줄 단위 세그먼트 + 씬별 브리지 문장 → 세그먼트 사이 LINE_PAUSE_MS 휴지로 합성
    segments = build_scene_tts_segments(idx, lines) or [title]
    audio_path = report_dir / f"scene_{idx:02d}.mp3"
    print(f"   🎙 씬 {idx} [{title[:20]}] 나레이션 생성... ({len(segments)}개 세그먼트)")
    timeline = await gen_audio(segments, audio_path)

    audio = AudioFileClip(str(audio_path))
    dur   = max(audio.duration, MIN_SCENE_SEC)
    caption_windows = _caption_windows(timeline, dur) if MODE == "long" else None

    if img_path.exists():
        base_arr = np.array(Image.open(img_path).convert("RGB"))
    else:
        base_arr = np.full((H, W, 3), (14, 17, 23), dtype=np.uint8)

    def make_frame(t):
        return make_anime_frame(t, base_arr, accent, dur, idx, caption_windows)

    video = VideoClip(make_frame, duration=dur).with_fps(FPS)
    video = video.with_audio(audio)

    print(f"   ✅ 씬 {idx} 완료 ({dur:.1f}초)")
    return video

# ── 영상 합성 ─────────────────────────────────────────────────────────────────

async def build_video_async(report_dir):
    from moviepy import concatenate_videoclips
    from moviepy.video.fx import CrossFadeIn

    # 색상 테마 로테이션 — prep.py와 같은 생성일 시드로 동기화 (썸네일/씬 색상 변형)
    global ACCENT_COLORS
    ACCENT_COLORS = ACCENT_THEMES[_theme_idx(report_dir.name)]

    script = json.loads((report_dir / "script.json").read_text(encoding="utf-8"))
    scenes = script.get("scenes", [])
    title  = script.get("title", f"{TICKER} 주가 분석")

    print(f"📽 {len(scenes)}개 씬 처리 (애니메이션 모드, 음성: {VOICE})")
    print(f"   제목: {title}")

    clips = []
    for scene in scenes:
        clip = await process_scene(scene, report_dir)
        clips.append(clip)

    print("\n🎬 최종 영상 합성 중 (씬 전환: 0.6초 크로스페이드)...")
    OVERLAP = 0.6
    if len(clips) > 1:
        # 첫 클립은 그대로, 이후 클립은 CrossFadeIn으로 이전 씬과 오버랩
        faded = [clips[0]]
        for c in clips[1:]:
            faded.append(c.with_effects([CrossFadeIn(OVERLAP)]))
        final = concatenate_videoclips(faded, method="compose", padding=-OVERLAP)
    else:
        final = clips[0]
    final = final.with_fps(FPS)

    # ── BGM 믹싱 ────────────────────────────────────────────────────────────
    bgm_path = download_bgm(f"{TICKER}{report_dir.name}")
    if bgm_path:
        try:
            from moviepy import AudioFileClip as _AFC, CompositeAudioClip, concatenate_audioclips
            from moviepy.audio.fx import MultiplyVolume
            # 길이 측정용 1회 로드 후 닫기
            _probe = _AFC(str(bgm_path))
            bgm_dur = max(_probe.duration, 0.1)
            _probe.close()
            n_loops = max(1, math.ceil(final.duration / bgm_dur))
            # 같은 인스턴스를 재사용하면 리더 start가 공유돼 루프가 깨지므로 루프마다 새 클립 생성
            bgm_clips = [_AFC(str(bgm_path)).with_effects([MultiplyVolume(BGM_VOLUME)])
                         for _ in range(n_loops)]
            bgm_looped = (concatenate_audioclips(bgm_clips) if n_loops > 1
                          else bgm_clips[0]).with_duration(final.duration)
            if final.audio:
                final = final.with_audio(CompositeAudioClip([final.audio, bgm_looped]))
            # write 이후 프로세스 종료 시 정리 — write 전 close 금지(리더 끊김 방지)
            print(f"   🎵 BGM 믹싱 완료 (볼륨 {int(BGM_VOLUME*100)}%)")
        except Exception as e:
            print(f"   ⚠ BGM 믹싱 실패: {e} — 음악 없이 진행", file=sys.stderr)

    # 파일명: {한글 종목명}_YYYYMMDD.mp4 (예: SK하이닉스_20260709.mp4) — 숫자 코드 대신
    # 사람이 읽는 종목명으로(사용자 요청). 리포트 디렉토리명(YYYY-MM-DD)에서 날짜 생성.
    out_path = report_dir / f"{safe_filename(COMPANY_KO)}_{report_dir.name.replace('-', '')}.mp4"
    final.write_videofile(
        str(out_path),
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        preset="ultrafast",
        threads=2,
        logger=None,
    )

    dur = final.duration
    final.close()
    for c in clips:
        c.close()

    print(f"\n✅ 영상 생성 완료!")
    print(f"   📁 {out_path}")
    print(f"   ⏱ 총 {dur:.1f}초 ({dur/60:.1f}분)")
    return out_path

# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    report_dir = find_latest_report()
    if not report_dir:
        print("⚠ script.json 없음 — weekly_video_prep.py 먼저 실행하세요", file=sys.stderr)
        sys.exit(1)

    print(f"📁 보고서: {report_dir.name}")
    asyncio.run(build_video_async(report_dir))


if __name__ == "__main__":
    main()
