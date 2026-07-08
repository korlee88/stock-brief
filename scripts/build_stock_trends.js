#!/usr/bin/env node
/**
 * 구글 관심 주식 순위 조사 — Gemini + Google Search 그라운딩으로 "지금 구글에서
 * 검색 관심이 가장 높은 상장사 TOP 30"을 조사해 data/stock-trends.json 에 기록한다.
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

async function main() {
  const today = new Date(Date.now() + 9 * 3600 * 1000).toISOString().split('T')[0];
  console.log(`🔥 구글 관심 주식 TOP 30 조사 중 (Gemini + Google Search, ${today} KST)...`);
  const data = await geminiPost({
    tools: [{ google_search: {} }],
    contents: [{
      role: 'user',
      parts: [{ text:
`Search Google for the stock companies (publicly listed) that are attracting the MOST search interest / attention right now (past few days, as of ${today} KST), from the perspective of Korean retail investors — include both Korean (KOSPI/KOSDAQ) and global (US) listed companies.
Signals to use: trending finance news volume, "인기 검색 종목"/"most searched stocks" lists, unusual price moves drawing attention, earnings/product buzz.

Return ONLY a JSON array of exactly 30 items ranked by estimated current Google search interest (1 = highest):
[{"rank":1,
  "name_ko":"회사명 한국어 표기 (예: 엔비디아, 삼성전자)",
  "ticker":"Yahoo Finance ticker (미국: NVDA / 한국: 005930.KS·247540.KQ 형식, 모르면 \\"\\")",
  "market":"한국|미국",
  "score": 1~100 정수 (1위=100 기준 상대 검색 관심도 추정)}]
Rules:
- 실제 상장사만, 중복 금지, ETF·지수 제외.
- 한국·미국 종목을 실제 관심도 순으로 섞어서 (강제 비율 없음).
- Return ONLY the JSON array, no prose.` }],
    }],
    generationConfig: { maxOutputTokens: 8192, temperature: 0.2, thinkingConfig: { thinkingBudget: 0 } },
  });

  let items = extractJsonArray(data);
  if (!Array.isArray(items) || items.length < 10) {
    console.error(`❌ 순위 항목 부족 (${Array.isArray(items) ? items.length : 0}건) — 기록하지 않음`);
    process.exit(1);
  }

  // 정리: 이름 필수·중복 제거·순위 재부여·score 클램프·30개 컷
  const seen = new Set();
  items = items
    .filter(it => it && it.name_ko && !seen.has(it.name_ko) && seen.add(it.name_ko))
    .slice(0, 30)
    .map((it, i) => ({
      rank: i + 1,
      name_ko: String(it.name_ko).trim(),
      ticker: String(it.ticker || '').trim().toUpperCase(),
      market: it.market === '한국' ? '한국' : '미국',
      score: Math.max(1, Math.min(100, Math.round(Number(it.score) || (100 - i * 3)))),
    }));

  const out = {
    generated_at: new Date().toISOString(),
    source: 'Gemini + Google Search 그라운딩 (AI 추정 순위)',
    items,
  };
  fs.writeFileSync(OUT_FILE, JSON.stringify(out, null, 1) + '\n');
  console.log(`✅ data/stock-trends.json — ${items.length}종목 기록`);
  console.log('   TOP 5: ' + items.slice(0, 5).map(i => `${i.rank}.${i.name_ko}`).join(' '));
}

main().catch(e => { console.error('❌ 실패:', e.message); process.exit(1); });
