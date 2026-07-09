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
// 검색 기간(일) — 소형주는 3일 내 뉴스가 없는 경우가 흔해 0건이면 기간을 점차 확대해 재시도
const NEWS_WINDOWS = [3, 7, 14];
const IS_KR = /\.(KS|KQ)$/.test(TICKER);   // 한국 종목: 한국 매체 포함·한국어 검색

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
  if (!m) {
    const reason = data?.candidates?.[0]?.finishReason || 'UNKNOWN';
    // 완결된 응답(STOP)인데 배열이 없음 = "관련 뉴스 없음"을 산문으로 답한 경우 → 빈 배열로 처리
    // (기간 확대 재시도 로직이 이어받는다). 잘림(MAX_TOKENS) 등은 실제 오류이므로 throw.
    if (reason === 'STOP') {
      console.warn('   ℹ 배열 없는 완결 응답(뉴스 없음으로 간주): ' + text.slice(0, 120).replace(/\n/g, ' '));
      return [];
    }
    throw new Error(`JSON 배열 응답 없음 (finishReason=${reason}): ` + text.slice(0, 200));
  }
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

async function collectAndAnalyze(newsDays) {
  // newsDays = null → 기간 무제한 '가장 최근 뉴스' 모드 (소형주 뉴스 가뭄 보강 —
  // 몇 주~몇 달 전 기사라도 가장 최근 것을 수집, 기사 날짜를 정확히 기재해 화면에 시점 표시)
  const recentMode = newsDays == null;
  const today = new Date().toISOString().split('T')[0];
  const keyPeople = (cfg.key_people || []).join(' and ');
  // 매체 화이트리스트: 한국 종목은 한국 주요 경제지·통신사가 주 취재원 (미국 매체만 허용하면 사실상 0건)
  const outlets = IS_KR
    ? '연합뉴스, 한국경제, 매일경제, 서울경제, 이데일리, 머니투데이, 조선비즈, 전자신문, 뉴시스, 헤럴드경제, 파이낸셜뉴스, Reuters, Bloomberg'
    : "Reuters, Bloomberg, CNBC, WSJ, FT, AP, MarketWatch, Barron's, Seeking Alpha, TechCrunch, Forbes, CNN Business, Fox Business";
  const searchName = IS_KR && cfg.company_ko
    ? `"${cfg.company_ko}" (${cfg.company_en}, ${TICKER})`
    : `${cfg.company_en} (${TICKER})`;
  const data = await geminiPost({
    tools: [{ google_search: {} }],
    contents: [{
      role: 'user',
      parts: [{ text:
`[필수 규칙] title·summary·reasoning은 반드시 한국어(Korean)로 작성. source·category만 영어 유지.

Search for the latest ${searchName}${keyPeople ? ` and ${keyPeople}` : ''} news ${recentMode
  ? `— the MOST RECENT articles you can find about this company with NO date restriction (they may be weeks or even months old; that is OK). Always include each article's accurate publication date`
  : `from the past ${newsDays} days`} that could impact ${TICKER} stock.${
  IS_KR ? `\nThis is a South Korean listed company — search in KOREAN (e.g. "${cfg.company_ko} 주가", "${cfg.company_ko} 뉴스") as well as English.` : ''}
Only include articles from major financial/tech news outlets (${outlets} 등).
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
⚠️ title·summary·reasoning에 영어 사용 금지. Return ONLY the JSON array.
⚠️ If you find NO relevant news in the period, return exactly [] (an empty JSON array) — never explanatory prose.` }],
    }],
    generationConfig: { maxOutputTokens: 8192, temperature: 0.1, thinkingConfig: { thinkingBudget: 0 } },
  });
  return extractJsonArray(data);
}

async function main() {
  const pricePromise = fetchLatestPrice();   // 뉴스 검색과 병렬 진행

  // 뉴스 0건이면 기간을 3→7→14일로 확대 재시도 (소형주는 좁은 창에 뉴스가 없는 게 흔함)
  let items = [];
  let usedDays = NEWS_WINDOWS[0];
  for (const days of NEWS_WINDOWS) {
    console.log(`📰 ${cfg.company_ko}(${TICKER}) 온디맨드 뉴스 수집·분석 (최근 ${days}일)...`);
    items = await collectAndAnalyze(days);
    usedDays = days;
    if (Array.isArray(items) && items.length) break;
    if (days !== NEWS_WINDOWS[NEWS_WINDOWS.length - 1]) {
      console.warn(`   ⚠ 최근 ${days}일 뉴스 0건 — 검색 기간을 확대해 재시도합니다`);
    }
  }
  // 소형주 뉴스 가뭄 보강: 14일까지 넓혀도 3건 미만이면 기간 무제한 '가장 최근 뉴스'로
  // 추가 수집(제목 기준 중복 제거) — 본문이 빈약해지는 것을 막는다. 기사 날짜는 씬1 카드에 표시됨.
  const MIN_ITEMS = 3;
  if (!Array.isArray(items)) items = [];
  if (items.length < MIN_ITEMS) {
    console.log(`📰 뉴스 ${items.length}건뿐 — 기간 무제한 '가장 최근 뉴스' 보강 검색...`);
    try {
      const extra = await collectAndAnalyze(null);
      const seenTitles = new Set(items.map(it => (it.title || '').trim()));
      for (const it of (extra || [])) {
        const t = (it.title || '').trim();
        if (t && !seenTitles.has(t)) { seenTitles.add(t); items.push(it); }
      }
      console.log(`   ℹ 보강 후 총 ${items.length}건`);
    } catch (e) {
      console.warn(`   ⚠ 보강 검색 실패(수집분으로 계속): ${e.message}`);
    }
    items = items.map((it, i) => ({ ...it, id: i + 1 }));   // 병합분 id 충돌 방지(재부여)
  }
  if (items.length === 0) {
    console.error('❌ 기간 무제한 검색까지 해도 뉴스 0건 — 영상 생성 중단');
    console.error('   (뉴스 기반 브리핑이라 소재가 없으면 영상을 만들 수 없어요. 뉴스가 생긴 뒤 다시 시도해 주세요.)');
    process.exit(1);
  }
  if (usedDays !== NEWS_WINDOWS[0]) {
    console.log(`   ℹ 최근 ${usedDays}일 기준으로 수집됨 (3일 내 뉴스 없음)`);
  }
  const latestPrice = await pricePromise;

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
              (latestPrice ? ` · 현재가 ${IS_KR ? `${latestPrice.toLocaleString()}원` : `$${latestPrice}`}` : ' · 가격 조회 실패(생략)'));
  console.log(`   저장: data/on-demand/${TICKER}/session.json`);
}

main().catch(e => { console.error('❌ 실패:', e.message); process.exit(1); });
