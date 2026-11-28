const costLabels = {
  acquisition: '취득·등기', repair: '수리', handover: '인도·권리 인수', other: '초기 금융비용·기타·예비비',
};
const checkLabels = {registry: '최신 등기', occupancy: '현재 점유·인도', liabilities: '낙찰자 인수 부담'};
const checkSources = {
  registry: '최신 등기사항증명서에서 설정과 말소를 확인한 뒤 매수 조건에 반영합니다.',
  occupancy: '현황조사서는 조사 당시 자료입니다. 현재 점유와 인도 가능 시점은 추가 확인이 필요합니다.',
  liabilities: '최신 등기와 해당 임대차·배당 자료를 함께 확인해야 실제 인수 부담을 판단할 수 있습니다.',
};
const bids = ['bid1', 'bid2', 'bid3'];
const tradeFields = ['date', 'price', 'area', 'source'];
const maxTrades = 20;

function money(value, minimum = 1) {
  if (value == null || String(value).trim() === '') return null;
  const amount = Number(value);
  return Number.isSafeInteger(amount) && amount >= minimum && amount <= 100000000 ? amount : null;
}

export function calculatePurchase(inputs, trades, {minimum = null, today = new Date()} = {}) {
  const missingCosts = Object.keys(costLabels).filter(key => money(inputs[key], 0) === null);
  const knownCosts = Object.keys(costLabels).reduce((sum, key) => sum + (money(inputs[key], 0) ?? 0), 0);
  const budget = money(inputs.budget);
  const lastDay = Date.parse(today.toLocaleDateString('sv-SE', {timeZone: 'Asia/Seoul'}));
  const cutoff = lastDay - 365 * 86400000;
  const valid = trades.filter(trade => /^\d{4}-\d{2}-\d{2}$/.test(trade.date) && Date.parse(trade.date) <= lastDay
    && trade.source?.trim() && Number(trade.area) > 0 && money(trade.price) !== null);
  const recent = valid.filter(trade => Date.parse(trade.date) >= cutoff);
  const used = recent.length ? recent : valid;
  const prices = used.map(trade => money(trade.price)).sort((a, b) => a - b);
  const middle = Math.floor(prices.length / 2);
  const median = prices.length ? (prices[Math.floor((prices.length - 1) / 2)] + prices[middle]) / 2 : null;
  return {
    missingCosts, knownCosts, median,
    priceRange: prices.length ? [prices[0], prices.at(-1)] : null,
    tradeCount: valid.length, recentCount: recent.length, minimum,
    bidLimit: budget !== null && !missingCosts.length ? budget - knownCosts : null,
    checks: Object.keys(checkLabels).filter(key => inputs[key] !== 'reviewed')
      .map(key => `${checkLabels[key]} ${inputs[key] === 'issue' ? '쟁점 있음' : '미확인'}`),
    scenarios: bids.flatMap((key, index) => {
      const bid = money(inputs[key]);
      if (bid === null) return [];
      const subtotal = bid + knownCosts;
      return [{number: index + 1, bid, subtotal, meetsMinimum: minimum === null || bid * 10000 >= minimum,
        budgetLeft: budget !== null && !missingCosts.length ? budget - subtotal : null,
        comparableGap: median !== null && !missingCosts.length ? median - subtotal : null}];
    }),
  };
}

