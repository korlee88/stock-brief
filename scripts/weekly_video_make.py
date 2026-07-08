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
VOICE         = "ko-KR-SunHiNeural"    # 밝은 여성 — 친근 튜닝 (edge-tts 지원 검증 음성)
RATE          = "+13%"                  # 대화하듯 자연스러운 속도
PITCH         = "+6Hz"                  # 살짝 올려 밝고 친근한 톤
LINE_PAUSE_MS = 600                     # 대본 줄(세그먼트) 사이 무음 휴지 (ms)
TRIM_DB       = -42.0                   # 세그먼트 가장자리 무음 판정 임계 (dBFS)
TRIM_KEEP_MS  = 60                      # 트리밍 후 가장자리에 남길 무음 (ms)
SCENE_LEAD_MS = 500                     # 씬 시작~첫 나레이션 사이 여유 무음 (씬 전환 딜레이)
SCENE_TAIL_MS = 300                     # 씬 끝 여유 무음 (ms)
FPS           = 24
W, H          = 1080, 1920
PHOTO_Y       = 500                     # 헤더 아래 사진 시작 Y (prep.py의 HEADER_H와 동일)
PHOTO_H       = 500                     # 사진 영역 높이 (prep.py의 PHOTO_H와 동일)
MIN_SCENE_SEC = 5.0

ACCENT_COLORS = [
    (167, 139, 250),  # scene 0 purple  - 주간 브리핑
    (34,  197,  94),  # scene 1 green   - 호재 심층
    (236,  72, 153),  # scene 2 magenta - 미래 비전 (클로징)
]

# 색상 테마 로테이션 (prep.py와 동일 — 생성일 시드로 동기화). 씬1(호재)은 항상 초록.
ACCENT_THEMES = [
    [(167, 139, 250), (34, 197, 94), (236, 72, 153)],  # A 보라·초록·마젠타 (기존)
    [(56, 189, 248),  (34, 197, 94), (251, 146, 60)],  # B 시안·초록·오렌지
    [(129, 140, 248), (34, 197, 94), (250, 204, 21)],  # C 인디고·초록·골드
]

def _theme_idx(date_str):
    """생성일 문자열로 결정적 테마 인덱스 (prep.py와 동일 함수 → 색상 동기화)."""
    return sum(ord(c) for c in (date_str or "")) % len(ACCENT_THEMES)
SCENE_MOODS = ["focused", "happy", "celebrating"]   # 차분 분석 톤에 맞춘 마스코트

# ── BGM 설정 (원본 합성 · CC0/로열티프리) ────────────────────────────────────
# 배경음악은 저장소에 커밋된 data/bgm.mp3 를 사용한다 → 빌드 시 네트워크 의존 0.
# 이 파일은 scripts/make_bgm.py 가 생성한 '원본' 앰비언트 패드라 저작권·출처 표기 의무가 없다.
# (외부 CC0 사이트는 빌드 환경에서 불안정: FreePD는 JS 렌더링이라 스크래핑 불가,
#  archive.org는 CC0 검색이 비고, yt-dlp+YouTube는 러너 IP 봇 차단 → 직접 합성·커밋으로 확정.)
BGM_VOLUME = 0.10                            # 나레이션 아래 배경음 (10%)
BGM_CACHE  = ROOT_DIR / "data" / "bgm.mp3"

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


def download_bgm() -> "Path | None":
    """저장소에 커밋된 BGM(data/bgm.mp3)을 반환. 없으면 None → 음악 없이 진행.

    음원은 scripts/make_bgm.py 로 미리 생성·커밋한다(원본·로열티프리). 빌드 중 네트워크 0.
    교체하려면 data/bgm.mp3 를 원하는 트랙으로 바꿔 커밋하면 된다.
    """
    if BGM_CACHE.exists():
        print(f"   🎵 BGM 사용: {BGM_CACHE.name}")
        return BGM_CACHE
    print("   ⚠ data/bgm.mp3 없음 — 음악 없이 진행", file=sys.stderr)
    return None


