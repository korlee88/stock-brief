#!/usr/bin/env python3
"""씬 이미지 로컬 프리뷰 — 영상 게시 전 배치·글씨·오타를 사람이 직접 눈으로 확인하기 위한 도구.

자동 파이프라인(weekly-video.yml)은 사람 없는 새벽에 돌아 한 단계가 조용히 깨져도
그대로 게시되므로, 손으로 고친 직후엔 이 도구로 실제 렌더 결과를 눈으로 확인한다.

사용법:
    # 1) 한글 폰트·(음성 합성용)ffmpeg 준비 — 없으면 글씨가 깨진 기본 비트맵으로 나옴
    apt-get install -y fonts-nanum ffmpeg

    # 2) 최신 대본으로 씬 3장 렌더 (실데이터 data/auto-sessions.json 기준)
    python3 scripts/preview_scenes.py
    #    → out/preview/scene_0.png ~ scene_2.png

    # 특정 대본/출력 위치 지정
    python3 scripts/preview_scenes.py --script data/weekly-report/2026-06-26/script.txt --out /tmp/pv

확인 못 하는 것 (제약):
    - 음성 실제 소리: edge-tts가 wss://speech.platform.bing.com WebSocket을 쓰는데
      에이전트 프록시가 막아 이 환경에선 합성 불가 → 읽는 문장·속도는 --dump-tts로 텍스트 확인.
      실제 소리는 CI(GitHub Actions) 산출물로 확인.
    - AI 배경 이미지: GEMINI/ANTHROPIC API 키 필요. 키 없으면 깔끔한 폴백 배경으로 렌더되며
      (배치·글씨 확인엔 충분), 프롬프트 텍스트는 --dump-prompts로 확인.
"""
import sys, os, glob, argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import weekly_video_prep as wp  # noqa: E402


def latest_script():
    cands = sorted(glob.glob("data/weekly-report/*/script.txt"))
    return cands[-1] if cands else None


def main():
    ap = argparse.ArgumentParser(description="씬 이미지 로컬 프리뷰")
    ap.add_argument("--script", help="대본 txt 경로 (기본: data/weekly-report 최신)")
    ap.add_argument("--out", default="out/preview", help="PNG 출력 디렉토리")
    ap.add_argument("--dump-tts", action="store_true", help="씬별 TTS 세그먼트(읽는 문장) 출력")
    ap.add_argument("--dump-prompts", action="store_true", help="씬별 AI 배경 이미지 프롬프트 출력")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="출력 배율 (예: 0.5 = 절반 해상도 — 에이전트 이미지 확인용 토큰 절감. 사용자 전달용은 1.0)")
    args = ap.parse_args()

    sessions = wp.load_week_sessions()
    summary = wp.summarize(sessions)
    if not summary:
        # 로컬 데이터가 분석 윈도우(LOOKBACK_DAYS) 밖이면 세션이 비어 None — 빈 요약으로 렌더(배치 확인엔 충분)
        print("⚠ 윈도우 내 세션 없음 — 빈 요약으로 렌더(가격·뉴스 칸은 폴백 표시)")
        summary = {}
    # 전일 시가→종가 (Yahoo 일봉) — 실패해도 계속 진행(씬0 스트립만 생략됨)
    summary["prev_day"] = wp.fetch_prev_day_ohlc(wp.TICKER)
    print(f"기간: {summary.get('week_start')} ~ {summary.get('week_end')}")

    spath = args.script or latest_script()
    if not spath or not os.path.exists(spath):
        print("대본을 찾을 수 없습니다. --script 로 경로를 지정하세요.")
        return 1
    print(f"대본: {spath}")
    raw_text = open(spath).read()
    scenes = wp.parse_script(raw_text)

    reg, bold = wp.find_font()
    if not reg:
        print("⚠ 한글 폰트 없음 — 글씨가 깨집니다. `apt-get install -y fonts-nanum` 후 재실행 권장.")

    os.makedirs(args.out, exist_ok=True)
    for sc in scenes:
        img = wp.build_scene_image(sc, summary, reg, bold)
        if args.scale != 1.0:
            img = img.resize((int(img.width * args.scale), int(img.height * args.scale)))
        p = os.path.join(args.out, f"scene_{sc['index']}.png")
        img.save(p)
        print(f"  저장: {p}  {img.size}")

    if args.dump_tts:
        try:
            import weekly_video_make as wm
            print("\n── 씬별 TTS 세그먼트(읽는 문장) ──")
            for sc in scenes:
                print(f"[씬{sc['index']}]")
                for s in wm.build_scene_tts_segments(sc["index"], sc["lines"]):
                    print("   •", s)
        except Exception as e:
            print("TTS 세그먼트 출력 실패:", e)

    if args.dump_prompts:
        print("\n── 씬별 AI 배경 이미지 프롬프트 ──")
        for ln in raw_text.splitlines():
            if ln.strip().startswith("IMAGE_PROMPT"):
                print("  ", ln.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
