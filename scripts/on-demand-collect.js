#!/usr/bin/env node
/**
 * 온디맨드 뉴스 수집·분석 — 요청 시점에 해당 종목 최근 뉴스를 검색·분석해
 * weekly_video_prep.py 가 읽는 세션 스키마(auto-sessions 항목 호환)로 저장한다.
 *
 * 기존 auto-analysis.js(매일 크론·26규칙 채점·백테스트 연동)와 달리:
 *   - 규칙 엔진·buyIndex·scoringLayers 없음 (뉴스 전용 — 뉴스별 방향/영향도만 AI 분석)
 *   - 세션 누적 없음 (요청마다 단일 세션 파일 새로 작성)
 *   - 주가는 Yahoo 일봉에서 현재가 스냅숏만
 *
 * 사용: TICKER=TSLA TICKER_CONFIG=configs/TSLA/ticker.json GEMINI_API_KEY=... \
 *        node scripts/on-demand-collect.js
 * 출력: data/on-demand/<TICKER>/session.json  (prep.py SESSIONS_FILE env로 지정)
 */
const fs = require('fs');
const path = require('path');

const API_KEY = process.env.GEMINI_API_KEY;
const CFG_PATH = process.env.TICKER_CONFIG;
if (!API_KEY) { console.error('❌ GEMINI_API_KEY 없음'); process.exit(1); }
if (!CFG_PATH || !fs.existsSync(CFG_PATH)) {
  console.error(`❌ TICKER_CONFIG 경로 없음: ${CFG_PATH}`); process.exit(1);
}
const cfg = JSON.parse(fs.readFileSync(CFG_PATH, 'utf-8'));
const TICKER = cfg.ticker;
const OUT_DIR = path.join(__dirname, '..', 'data', 'on-demand', TICKER);
const OUT_FILE = path.join(OUT_DIR, 'session.json');
const NEWS_DAYS = 3;   // 요청 시점 기준 최근 N일 뉴스

const MODELS = ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-2.0-flash-lite'];
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function geminiPost(body, retries = 6) {
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
    if (attempt < retries) {
      console.warn(`   ⏳ 재시도 ${attempt + 1}/${retries}...`);
      await sleep(10000 * (attempt + 1));
    }
  }
  throw lastError;
}

function extractJsonArray(data) {
  const text = (data?.candidates?.[0]?.content?.parts || []).map(p => p.text || '').join('');
  const m = text.match(/\[[\s\S]*\]/);
  if (!m) throw new Error('JSON 배열 응답 없음: ' + text.slice(0, 200));
  return JSON.parse(m[0]);
}

async function fetchLatestPrice() {
  for (const host of ['query1', 'query2']) {
    try {
      const res = await fetch(
        `https://${host}.finance.yahoo.com/v8/finance/chart/${TICKER}?range=5d&interval=1d`,
        { headers: { 'User-Agent': 'Mozilla/5.0' } });
      if (!res.ok) continue;
      const j = await res.json();
      const r = j?.chart?.result?.[0];
      const closes = (r?.indicators?.quote?.[0]?.close || []).filter(c => c != null);
      if (closes.length) return Math.round(closes[closes.length - 1] * 100) / 100;
    } catch (e) { /* 다음 호스트 */ }
  }
  return null;   // 가격 없이도 파이프라인 계속 (씬0 가격 표기만 생략)
}

async function collectAndAnalyze() {
  const today = new Date().toISOString().split('T')[0];
  const keyPeople = (cfg.key_people || []).join(' and ');
  const data = await geminiPost({
    tools: [{ google_search: {} }],
    contents: [{
      role: 'user',
      parts: [{ text:
`[필수 규칙] title·summary·reasoning은 반드시 한국어(Korean)로 작성. source·category만 영어 유지.

Search for the latest ${cfg.company_en} (${TICKER})${keyPeople ? ` and ${keyPeople}` : ''} news from the past ${NEWS_DAYS} days that could impact ${TICKER} stock.
Only include articles from major financial/tech news outlets (Reuters, Bloomberg, CNBC, WSJ, FT, AP, MarketWatch, Barron's, Seeking Alpha, TechCrunch, Forbes, CNN Business, Fox Business 등).
Return ONLY a JSON array of the 8~10 most market-impactful items, strictly no duplicates, each from a different event.
Each item must include your own analysis of the stock-price impact:
[{"id":1,
  "title":"(한국어 번역 제목)",
  "summary":"(한국어 1~2문장 요약)",
  "source":"Reuters",
  "date":"YYYY-MM-DD (기사 날짜, 오늘은 ${today})",
  "category":"Earnings|Product|Competition|Regulatory|Macro|Contract|Market|Legal",
  "direction":"bullish|bearish|neutral",
  "impact_score": -5~5 정수 (bearish는 음수, bullish는 양수, neutral은 0 근처),
  "reasoning":"(한국어 1~2문장 — 왜 주가에 그 방향으로 작용하는지)"}]
⚠️ title·summary·reasoning에 영어 사용 금지. Return ONLY the JSON array.` }],
    }],
    generationConfig: { maxOutputTokens: 8192, temperature: 0.1, thinkingConfig: { thinkingBudget: 0 } },
  });
  return extractJsonArray(data);
}

async function main() {
  console.log(`📰 ${cfg.company_ko}(${TICKER}) 온디맨드 뉴스 수집·분석 (최근 ${NEWS_DAYS}일)...`);
  const [items, latestPrice] = await Promise.all([collectAndAnalyze(), fetchLatestPrice()]);
  if (!Array.isArray(items) || items.length === 0) {
    console.error('❌ 뉴스 0건 — 영상 생성 중단');
    process.exit(1);
  }

  // prep.py summarize()가 읽는 세션 스키마로 변환 (news 배열 + analyses 맵)
  const news = items.map((it, i) => ({
    id: it.id ?? i + 1,
    title: it.title || '',
    summary: it.summary || '',
    source: it.source || '',
    date: it.date || new Date().toISOString().split('T')[0],
    category: it.category || 'Market',
  }));
  const analyses = {};
  for (const it of items) {
    analyses[String(it.id)] = {
      direction: it.direction || 'neutral',
      impact_score: Number.isFinite(it.impact_score) ? it.impact_score : 0,
      reasoning: it.reasoning || '',
    };
  }

  const kstDate = new Date(Date.now() + 9 * 3600 * 1000).toISOString().split('T')[0];
  const session = [{
    date: kstDate,
    generatedAt: new Date().toISOString(),
    mode: 'on-demand',
    latestPrice,
    news,
    analyses,
    dailyForecasts: [],   // 뉴스 전용 모드 — 가격 예측 없음 (씬2는 일정·관전 포인트 중심)
  }];

  fs.mkdirSync(OUT_DIR, { recursive: true });
  fs.writeFileSync(OUT_FILE, JSON.stringify(session, null, 2) + '\n');
  const dirs = { bullish: 0, bearish: 0, neutral: 0 };
  for (const a of Object.values(analyses)) dirs[a.direction] = (dirs[a.direction] || 0) + 1;
  console.log(`✅ 뉴스 ${news.length}건 (호재 ${dirs.bullish} · 악재 ${dirs.bearish} · 보합 ${dirs.neutral})` +
              (latestPrice ? ` · 현재가 $${latestPrice}` : ' · 가격 조회 실패(생략)'));
  console.log(`   저장: data/on-demand/${TICKER}/session.json`);
}

main().catch(e => { console.error('❌ 실패:', e.message); process.exit(1); });
