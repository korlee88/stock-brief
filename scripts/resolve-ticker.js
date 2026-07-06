#!/usr/bin/env node
/**
 * 온디맨드 종목 메타데이터 리졸버 — 티커만 입력하면 Gemini 구글 검색으로
 * 회사명·산업·핵심인물·경쟁사 등 프롬프트용 메타데이터를 자동 조사해
 * configs/<TICKER>/ticker.json 으로 캐시한다 (이미 있으면 재사용, 재조사 없음).
 *
 * 사용: TICKER=TSLA GEMINI_API_KEY=... node scripts/resolve-ticker.js
 * 출력: configs/<TICKER>/ticker.json (weekly_video_prep.py 등이 TICKER_CONFIG env로 읽음)
 */
const fs = require('fs');
const path = require('path');

const TICKER = (process.env.TICKER || '').trim().toUpperCase();
const API_KEY = process.env.GEMINI_API_KEY;
if (!TICKER || !/^[A-Z0-9.\-]{1,10}$/.test(TICKER)) {
  console.error(`❌ 올바른 티커가 아닙니다: "${process.env.TICKER || ''}" (env TICKER)`);
  process.exit(1);
}

const OUT_DIR  = path.join(__dirname, '..', 'configs', TICKER);
const OUT_FILE = path.join(OUT_DIR, 'ticker.json');

if (fs.existsSync(OUT_FILE)) {
  console.log(`✅ 캐시된 종목 config 재사용: configs/${TICKER}/ticker.json`);
  process.exit(0);
}
if (!API_KEY) {
  console.error('❌ GEMINI_API_KEY 없음 — 신규 종목 메타데이터 조사 불가');
  process.exit(1);
}

const MODELS = ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-2.0-flash-lite'];
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function geminiPost(body, retries = 5) {
  let lastError;
  for (let attempt = 0; attempt <= retries; attempt++) {
    const model = MODELS[Math.min(Math.floor(attempt / 2), MODELS.length - 1)];
    try {
      const res = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${API_KEY}`,
        { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
      if (res.ok) return res.json();
      const e = await res.json().catch(() => ({}));
      const msg = e?.error?.message || `HTTP ${res.status}`;
      if (![429, 500, 503, 529].includes(res.status)) throw new Error(msg);
      lastError = new Error(msg);
    } catch (err) {
      lastError = err;
    }
    if (attempt < retries) await sleep(8000 * (attempt + 1));
  }
  throw lastError;
}

function extractJson(data) {
  const text = (data?.candidates?.[0]?.content?.parts || []).map(p => p.text || '').join('');
  const m = text.match(/\{[\s\S]*\}/);
  if (!m) throw new Error('JSON 응답 없음: ' + text.slice(0, 200));
  return JSON.parse(m[0]);
}

async function main() {
  console.log(`🔎 ${TICKER} 종목 메타데이터 조사 중 (Gemini + Google Search)...`);
  const data = await geminiPost({
    tools: [{ google_search: {} }],
    contents: [{
      role: 'user',
      parts: [{ text:
`Search the web and identify the publicly traded company with stock ticker "${TICKER}" (US listing preferred).
Return ONLY a JSON object, no other text:
{
 "ticker": "${TICKER}",
 "company_en": "official company name in English",
 "company_ko": "회사명 한국어 표기 (통용 표기, 예: 테슬라/로켓랩)",
 "industry_ko": "산업 분류 한국어 (예: 전기차, 반도체, 우주·발사체)",
 "exchange": "상장 거래소 (NASDAQ/NYSE 등, 비상장이면 \\"비상장\\")",
 "key_people": ["CEO 등 핵심 인물 영문명 1~2명"],
 "competitor_ticker": "대표 경쟁사 상장 티커 1개 (없으면 \\"\\")",
 "google_trends_keywords": ["한국어 연관 검색 키워드 4~6개 (회사명·제품·CEO 등)"],
 "video_tags": ["한국어/영문 YouTube 태그 4~5개"],
 "image_future_tech_en": "the company's flagship future products/roadmap as a vivid 1-2 sentence English visual description for AI image generation"
}
If the ticker does not correspond to any real listed company, return {"error": "설명"}.` }],
    }],
    generationConfig: { maxOutputTokens: 2048, temperature: 0.1 },
  });

  const meta = extractJson(data);
  if (meta.error) {
    console.error(`❌ 종목 식별 실패: ${meta.error}`);
    process.exit(1);
  }
  if (!meta.company_ko || !meta.company_en) {
    console.error('❌ 필수 필드(company_ko/en) 누락 — 응답:', JSON.stringify(meta).slice(0, 300));
    process.exit(1);
  }

  // 파이프라인이 기대하는 나머지 필드 채움 (온디맨드 뉴스 전용 모드 기본값)
  const cfg = {
    ticker: TICKER,
    company_en: meta.company_en,
    company_ko: meta.company_ko,
    industry_ko: meta.industry_ko || '',
    exchange: meta.exchange || '',
    image_future_tech_en: meta.image_future_tech_en || '',
    brand_label: `${TICKER} BRIEF`,
    repo: process.env.GITHUB_REPOSITORY || '',
    data_source: 'yahoo',
    competitor_ticker: meta.competitor_ticker || '',
    key_people: meta.key_people || [],
    google_trends_keywords: meta.google_trends_keywords || [meta.company_ko, TICKER],
    video_tags: meta.video_tags || [meta.company_ko, TICKER, '주식', 'Shorts'],
    resolved_by: 'resolve-ticker.js (Gemini google_search)',
    resolved_at: new Date().toISOString(),
  };

  fs.mkdirSync(OUT_DIR, { recursive: true });
  fs.writeFileSync(OUT_FILE, JSON.stringify(cfg, null, 2) + '\n');
  console.log(`✅ configs/${TICKER}/ticker.json 생성 — ${cfg.company_ko}(${cfg.company_en}) · ${cfg.industry_ko} · ${cfg.exchange}`);
}

main().catch(e => { console.error('❌ 실패:', e.message); process.exit(1); });
