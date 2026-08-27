"""data/bgm/*.mp3 생성기 (원본·로열티프리/CC0).

외부 CC0 음원 사이트(FreePD=JS 렌더링, archive.org=CC0 검색 불가)가 빌드 환경에서
안정적으로 접근되지 않아, 저작권·네트워크 의존이 전혀 없는 '원본' 배경 음악을 직접 합성한다.

트랙 두 종류:
  · 코드 패드(warm/dreamy/airy — PRESETS): 따뜻한 코드 패드(스테레오 코러스) + 사이클마다
    바뀌는 아르페지오 + 가끔 울리는 high shimmer + 스테레오 크로스피드 에코.
  · 왈츠(waltz — generate_waltz): 사용자가 "하울의 움직이는 성" OST 같은 분위기를 요청 —
    실제 멜로디는 베끼지 않고 같은 장르(따뜻한 3박자 왈츠풍 피아노, 오르골·캐러셀 느낌)로
    새로 작곡한 오리지널 멜로디 + 붕작작 반주.
나레이션 아래 10%용.

여러 트랙을 생성해 매 영상 생성마다 다른 곡으로 변화를 준다(사용자 요청 — 항상 같은
곡이라 단조롭다는 피드백). weekly_video_make.py의 download_bgm()이 data/bgm/ 안의
파일 중 하나를 (티커+날짜) 시드로 결정적으로 골라 쓴다.

재생성: python scripts/make_bgm.py  (출력: data/bgm/<이름>.mp3, 이음매 없는 스테레오 루프,
시드 고정이라 재생성해도 항상 동일한 결과)
"""
import math
from pathlib import Path
import numpy as np

SR = 44100
CHORD_SEC = 4.0
CYCLES = 4
FADE = 1.0          # 루프 이음매 크로스페이드 길이(초)
ARP_NOTE = 0.5      # 아르페지오 한 음 길이(초)
DETUNE = 0.0035     # 스테레오 코러스 디튠 비율(약 6센트) — L/R 살짝 다르게 해 폭을 만듦

# 사이클마다 다른 아르페지오 패턴(코드 톤 인덱스) — 똑같은 루프가 기계적으로 반복되지 않도록
ARP_PATTERNS = [
    [0, 1, 2, 3, 2, 1, 2, 3],
    [0, 2, 1, 3, 1, 2, 3, 2],
    [0, 1, 3, 2, 3, 1, 2, 1],
    [2, 1, 0, 1, 2, 3, 2, 3],
]

# 프리셋별 코드 진행(Hz) — 전부 잔잔한 메이저/서스 계열, 무드만 다르게 (사용자 요청: 매번
# 다른 잔잔하고 듣기 좋은 곡).
PRESETS = {
    # 기존 곡 그대로 — 따뜻한 재즈풍 메이저7 패드
    "warm": {
        "seed": 7,
        "chords": [
            ([261.63, 329.63, 392.00, 493.88], 130.81),  # Cmaj7
            ([220.00, 261.63, 329.63, 392.00], 110.00),  # Am7
            ([174.61, 220.00, 261.63, 329.63],  87.31),  # Fmaj7
            ([196.00, 246.94, 293.66, 349.23],  98.00),  # G7
        ],
    },
    # 더 몽환적인 마이너7 계열 — 슬프지 않고 차분하고 부드러운 무드
    "dreamy": {
        "seed": 11,
        "chords": [
            ([293.66, 349.23, 440.00, 523.25], 146.83),  # Dm7
            ([233.08, 293.66, 349.23, 440.00], 116.54),  # Bbmaj7
            ([196.00, 233.08, 293.66, 349.23],  98.00),  # Gm7
            ([220.00, 293.66, 329.63, 392.00], 110.00),  # A7sus4
        ],
    },
    # add9 보이싱으로 더 넓고 공기 같은(airy) 느낌 — 흔한 I-V-vi-IV 진행을 색다르게
    "airy": {
        "seed": 17,
        "chords": [
            ([261.63, 329.63, 392.00, 587.33], 130.81),  # Cadd9
            ([196.00, 246.94, 293.66, 440.00],  98.00),  # Gadd9
            ([220.00, 261.63, 329.63, 493.88], 110.00),  # Am(add9)
            ([174.61, 220.00, 261.63, 392.00],  87.31),  # Fadd9
        ],
    },
}