def clean_for_tts(lines):
    table = {
        '【': '', '】': '', '①': '첫째,', '②': '둘째,', '③': '셋째,',
        '④': '넷째,', '⑤': '다섯째,', '$': '달러 ', '%': '퍼센트,',
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
        '④': '넷째,', '⑤': '다섯째,', '$': '달러 ', '%': '퍼센트,',
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
        # 회사 소개 & 간략한 주가 흐름 — 줄1(간략한 주가 흐름) + 줄2~(회사가 추구하는 방향)
        head    = cleaned[0] if cleaned else ""       # 간략한 주가 흐름
        dir1    = cleaned[1] if len(cleaned) > 1 else ""   # 회사 방향·비전
        dir2    = cleaned[2] if len(cleaned) > 2 else ""   # 방향·강점 부연
        segments = []
        if head: segments.append(head)
        if dir1: segments.append("이 회사가 그리는 방향은 이래요. " + dir1)
        if dir2: segments.append(dir2)

    elif idx == 1:
        # 핵심 뉴스 3선 — 호재/악재/보합 각 줄("호재:/악재:/보합:" 접두어)에 다정한 브리지를 붙여 narration
        import re as _re
        bridges = {
            "호재": "먼저 좋은 소식이에요. ",
            "악재": "반대로 짚어볼 점은요. ",
            "보합": "끝으로 중립적인 소식이에요. ",
        }
        segments = []
        for ln in cleaned:
            m = _re.match(r'^\s*(호재|악재|보합)\s*[:：]\s*(.*)$', ln)
            if m:
                key, body = m.group(1), m.group(2).strip()
                segments.append((bridges.get(key, "") + body).strip() if body else "")
            elif ln.strip():
                segments.append(ln)   # 접두어 없는 예외 줄도 살림

    elif idx == 2:
        # 클로징(다음주 전망) — 6줄: 일정·시나리오·가격예측·흐름·변수·마무리
        head = cleaned[0] if cleaned else ""
        rest = cleaned[1:]
        segments = []
        if head:
            segments.append("자, 다음 주는 어떨까요? " + head)
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
    """
    segments = [s for s in segments if s and s.strip()]
    if not segments:
        return
    try:
        from pydub import AudioSegment
        line_gap = AudioSegment.silent(duration=LINE_PAUSE_MS)
        combined = None
        for i, seg in enumerate(segments):
            tmp = path.with_name(f"{path.stem}__seg{i}.mp3")
            await _tts(seg, tmp)
            piece = _trim_edge_silence(AudioSegment.from_file(tmp))
            combined = piece if combined is None else (combined + line_gap + piece)
            try: tmp.unlink()
            except Exception: pass
        # 씬 시작 SCENE_LEAD_MS·끝 SCENE_TAIL_MS 여유 무음 — 씬 전환 직후 나레이션이 바로 붙지 않게
        combined = (AudioSegment.silent(duration=SCENE_LEAD_MS)
                    + combined + AudioSegment.silent(duration=SCENE_TAIL_MS))
        combined.export(str(path), format="mp3")
    except Exception as e:
        print(f"   ⚠ 세그먼트 합성 실패({e}) → 단일 TTS 폴백", file=sys.stderr)
        await _tts(" ".join(segments), path)

# ── 로봇 마스코트 ─────────────────────────────────────────────────────────────

def draw_robot_pil(img, rx, ry, mood="neutral", accent=(167, 139, 250)):
    """Pillow 도형으로 로봇 캐릭터를 img 위에 합성."""
    from PIL import Image, ImageDraw
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d     = ImageDraw.Draw(layer)
    BODY  = (38, 44, 60)
    METAL = (78, 88, 112)
    SHINE = (215, 225, 240)
    _G    = (34, 197, 94)
    _R    = (239, 68, 68)
    _A    = (245, 158, 11)

    ax = rx + 50
    d.line([ax, ry-26, ax, ry], fill=METAL, width=3)
    d.ellipse([ax-8, ry-36, ax+8, ry-20], fill=accent, outline=SHINE, width=1)
    d.rounded_rectangle([rx, ry, rx+100, ry+86], radius=18, fill=BODY, outline=METAL, width=2)

    ey = ry + 22
    lx, rx2 = rx+13, rx+57
    ew, eh  = 28, 22

    if mood == "happy":
        d.arc([lx, ey, lx+ew, ey+eh], start=200, end=340, fill=accent, width=4)
        d.arc([rx2, ey, rx2+ew, ey+eh], start=200, end=340, fill=accent, width=4)
    elif mood == "excited":
        d.ellipse([lx, ey-2, lx+ew, ey+ew-2], fill=accent)
        d.ellipse([rx2, ey-2, rx2+ew, ey+ew-2], fill=accent)
        d.ellipse([lx+4, ey+2, lx+9, ey+7], fill=SHINE)
        d.ellipse([rx2+4, ey+2, rx2+9, ey+7], fill=SHINE)
    elif mood == "worried":
        d.line([lx, ey+10, lx+ew, ey+4], fill=_R, width=5)
        d.line([rx2, ey+4, rx2+ew, ey+10], fill=_R, width=5)
    elif mood == "focused":
        d.rectangle([lx, ey+7, lx+ew, ey+15], fill=accent)
        d.rectangle([rx2, ey+7, rx2+ew, ey+15], fill=accent)
    elif mood == "shocked":
        # 동그란 큰 눈 + 동공 작게
        d.ellipse([lx-2, ey-4, lx+ew+2, ey+ew], fill=SHINE)
        d.ellipse([rx2-2, ey-4, rx2+ew+2, ey+ew], fill=SHINE)
        d.ellipse([lx+ew//2-3, ey+ew//2-3, lx+ew//2+3, ey+ew//2+3], fill=_R)
        d.ellipse([rx2+ew//2-3, ey+ew//2-3, rx2+ew//2+3, ey+ew//2+3], fill=_R)
        # 머리 위 ! 표시
        d.text((ax-4, ry-58), "!", fill=_R)
    elif mood == "celebrating":
        # 별 모양 눈 (대각선 + 가로 라인)
        for cx_eye in (lx + ew // 2, rx2 + ew // 2):
            cy_eye = ey + ew // 2
            d.line([cx_eye-10, cy_eye, cx_eye+10, cy_eye], fill=accent, width=3)
            d.line([cx_eye, cy_eye-10, cx_eye, cy_eye+10], fill=accent, width=3)
            d.line([cx_eye-7, cy_eye-7, cx_eye+7, cy_eye+7], fill=accent, width=2)
            d.line([cx_eye-7, cy_eye+7, cx_eye+7, cy_eye-7], fill=accent, width=2)
    else:
        d.rectangle([lx, ey, lx+ew, ey+eh], fill=accent)
        d.rectangle([rx2, ey, rx2+ew, ey+eh], fill=accent)
        d.ellipse([lx+4, ey+3, lx+9, ey+9], fill=SHINE)
        d.ellipse([rx2+4, ey+3, rx2+9, ey+9], fill=SHINE)

    my = ry + 60
    if mood in ("happy", "excited", "celebrating"):
        d.arc([rx+26, my-8, rx+74, my+14], start=0, end=180, fill=accent, width=3)
    elif mood == "worried":
        d.arc([rx+26, my, rx+74, my+18], start=180, end=360, fill=_R, width=3)
    elif mood == "shocked":
        # 입을 큰 동그라미 (놀란 표정)
        d.ellipse([rx+38, my-4, rx+62, my+18], fill=_R, outline=SHINE, width=2)
    else:
        d.line([rx+30, my+6, rx+70, my+6], fill=METAL, width=3)

    bx, by = rx+12, ry+94
    d.rounded_rectangle([bx, by, bx+76, by+66], radius=10, fill=BODY, outline=METAL, width=2)
    d.rounded_rectangle([bx+18, by+10, bx+58, by+44], radius=6, fill=accent)
    tx = bx + 28
    d.line([tx, by+16, tx+20, by+16], fill=(255,255,255), width=3)
    d.line([tx+10, by+16, tx+10, by+40], fill=(255,255,255), width=3)
    led = _G if mood in ("happy","excited") else _R if mood=="worried" else _A
    d.ellipse([bx+56, by+46, bx+66, by+56], fill=led)

    d.rounded_rectangle([bx-20, by+8, bx-5, by+48], radius=6, fill=METAL)
    d.rounded_rectangle([bx+81, by+8, bx+96, by+48], radius=6, fill=METAL)
    d.ellipse([bx-24, by+42, bx-6, by+60], fill=METAL)
    d.ellipse([bx+82, by+42, bx+100, by+60], fill=METAL)
    d.rounded_rectangle([bx+8,  by+70, bx+30, by+88], radius=6, fill=METAL)
    d.rounded_rectangle([bx+46, by+70, bx+68, by+88], radius=6, fill=METAL)
    d.rounded_rectangle([bx+4,  by+84, bx+34, by+96], radius=4, fill=(55,62,80))
    d.rounded_rectangle([bx+42, by+84, bx+72, by+96], radius=4, fill=(55,62,80))

    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")

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

# ── 애니메이션 프레임 합성 ────────────────────────────────────────────────────

def make_anime_frame(t, base_arr, accent, dur, scene_idx):
    import numpy as np
    from PIL import Image
    img = Image.fromarray(base_arr).copy()

    is_intro   = (scene_idx == 0)   # 주간 브리핑(첫 씬) — 부드러운 페이드인
    is_closing = (scene_idx == 2)   # 미래 비전(마지막 씬) — 페이드아웃

    # Ken Burns 효과 제거 — 정적 이미지 유지

    # 차분한 분석체 톤 — 자극적 효과 제거 (속도선·플래시 약화)
    img = fx_scanline(img, t)
    img = fx_pulse_glow(img, t, accent)

    mood     = SCENE_MOODS[scene_idx]
    robot_dy = int(math.sin(t * 3.5) * 3)
    img = draw_robot_pil(img, W - 130, 40 + robot_dy, mood, accent)

    # 영상 시작/종료에만 부드러운 페이드 (그 외 씬 전환은 CrossFadeIn 처리)
    if is_intro:
        img = fx_fade_in(img, t, 0.30)
    if is_closing:
        img = fx_fade_out(img, t, dur, 0.40)

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
    accent   = ACCENT_COLORS[idx]   # 0-based: 0=주간브리핑, 1=호재심층, 2=미래비전
    title    = scene.get("title", f"씬 {idx}")
    img_path = report_dir / f"scene_{idx:02d}.png"

    # 줄 단위 세그먼트 + 씬별 브리지 문장 → 세그먼트 사이 LINE_PAUSE_MS 휴지로 합성
    segments = build_scene_tts_segments(idx, lines) or [title]
    audio_path = report_dir / f"scene_{idx:02d}.mp3"
    print(f"   🎙 씬 {idx} [{title[:20]}] 나레이션 생성... ({len(segments)}개 세그먼트)")
    await gen_audio(segments, audio_path)

    audio = AudioFileClip(str(audio_path))
    dur   = max(audio.duration, MIN_SCENE_SEC)

    if img_path.exists():
        base_arr = np.array(Image.open(img_path).convert("RGB"))
    else:
        base_arr = np.full((H, W, 3), (14, 17, 23), dtype=np.uint8)

    def make_frame(t):
        return make_anime_frame(t, base_arr, accent, dur, idx)

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
    bgm_path = download_bgm()
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
