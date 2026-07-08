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
  if (!m) {
    // 대부분 finishReason=MAX_TOKENS 로 JSON이 닫히기 전에 잘린 경우 (thinking 토큰이 예산 소진)
    const reason = data?.candidates?.[0]?.finishReason || 'UNKNOWN';
    throw new Error(`JSON 응답 없음 (finishReason=${reason}): ` + text.slice(0, 200));
  }
  return JSON.parse(m[0]);
}

// ── 한국 티커(.KS/.KQ) 오인 방지 ─────────────────────────────────────────────
// 사례: "381620.KQ"를 검색이 케냐항공(나이로비 거래소 티커 "KQ")으로 오인해
// 완전히 다른 회사의 영상이 생성됨. 한국 티커는 로컬 전 종목 목록(data/kr-stocks.json,
// KRX에서 받아 커밋)에서 정식 회사명을 먼저 찾아 프롬프트에 명시하고,
// 결과가 한국 상장사가 아니면 캐시하지 않고 실패시킨다(불량 캐시 방지).
const IS_KR = /\.(KS|KQ)$/.test(TICKER);
const KR_MARKET = TICKER.endsWith('.KQ') ? 'KOSDAQ' : 'KOSPI';

function lookupKrName() {
  if (!IS_KR) return null;
  try {
    const list = JSON.parse(fs.readFileSync(
      path.join(__dirname, '..', 'data', 'kr-stocks.json'), 'utf8'));
    const hit = list.find(([, code]) => code === TICKER);
    return hit ? hit[0] : null;
  } catch { return null; }   // 목록 없어도 진행(프롬프트 지침만으로 방어)
}

const _norm = s => String(s || '').toLowerCase().replace(/[\s·.,()]/g, '');

async function main() {
  const krName = lookupKrName();
  if (krName) console.log(`📌 한국 종목 확인: ${TICKER} = ${krName} (${KR_MARKET}, data/kr-stocks.json)`);
  console.log(`🔎 ${TICKER} 종목 메타데이터 조사 중 (Gemini + Google Search)...`);

  const krContext = IS_KR
    ? `\nIMPORTANT: The suffix ".KS"/".KQ" means this is a SOUTH KOREAN stock on the Korea Exchange \
(.KS = KOSPI, .KQ = KOSDAQ). The numeric part is the Korean stock code. \
Do NOT confuse the suffix with other companies' ticker symbols (e.g. Kenya Airways "KQ").${
      krName ? `\nThis ticker is the ${KR_MARKET}-listed Korean company "${krName}" — research THIS company.` : ''}\n`
    : '';

  const data = await geminiPost({
    tools: [{ google_search: {} }],
    contents: [{
      role: 'user',
      parts: [{ text:
`Search the web and identify the publicly traded company with stock ticker "${TICKER}"${IS_KR ? '' : ' (US listing preferred)'}.
${krContext}Return ONLY a JSON object, no other text:
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
    // gemini-2.5-flash 는 thinking 모델 — thinkingBudget:0 으로 사고 토큰을 끄지 않으면
    // 사고 토큰이 maxOutputTokens 예산을 소진해 JSON 이 닫히기 전에 잘린다(on-demand-collect.js 와 동일 처리).
    generationConfig: { maxOutputTokens: 8192, temperature: 0.1, thinkingConfig: { thinkingBudget: 0 } },
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

  // ── 한국 티커 검증: 오답을 캐시하면 재요청마다 같은 오류가 반복되므로, 캐시 전에 확인 ──
  if (IS_KR) {
    const exch = _norm(meta.exchange);
    const exchOk = ['krx', 'kospi', 'kosdaq', 'korea', '코스피', '코스닥', '한국'].some(k => exch.includes(_norm(k)));
    if (!exchOk) {
      console.error(`❌ 한국 티커(${TICKER})인데 거래소가 "${meta.exchange}" — 다른 회사로 오인된 응답이라 캐시하지 않습니다.`);
      console.error(`   응답 회사: ${meta.company_ko}(${meta.company_en})${krName ? ` / 기대 회사: ${krName}` : ''}`);
      process.exit(1);
    }
    if (krName) {
      const a = _norm(meta.company_ko), b = _norm(krName), c = _norm(meta.company_en);
      if (!(a.includes(b) || b.includes(a) || c.includes(b))) {
        console.warn(`⚠ 회사명 불일치: 응답 "${meta.company_ko}" vs KRX 목록 "${krName}" — KRX 정식명으로 교체`);
        meta.company_ko = krName;
      }
      meta.exchange = KR_MARKET;   // KRX 목록이 확정한 시장으로 고정
    }
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