def _tone(freq, n, harm, t0, bright_rate=0.06, bright_depth=0.35, bright_phase=0.0):
    """배음 진폭이 절대 시간 기준 느린 LFO로 출렁이는 톤 — 같은 코드라도 시간에 따라 색이 변한다."""
    t = (np.arange(n) + t0) / SR
    w = np.sin(2 * np.pi * freq * t)
    bright = 1.0 + bright_depth * np.sin(2 * np.pi * bright_rate * t + bright_phase)
    for k, amp in harm:
        w += amp * bright * np.sin(2 * np.pi * k * freq * t)
    return w


def grain_stereo(freqs, sub, dur, t0, detune_sign):
    """부드러운 패드 코드 (긴 어택/릴리즈로 서로 자연스럽게 겹침). detune_sign=+1/-1로 L/R 디튠."""
    n = int(dur * SR)
    sig = np.zeros(n)
    for fi, f in enumerate(freqs):
        sig += _tone(f * (1 + detune_sign * DETUNE), n, [(2, 0.30), (3, 0.12)],
                     t0, bright_rate=0.05 + 0.01 * fi, bright_phase=fi * 1.3)
    sig += 1.1 * _tone(sub * (1 + detune_sign * DETUNE * 0.5), n, [(2, 0.25)], t0, bright_rate=0.04)
    env = np.ones(n)
    a, r = int(0.5 * SR), int(1.6 * SR)
    env[:a] = np.linspace(0, 1, a) ** 2
    env[-r:] = np.linspace(1, 0, r) ** 2
    return sig * env


def pluck(freq, n, amp_scale=1.0):
    """뜯는 듯한 짧은 음 (아르페지오용) — 빠른 어택 + 지수 감쇠."""
    t = np.arange(n) / SR
    env = np.exp(-t * 4.5) * amp_scale
    w = (_tone(freq, n, [(2, 0.4), (3, 0.15)], 0, bright_rate=0)) * env
    a = int(0.006 * SR)
    w[:a] *= np.linspace(0, 1, a)
    return w


def bell(freq, n):
    """드문드문 울리는 high shimmer — 느린 어택 + 긴 잔향성 감쇠."""
    t = np.arange(n) / SR
    env = np.exp(-t * 1.1) * np.clip(t / 0.8, 0, 1)
    return _tone(freq, n, [(2, 0.18)], 0, bright_rate=0) * env


def echo_stereo(sigL, sigR, delay_s=0.36, decay=0.34, taps=4, cross=0.45, r_offset_s=0.07):
    """스테레오 크로스피드 에코 — L/R을 서로 살짝 섞고 지연 시간도 달라 더 넓고 흐릿한 잔향감."""
    outL, outR = sigL.copy(), sigR.copy()
    d = int(delay_s * SR)
    ro = int(r_offset_s * SR)
    n = len(sigL)
    for i in range(1, taps + 1):
        dl = i * d
        if dl < n:
            outL[dl:] += (sigL[:n - dl] * (1 - cross) + sigR[:n - dl] * cross) * (decay ** i)
        dr = dl + ro
        if dr < n:
            outR[dr:] += (sigR[:n - dr] * (1 - cross) + sigL[:n - dr] * cross) * (decay ** i)
    return outL, outR


def _export(name, loopL, loopR, peak):
    """정규화·PCM 변환·mp3 인코딩까지 공통 마무리 (트랙 종류 무관 공용)."""
    pcm = np.empty(len(loopL) * 2, dtype=np.int16)
    pcm[0::2] = (loopL * 32767).astype(np.int16)
    pcm[1::2] = (loopR * 32767).astype(np.int16)

    out_dir = Path(__file__).parent.parent / "data" / "bgm"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{name}.mp3"
    try:
        import lameenc
        enc = lameenc.Encoder()
        enc.set_bit_rate(160)
        enc.set_in_sample_rate(SR)
        enc.set_channels(2)
        enc.set_quality(2)
        mp3 = enc.encode(pcm.tobytes()) + enc.flush()
        out.write_bytes(mp3)
        print(f"✅ {out} ({len(mp3)} bytes, {len(loopL) / SR:.1f}s stereo, peak={peak:.2f})")
    except ImportError:
        import wave
        wav = out.with_suffix(".wav")
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        print(f"⚠ lameenc 없음 → {wav} (WAV) 생성. ffmpeg로 mp3 변환 필요")