export function purchaseOpinion(result, documentReport, {comparisonEnabled = true} = {}) {
  const blockers = [];
  const add = (item, source) => blockers.push({item, source});
  if (!documentReport) add('문서 근거 확보', 'AI 문서 분석에서 읽은 PDF 쪽과 발췌를 확인합니다. 필요한 스캔 쪽은 로컬 OCR로 읽을 수 있습니다.');
  else if (documentReport.verdict === '판단 보류' || documentReport.verdict === '주의 필요') {
    add('문서에 남은 쟁점 확인', '아래 원문 근거와 AI 문서 분석의 미확인 사항에서 필요한 자료를 확인합니다.');
  }
  if (comparisonEnabled) {
    if (result.minimum === null) add('해당 회차 공고 최저매각가 확인', '법원 수집 자료에 나온 최저가를 사용합니다. 여러 값이 있으면 현재 회차를 원문에서 대조합니다.');
    if (!result.recentCount) add('최근 365일 내 비교 가능한 거래 확인', '실거래가 API로 조회할 수 있고, 매수 조건에 공개 거래를 직접 입력할 수도 있습니다.');
    if (result.missingCosts.length) add('미확인 부대비용 입력', '지금 확보한 비용으로 부분합을 계산합니다. 확인한 비용과 견적을 입력하면 총비용을 다시 계산합니다.');
    if (result.bidLimit === null && !result.missingCosts.length) add('총매수 예산 입력', '매수 조건에 입력한 예산과 예상 총비용을 비교합니다.');
    if (!result.scenarios.length) add('입찰가 입력', '매수 조건에 검토할 입찰가를 입력합니다.');
  }
  for (const check of result.checks) {
    const key = Object.keys(checkLabels).find(key => check.startsWith(checkLabels[key]));
    if (key) add(check, checkSources[key]);
  }
  if (!comparisonEnabled) {
    return {blockers, candidates: [], verdict: blockers.length ? '판단 보류' : '서류 검토 가능'};
  }
  const candidates = result.scenarios.filter(scenario => result.minimum !== null && scenario.meetsMinimum
    && scenario.budgetLeft !== null && scenario.budgetLeft >= 0 && scenario.comparableGap > 0).map(scenario => scenario.number);
  return {blockers, candidates, verdict: blockers.length ? '판단 보류' : candidates.length ? '조건 충족 시 추가 검토' : '가격 조건 재검토'};
}

const format = new Intl.NumberFormat('ko-KR', {maximumFractionDigits: 1});
const amount = value => value === null ? '—' : `${format.format(value)}만원`;

function reviewSteps(result, report, opinion, comparisonEnabled) {
  const rights = result.checks.length ? result.checks.join(' · ') : '세 항목 모두 자료 확인으로 입력됨';
  const verdict = ['판단 보류', '주의 필요', '검토 가능'].find(value => value === report?.verdict);
  const documents = verdict ? `문서 의견 ${verdict}. ${rights}.` : `문서 분석 또는 원문 대조가 필요합니다. ${rights}.`;
  if (!comparisonEnabled) {
    return [
      ['문서와 권리 확인', documents],
      ['서류 검토 의견', opinion.blockers.length ? '확인할 증빙이 남아 서류 판단을 보류합니다.' : '입력한 확인 상태를 바탕으로 서류 검토가 가능합니다.'],
    ];
  }
  const trades = result.recentCount
    ? `입력 거래 ${result.tradeCount}건 중 최근 365일 거래 ${result.recentCount}건을 가격 비교에 사용했습니다.`
    : result.tradeCount ? `입력 거래 ${result.tradeCount}건 모두 365일을 넘었습니다. 과거 가격은 참고값으로 표시합니다.`
      : '비교할 거래의 날짜, 가격, 면적과 출처가 필요합니다.';
  const minimum = result.minimum === null ? '해당 회차 공고 최저가를 확인해야 합니다.'
    : `공고 최저가 ${amount(result.minimum / 10000)}를 적용했습니다.`;
  const costs = result.missingCosts.length ? '미확인 비용이 있어 입력된 부분합을 표시합니다.'
    : `각 입찰가에 입력한 비용 ${amount(result.knownCosts)}를 더해 예산 잔액을 계산합니다.`;
  const conclusion = opinion.blockers.length ? '아래 확인 사항이 남아 종합 의견을 보류합니다.'
    : opinion.candidates.length ? '공고가·예산과 최근 거래 비교 조건을 충족하는 안이 있습니다.'
      : '입찰가와 비용을 비교한 결과 금액 조건을 함께 충족하는 안이 없습니다.';
  return [
    ['문서와 권리 확인', documents],
    ['비교 거래의 시점 확인', trades],
    ['공고가와 총비용 계산', `${minimum} ${costs}`],
    ['종합 의견 결정', conclusion],
  ];
}

