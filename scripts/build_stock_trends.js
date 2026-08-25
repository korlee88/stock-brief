#!/usr/bin/env node
/**
 * 구글 관심 주식 순위 조사 — Gemini + Google Search 그라운딩으로 "지금 구글에서
 * 검색 관심이 가장 높은 상장사 TOP 30"을 조사해 data/stock-trends.json 에 기록한다.
 *
 * 한국·미국 시장을 **독립된 두 번의 검색**으로 각각 조사해 합친다(사용자 요청 —
 * 한 번의 통합 검색은 "한국 개인투자자 관점" 프롬프트에 끌려 미국 쪽 결과 품질이
 * 떨어짐: 실제 미국에서 화제인 종목이 아니라 한국어 검색 맥락에 걸리는 종목 위주로
 * 나옴). 각 시장 검색은 그 시장 언어·매체 기준으로 독립 그라운딩되고, 결과를
 * 시장 내 순위 기준으로 교차 배치(interleave)하되 **미국을 한국보다 우선**한다
 * (사용자 요청 — US1,KR1,US2,KR2,... 순, 동순위면 미국이 위).
 *
 * on-demand.html 우측 '구글 관심 주식 TOP 30' 패널이 이 파일을 읽어 표시하며,
 * 갱신은 페이지의 [리셋] 버튼 → update-stock-trends.yml workflow_dispatch 로만 일어난다
 * (사용자 요청: 리셋을 누를 때만 순위를 검색해서 기록).
 *
 * 사용: GEMINI_API_KEY=... node scripts/build_stock_trends.js
 * 출력: data/stock-trends.json {generated_at, source, items:[{rank,name_ko,ticker,market,score}]}
 */
const fs = require('fs');
const path = require('path');

const API_KEY = process.env.GEMINI_API_KEY;
if (!API_KEY) { console.error('❌ GEMINI_API_KEY 없음'); process.exit(1); }

const OUT_FILE = path.join(__dirname, '..', 'data', 'stock-trends.json');
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
    } catch (err) { lastError = err; }
    if (attempt < retries) await sleep(8000 * (attempt + 1));
  }
  throw lastError;
}

function extractJsonArray(data) {
  const text = (data?.candidates?.[0]?.content?.parts || []).map(p => p.text || '').join('');
  const m = text.match(/\[[\s\S]*\]/);
  if (!m) {
    const reason = data?.candidates?.[0]?.finishReason || 'UNKNOWN';
    throw new Error(`JSON 배열 응답 없음 (finishReason=${reason}): ` + text.slice(0, 200));
  }
  return JSON.parse(m[0]);
}

const KR_PROMPT = (today) =>
`Search Google (한국어 검색, 국내 경제 뉴스·실시간 인기 검색어 위주) for KOSPI/KOSDAQ 상장 기업 중 지금(최근 며칠, ${today} KST 기준) 한국 투자자 사이에서 검색 관심이 가장 높은 종목을 조사해줘.
신호: 실시간 인기 검색 종목, 국내 경제·증권 뉴스 화제도, 급등락 이슈, 실적 발표 화제.

Return ONLY a JSON array of exactly 15 items ranked by search interest (1 = highest):
[{"rank":1,"name_ko":"회사명 한국어 (예: 삼성전자)","ticker":"005930.KS 또는 247540.KQ 형식","score":1~100 정수}]
Rules:
- 반드시 한국거래소(KOSPI/KOSDAQ) 상장사만. 중복 금지, ETF·지수 제외.
- Return ONLY the JSON array, no prose.`;

const US_PROMPT = (today) =>
`Search Google (English-language sources — US financial media, Google Trends US, Reddit/StockTwits buzz, earnings news) for US-listed (NYSE/NASDAQ) companies that are attracting the MOST search interest among US retail investors RIGHT NOW (past few days, as of ${today}).
Focus on what is genuinely trending/buzzing in the US market itself right now (earnings surprises, viral or meme-stock activity, major product/AI news, unusual price moves) — NOT just large stable blue-chip names by default.

Return ONLY a JSON array of exactly 15 items ranked by search interest (1 = highest):
[{"rank":1,"name_ko":"회사명 한국어 표기 (예: 엔비디아)","ticker":"Yahoo Finance 심볼, 접미사 없음 (예: NVDA)","score":1~100 정수}]
Rules:
- 반드시 미국 거래소(NYSE/NASDAQ) 상장사만. 중복 금지, ETF·지수 제외.
- Return ONLY the JSON array, no prose.`;

async function fetchMarketTrends(label, promptText) {
  const data = await geminiPost({
    tools: [{ google_search: {} }],
    contents: [{ role: 'user', parts: [{ text: promptText }] }],
    generationConfig: { maxOutputTokens: 8192, temperature: 0.2, thinkingConfig: { thinkingBudget: 0 } },
  });
  const items = extractJsonArray(data);
  if (!Array.isArray(items) || items.length < 5) {
    throw new Error(`${label} 순위 항목 부족 (${Array.isArray(items) ? items.length : 0}건)`);
  }
  return items
    .filter(it => it && it.name_ko)
    .map((it, i) => ({
      name_ko: String(it.name_ko).trim(),
      ticker: String(it.ticker || '').trim().toUpperCase(),
      market: label,
      score: Math.max(1, Math.min(100, Math.round(Number(it.score) || (100 - i * 3)))),
    }));
}

async function main() {
  const today = new Date(Date.now() + 9 * 3600 * 1000).toISOString().split('T')[0];
  console.log(`🔥 관심 주식 순위 조사 중 — 한국·미국 독립 검색 (Gemini + Google Search, ${today} KST)...`);

  // 한국·미국을 별도 검색으로 조사 — 한쪽이 다른 쪽 언어·매체 맥락에 끌려가지 않게.
  const [krItems, usItems] = await Promise.all([
    fetchMarketTrends('한국', KR_PROMPT(today)),
    fetchMarketTrends('미국', US_PROMPT(today)),
  ]);
  console.log(`   한국 ${krItems.length}건, 미국 ${usItems.length}건 수신`);

  // 시장 내 순위(이미 score 내림차순으로 옴) 기준으로 교차 배치하되 미국을 우선
  // 배치(US1,KR1,US2,KR2,...) — 사용자 요청: 한국보다 미국 검색 순위를 우선한다.
  const interleaved = [];
  const maxLen = Math.max(krItems.length, usItems.length);
  for (let i = 0; i < maxLen; i++) {
    if (usItems[i]) interleaved.push(usItems[i]);
    if (krItems[i]) interleaved.push(krItems[i]);
  }

  // 정리: 이름 중복 제거(양쪽에 같은 종목이 우연히 잡힌 경우 대비)·30개 컷·순위 재부여
  const seen = new Set();
  const items = interleaved
    .filter(it => !seen.has(it.name_ko) && seen.add(it.name_ko))
    .slice(0, 30)
    .map((it, i) => ({ rank: i + 1, ...it }));

  const out = {
    generated_at: new Date().toISOString(),
    source: 'Gemini + Google Search 그라운딩 — 한국·미국 독립 검색 후 미국 우선 교차 배치 (AI 추정 순위)',
    items,
  };
  fs.writeFileSync(OUT_FILE, JSON.stringify(out, null, 1) + '\n');
  console.log(`✅ data/stock-trends.json — ${items.length}종목 기록`);
  console.log('   TOP 5: ' + items.slice(0, 5).map(i => `${i.rank}.${i.name_ko}(${i.market})`).join(' '));
}

main().catch(e => { console.error('❌ 실패:', e.message); process.exit(1); });