def generate_track(name, chords, seed):
    rng = np.random.default_rng(seed)
    total = int(CHORD_SEC * len(chords) * CYCLES * SR)
    buf_len = total + int(2.0 * SR)
    padL = np.zeros(buf_len)
    padR = np.zeros(buf_len)
    arpL = np.zeros(buf_len)
    arpR = np.zeros(buf_len)
    bellL = np.zeros(buf_len)
    bellR = np.zeros(buf_len)

    step = int(CHORD_SEC * SR)
    nlen = int(ARP_NOTE * SR)
    idx = 0
    hit_i = 0
    for c in range(CYCLES):
        pattern = ARP_PATTERNS[c % len(ARP_PATTERNS)]
        for ci, (freqs, sub) in enumerate(chords):
            # 패드(다음 코드와 1.6초 겹침) — L/R 살짝 디튠해 코러스/스테레오 폭 형성
            gL = grain_stereo(freqs, sub, CHORD_SEC + 1.6, idx, +1)
            gR = grain_stereo(freqs, sub, CHORD_SEC + 1.6, idx, -1)
            end = min(idx + len(gL), buf_len)
            padL[idx:end] += gL[:end - idx]
            padR[idx:end] += gR[:end - idx]

            # 아르페지오 (코드 톤, 한 옥타브 위) — 사이클마다 다른 패턴 + 휴머나이즈 지터
            for k, pi in enumerate(pattern):
                f = freqs[pi % len(freqs)] * 2.0
                jitter_t = int(rng.uniform(-0.015, 0.015) * SR)
                jitter_amp = rng.uniform(0.85, 1.0)
                s = max(0, idx + int(k * ARP_NOTE * SR) + jitter_t)
                p = pluck(f, nlen, jitter_amp)
                e = min(s + len(p), buf_len)
                # 아르페지오도 좌우로 약하게 흔들어 정적인 중앙 고정음 탈피 (짝/홀 음 교대로 살짝 좌우)
                gLp, gRp = (0.95, 0.55) if k % 2 == 0 else (0.55, 0.95)
                arpL[s:e] += p[:e - s] * gLp
                arpR[s:e] += p[:e - s] * gRp

            # 가끔 울리는 high shimmer — 코드 전환마다 매번이 아니라 절반만, 좌우 교대로 팬
            if hit_i % 2 == 0:
                bn = int(3.5 * SR)
                b = bell(freqs[0] * 4.0, bn)
                e = min(idx + bn, buf_len)
                if hit_i % 4 == 0:
                    bellL[idx:e] += b[:e - idx] * 0.85
                    bellR[idx:e] += b[:e - idx] * 0.30
                else:
                    bellL[idx:e] += b[:e - idx] * 0.30
                    bellR[idx:e] += b[:e - idx] * 0.85
            hit_i += 1
            idx += step

    arpL, arpR = echo_stereo(arpL, arpR)
    bufL = 0.85 * padL[:total] + 0.5 * arpL[:total] + 0.13 * bellL[:total]
    bufR = 0.85 * padR[:total] + 0.5 * arpR[:total] + 0.13 * bellR[:total]

    # 이음매 없는 루프: 앞 FADE초를 끝 FADE초와 크로스페이드 (L/R 동일하게)
    F = int(FADE * SR)
    ramp = np.linspace(0, 1, F)
    for buf in (bufL, bufR):
        head, tail = buf[:F].copy(), buf[-F:].copy()
        buf[:F] = head * ramp + tail * (1 - ramp)
    loopL = bufL[:len(bufL) - F]
    loopR = bufR[:len(bufR) - F]

    # 정규화 (-3 dBFS, L/R 공통 스케일로 스테레오 밸런스 유지)
    peak = max(np.max(np.abs(loopL)), np.max(np.abs(loopR))) or 1.0
    scale = (10 ** (-3 / 20)) / peak
    loopL = loopL * scale
    loopR = loopR * scale
    _export(name, loopL, loopR, peak)


def piano_tone(freq, n, decay=2.0, amp=1.0):
    """피아노/뮤직박스 느낌의 톤 — 배음 여러 개(2·3·4배음) + 지수 감쇠. 왈츠 멜로디·반주용."""
    t = np.arange(n) / SR
    env = np.exp(-t * decay) * amp
    w = _tone(freq, n, [(2, 0.5), (3, 0.25), (4, 0.12)], 0, bright_rate=0)
    a = int(0.004 * SR)
    w[:a] *= np.linspace(0, 1, a)
    return w * env