export function createPurchaseReview({comparisonEnabled = true} = {}) {
  const panel = document.getElementById('purchase');
  const form = document.getElementById('purchase-form');
  const list = document.getElementById('comparisons');
  const add = document.getElementById('add-comparison');
  const summary = document.getElementById('purchase-summary');
  const storageMessage = document.getElementById('purchase-storage');
  const evidence = document.getElementById('purchase-evidence');
  document.getElementById('comparison-inputs').hidden = !comparisonEnabled;
  let storageKey;
  let minimum;
  let documentReport;
  let reviewedDocuments;
  let hasEdits;

  function applyInputs({inputs, trades}) {
    for (const field of form.elements) {
      if (field.name && typeof inputs?.[field.name] === 'string') field.value = inputs[field.name];
    }
    if (Array.isArray(trades)) trades.slice(0, maxTrades).forEach(trade => addTrade(trade));
  }

  function refresh(save) {
    const inputs = Object.fromEntries(new FormData(form));
    const trades = Array.from(list.children, row => Object.fromEntries(tradeFields.map(field => [
      field, row.querySelector(`[data-field="${field}"]`).value,
    ])));
    const result = calculatePurchase(inputs, comparisonEnabled ? trades : [], {minimum});
    const report = documentReport ?? reviewedDocuments;
    const opinion = purchaseOpinion(result, report, {comparisonEnabled});
    add.disabled = trades.length >= maxTrades;
    const comparison = result.tradeCount ? `비교 거래 ${result.tradeCount}건 · ${result.recentCount ? `최근 거래 ${result.recentCount}건` : '과거 거래만 확인'} · 중앙값 ${amount(result.median)} · 범위 ${result.priceRange.map(amount).join(' ~ ')}`
      : '날짜·가격·면적·출처를 입력한 비교 거래가 없습니다.';
    const incompleteTrades = trades.length - result.tradeCount;
    const costStatus = result.missingCosts.length
      ? `비용 미확인 · ${result.missingCosts.map(key => costLabels[key]).join(', ')}` : '';
    const limit = result.bidLimit === null ? '' : result.bidLimit > 0
      ? `입력 비용을 뺀 예산상 입찰가 상한 ${amount(result.bidLimit)}` : '입력 비용을 빼면 입찰 가능한 예산이 남지 않습니다.';
    const priceStatus = result.scenarios.length && result.recentCount && result.minimum !== null && result.bidLimit !== null
      ? (opinion.candidates.length
        ? `공고가와 예산 조건을 충족하고, 총비용이 최근 거래 중앙값보다 낮은 안은 ${opinion.candidates.map(number => `${number}안`).join(', ')}입니다.`
        : '입력한 입찰가 중 금액 조건을 모두 충족하는 안이 없습니다.') : '';
    const rows = result.scenarios.map(scenario => `<tr><th scope="row">${scenario.number}</th>
      <td>${amount(scenario.bid)}${scenario.meetsMinimum ? '' : ' · 공고가 미만'}</td><td>${amount(scenario.subtotal)}</td>
      <td>${amount(scenario.budgetLeft)}</td>${comparisonEnabled ? `<td>${amount(scenario.comparableGap)}</td>` : ''}</tr>`).join('');
    const table = rows ? `<div class="review-table"><table><thead><tr><th scope="col">안</th><th scope="col">입찰가</th>
      <th scope="col">${costStatus ? '확인 비용만 합산' : '예상 총비용'}</th><th scope="col">예산 잔액</th>
      ${comparisonEnabled ? `<th scope="col">${result.tradeCount && !result.recentCount ? '과거 거래' : '거래'} 중앙값 − 총비용</th>` : ''}</tr></thead><tbody>${rows}</tbody></table></div>`
      : '<p class="small">입찰가를 입력하면 시나리오 금액이 표시됩니다.</p>';
    const purpose = inputs.purpose === 'rental' ? '임대 검토에는 별도로 임대료·공실·보유비용 확인이 필요합니다.'
      : '실거주 검토에는 별도로 입주 가능일과 통근·생활 조건 확인이 필요합니다.';
    const needs = opinion.blockers.map(({item, source}) => `<li><strong>${item}</strong><p class="small">${source}</p></li>`).join('');
    const steps = reviewSteps(result, report, opinion, comparisonEnabled).map(([title, detail]) => `<li><strong>${title}</strong><p class="small">${detail}</p></li>`).join('');
    const comparisonSummary = comparisonEnabled ? `<p>${comparison}</p>${incompleteTrades ? `<p class="small">비교 정보가 부족한 ${incompleteTrades}건 제외</p>` : ''}` : '';
    summary.innerHTML = `<div class="decision${opinion.blockers.length ? ' pending' : ''}"><h2>${opinion.verdict}</h2><p class="small">${comparisonEnabled ? '현재 입력과 문서 근거를 종합한 의견' : '문서와 별도 증빙 확인에 대한 의견'}</p></div>
      <h3>판정 과정</h3><ol class="review-steps">${steps}</ol>
      ${needs ? `<h3>다시 검토하기 전에 확인할 것</h3><ul class="small review-steps">${needs}</ul>` : ''}
      <h3>${comparisonEnabled ? '비용과 비교 결과' : '입력 비용 계산'}</h3>${comparisonSummary}
      ${table}${costStatus ? `<p class="small">${costStatus}</p>` : ''}
      ${limit ? `<p class="small">${limit}</p>` : ''}${priceStatus ? `<p>${priceStatus}</p>` : ''}<p class="small">${purpose}</p>`;
    if (save && storageKey) {
      hasEdits = true;
      try {
        localStorage.setItem(storageKey, JSON.stringify({inputs, trades}));
        storageMessage.textContent = '이 브라우저에 물건별로 저장했습니다.';
      } catch {
        storageMessage.textContent = '브라우저 저장을 사용할 수 없습니다. 새로고침하면 입력이 사라질 수 있습니다.';
      }
    }
  }

  function addTrade(trade = {}) {
    const row = document.createElement('div');
    row.className = 'comparison-row';
    row.innerHTML = `<label>거래일 <input data-field="date" type="date"></label>
      <label>거래가 (만원) <input data-field="price" type="number" min="1" step="1"></label>
      <label>거래 면적 (㎡) <input data-field="area" type="number" min="0.01" step="0.01"></label>
      <label>출처·층·동 <input data-field="source" maxlength="240" placeholder="국토부 실거래가 · 8층"></label>
      <button type="button" class="secondary" aria-label="이 비교 거래 삭제">삭제</button>`;
    for (const field of tradeFields) row.querySelector(`[data-field="${field}"]`).value = trade[field] ?? '';
    row.querySelector('button').onclick = () => { row.remove(); refresh(true); };
    list.append(row);
    return row;
  }

  form.addEventListener('submit', event => event.preventDefault());
  form.addEventListener('input', () => refresh(true));
  add.onclick = () => {
    if (list.children.length >= maxTrades) return;
    addTrade().querySelector('input').focus();
    refresh(true);
  };

  return {show(property, details) {
    storageKey = 'auction-purchase-review-v1:' + JSON.stringify([property.court_name, property.case_number, property.item_number]);
    const prices = [...new Set((details?.minimum_sale_prices || []).map(row => row.amount).filter(value => Number.isSafeInteger(value) && value > 0))];
    minimum = prices.length === 1 ? prices[0] : null;
    documentReport = null;
    reviewedDocuments = null;
    hasEdits = false;
    form.reset();
    list.replaceChildren();
    evidence.replaceChildren();
    document.getElementById('purchase-sample').textContent = '';
    storageMessage.textContent = '';
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey));
      if (saved) {
        applyInputs(saved);
        hasEdits = true;
        storageMessage.textContent = '이 물건의 저장된 입력을 불러왔습니다.';
      }
    } catch {
      storageMessage.textContent = '저장된 입력을 읽을 수 없습니다.';
    }
    document.getElementById('purchase-case').textContent = `${property.court_name} ${property.case_number} · 물건 ${property.item_number}`;
    panel.classList.remove('hidden');
    refresh(false);
  }, loadExample(example) {
    if (example.document_verdict === '판단 보류') reviewedDocuments = {verdict: example.document_verdict};
    if (!hasEdits) {
      applyInputs(example);
      document.getElementById('purchase-sample').textContent = example.note;
    }
    refresh(false);
  }, setDocumentReport(report, evidenceHtml) {
    documentReport = report;
    evidence.innerHTML = evidenceHtml;
    refresh(false);
  }};
}
