"""data/bgm.mp3 생성기 (원본·로열티프리/CC0).

외부 CC0 음원 사이트(FreePD=JS 렌더링, archive.org=CC0 검색 불가)가 빌드 환경에서
안정적으로 접근되지 않아, 저작권·네트워크 의존이 전혀 없는 '원본' 배경 음악을 직접 합성한다.
구성: 따뜻한 메이저7 패드(C–Am–F–G, 스테레오 코러스) + 사이클마다 바뀌는 아르페지오 +
가끔 울리는 high shimmer + 스테레오 크로스피드 에코. 나레이션 아래 10%용.
재생성: python scripts/make_bgm.py  (출력: data/bgm.mp3, 이음매 없는 ~63초 스테레오 루프, 매번 동일)
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
RNG = np.random.default_rng(7)   # 휴머나이즈 지터 — 고정 시드로 재생성해도 결과 동일

# 코드별 음정(Hz): Cmaj7 – Am7 – Fmaj7 – G7  + 서브베이스(루트 한 옥타브 아래)
CHORDS = [
    ([261.63, 329.63, 392.00, 493.88], 130.81),  # Cmaj7
    ([220.00, 261.63, 329.63, 392.00], 110.00),   # Am7
    ([174.61, 220.00, 261.63, 329.63],  87.31),   # Fmaj7
    ([196.00, 246.94, 293.66, 349.23],  98.00),   # G7
]
# 사이클마다 다른 아르페지오 패턴(코드 톤 인덱스) — 똑같은 루프가 기계적으로 반복되지 않도록
ARP_PATTERNS = [
    [0, 1, 2, 3, 2, 1, 2, 3],
    [0, 2, 1, 3, 1, 2, 3, 2],
    [0, 1, 3, 2, 3, 1, 2, 1],
    [2, 1, 0, 1, 2, 3, 2, 3],
]


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


total = int(CHORD_SEC * len(CHORDS) * CYCLES * SR)
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
    for ci, (freqs, sub) in enumerate(CHORDS):
        # 패드(다음 코드와 1.6초 겹침) — L/R 살짝 디튠해 코러스/스테레오 폭 형성
        gL = grain_stereo(freqs, sub, CHORD_SEC + 1.6, idx, +1)
        gR = grain_stereo(freqs, sub, CHORD_SEC + 1.6, idx, -1)
        end = min(idx + len(gL), buf_len)
        padL[idx:end] += gL[:end - idx]
        padR[idx:end] += gR[:end - idx]

        # 아르페지오 (코드 톤, 한 옥타브 위) — 사이클마다 다른 패턴 + 휴머나이즈 지터
        for k, pi in enumerate(pattern):
            f = freqs[pi % len(freqs)] * 2.0
            jitter_t = int(RNG.uniform(-0.015, 0.015) * SR)
            jitter_amp = RNG.uniform(0.85, 1.0)
            s = max(0, idx + int(k * ARP_NOTE * SR) + jitter_t)
            p = pluck(f, nlen, jitter_amp)
            e = min(s + len(p), buf_len)
            # 아르페지오도 좌우로 약하게 흔들어 정적인 중앙 고정음 탈피 (짝/홀 음 교대로 살짝 좌우)
            gL, gR = (0.95, 0.55) if k % 2 == 0 else (0.55, 0.95)
            arpL[s:e] += p[:e - s] * gL
            arpR[s:e] += p[:e - s] * gR

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

pcm = np.empty(len(loopL) * 2, dtype=np.int16)
pcm[0::2] = (loopL * 32767).astype(np.int16)
pcm[1::2] = (loopR * 32767).astype(np.int16)

out = Path(__file__).parent.parent / "data" / "bgm.mp3"
out.parent.mkdir(parents=True, exist_ok=True)
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