# 왈츠 프리셋 — 사용자가 "하울의 움직이는 성" OST 같은 분위기를 요청(따뜻한 3박자 왈츠풍
# 피아노). 실제 멜로디를 베끼지 않고 같은 장르로 새로 작곡한 오리지널 캐러셀풍 멜로디.
# 마디: (베이스 Hz, 코드 톤 2개 Hz, [(멜로디 Hz, 박자), ...]) — 박자 합이 마디당 3이 되게 구성.
WALTZ_BPM = 132
WALTZ_SEED = 23
WALTZ_CYCLES = 3
WALTZ_BARS = [
    (174.61, (220.00, 261.63), [(523.25, 1), (587.33, 1), (698.46, 1)]),  # F:  C5 D5 F5
    (146.83, (174.61, 220.00), [(659.25, 1), (587.33, 1), (523.25, 1)]),  # Dm: E5 D5 C5
    (116.54, (146.83, 174.61), [(466.16, 1), (523.25, 1), (587.33, 1)]),  # Bb: Bb4 C5 D5
    (130.81, (164.81, 196.00), [(523.25, 2), (466.16, 1)]),               # C:  C5(2) Bb4
    (174.61, (220.00, 261.63), [(440.00, 1), (523.25, 1), (698.46, 1)]),  # F:  A4 C5 F5
    (146.83, (174.61, 220.00), [(698.46, 1), (659.25, 1), (587.33, 1)]),  # Dm: F5 E5 D5
    (116.54, (146.83, 174.61), [(523.25, 1), (466.16, 1), (440.00, 1)]),  # Bb: C5 Bb4 A4
    (130.81, (164.81, 196.00), [(392.00, 2), (349.23, 1)]),               # C:  G4(2) F4
]


def generate_waltz(name="waltz", seed=WALTZ_SEED):
    """3/4박 왈츠 — 오르골·캐러셀 느낌의 붕작작 반주 위에 손으로 쓴 멜로디."""
    rng = np.random.default_rng(seed)
    beat_n = int(60 / WALTZ_BPM * SR)
    bar_beats = 3
    total = len(WALTZ_BARS) * WALTZ_CYCLES * bar_beats * beat_n
    buf_len = total + int(2.0 * SR)
    melL, melR = np.zeros(buf_len), np.zeros(buf_len)
    accL, accR = np.zeros(buf_len), np.zeros(buf_len)

    idx = 0
    for _cyc in range(WALTZ_CYCLES):
        for bass, chord, melody in WALTZ_BARS:
            # 반주(붕-작-작): 베이스는 1박(약간 넓게), 코드는 2·3박에 짧게 — 좌우로 살짝 벌림
            bt = piano_tone(bass, int(1.6 * beat_n), decay=2.6, amp=0.5)
            e = min(idx + len(bt), buf_len)
            accL[idx:e] += bt[:e - idx] * 0.9
            accR[idx:e] += bt[:e - idx] * 0.7
            for beat_i in (1, 2):
                s = idx + beat_i * beat_n
                ct = sum(piano_tone(f, int(1.3 * beat_n), decay=3.2, amp=0.32) for f in chord)
                e2 = min(s + len(ct), buf_len)
                accL[s:e2] += ct[:e2 - s] * 0.65
                accR[s:e2] += ct[:e2 - s] * 0.85

            # 멜로디 — 마디 내 누적 박자 위치에 배치, 살짝 휴머나이즈(타이밍 지터)
            beat_pos = 0.0
            for freq, beats in melody:
                s = idx + int(beat_pos * beat_n) + int(rng.uniform(-0.01, 0.01) * SR)
                note = piano_tone(freq, int((beats + 1.3) * beat_n),
                                   decay=2.0 / max(beats, 1) + 0.6, amp=0.85)
                e3 = min(s + len(note), buf_len)
                melL[s:e3] += note[:e3 - s]
                melR[s:e3] += note[:e3 - s]
                beat_pos += beats

            idx += bar_beats * beat_n

    melL, melR = echo_stereo(melL, melR, delay_s=0.3, decay=0.28, taps=3, cross=0.35, r_offset_s=0.05)
    bufL = 0.6 * accL[:total] + 0.9 * melL[:total]
    bufR = 0.6 * accR[:total] + 0.9 * melR[:total]

    # 이음매 없는 루프
    F = int(FADE * SR)
    ramp = np.linspace(0, 1, F)
    for buf in (bufL, bufR):
        head, tail = buf[:F].copy(), buf[-F:].copy()
        buf[:F] = head * ramp + tail * (1 - ramp)
    loopL = bufL[:len(bufL) - F]
    loopR = bufR[:len(bufR) - F]

    peak = max(np.max(np.abs(loopL)), np.max(np.abs(loopR))) or 1.0
    scale = (10 ** (-3 / 20)) / peak
    loopL = loopL * scale
    loopR = loopR * scale
    _export(name, loopL, loopR, peak)


if __name__ == "__main__":
    for preset_name, cfg in PRESETS.items():
        generate_track(preset_name, cfg["chords"], cfg["seed"])
    generate_waltz()
