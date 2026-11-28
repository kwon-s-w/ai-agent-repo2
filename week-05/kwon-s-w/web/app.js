import {createPurchaseReview} from '/purchase-review.js';

const purchaseReview = createPurchaseReview({comparisonEnabled: false});
const $ = id => document.getElementById(id);
const viewLinks = Array.from(document.querySelectorAll('.view-links [data-view]'));
const panels = new Map(viewLinks.map(link => [link.dataset.view, $(`panel-${link.dataset.view}`)]));
let busy = false;
let selectedCase = '';
let selectedRun;
let displayedAnalysis;

function showView(resetScroll = false) {
  const hash = location.hash.slice(1);
  const route = hash === 'result' ? 'documents' : hash;
  const view = panels.has(route) ? route : 'lookup';
  for (const [name, panel] of panels) panel.hidden = name !== view;
  for (const link of viewLinks) {
    if (link.dataset.view === view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  if (resetScroll) window.scrollTo(0, 0);
}

showView();
window.addEventListener('hashchange', () => showView(true));
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const won=n=>n==null?'미확인':Number(n).toLocaleString('ko-KR')+'원';
const labels={status_report:'현황조사서',appraisal:'감정평가서',specification:'매각물건명세서'};
const states={saved:'저장 완료',unavailable:'미제공',failed:'실패',partial:'일부 저장',blocked:'접근 제한'};
const selectionKey = () => `${$('court').value}|${$('case').value}`;
function setBusy(value) {
  busy = value;
  for (const id of ['court', 'case', 'item', 'lookup', 'analyze', 'ocr-run']) $(id).disabled = value;
  $('collect').disabled = value || selectedCase !== selectionKey();
}

function invalidate() {
  selectedCase = '';
  $('collect').disabled = true;
  $('selection').textContent = '입력이 변경되었습니다. 사건을 다시 조회하세요.';
}

async function runJob(kind, payload, onProgress) {
  const response = await fetch(`/api/${kind}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Auction-Client': 'local-ui'},
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error);
  let job;
  do {
    await new Promise(resolve => setTimeout(resolve, 1000));
    const reply = await fetch(`/api/jobs/${data.job_id}`);
    job = await reply.json();
    if (!reply.ok || job.state === 'failed') throw Error(job.error);
    onProgress(job.progress || []);
  } while (job.state === 'running');
  return job.result;
}

async function request(kind) {
  if (busy) return;
  const payload = {court_name: $('court').value, case_number: $('case').value, item_number: Number($('item').value)};
  setBusy(true);
  $('message').textContent = '법원에서 자료를 확인하고 있습니다. 문서뷰어 저장에는 수 분이 걸릴 수 있습니다.';
  $('progress').textContent = '';
  try {
    const result = await runJob(kind, payload, steps => { $('progress').textContent = steps.join('\n'); });
    if (kind === 'lookup') {
      selectedCase = selectionKey();
      $('item').replaceChildren(...result.items.map(item => new Option(item.item_number, item.item_number)));
      $('selection').textContent = `${result.court_name} ${result.case_number} · 물건 ${result.items.length}개 확인`;
      $('message').textContent = '물건번호를 선택한 뒤 자료 수집을 누르세요.';
    } else {
      showCollection(result);
      $('message').textContent = result.termination_reason || '수집 종료';
      if (panels.get('lookup').hidden === false) location.hash = 'documents';
    }
  } catch (error) {
    $('message').textContent = error.message;
  } finally {
    setBusy(false);
  }
}

$('court').addEventListener('change', invalidate);
$('case').addEventListener('input', invalidate);
function render(collection) {
  const {download_base: base, property = {}, request} = collection;
  const documents = Object.entries(collection.documents || {}).map(([kind, doc]) => {
    const links = (doc.artifacts || []).flatMap(artifact => {
      const round = artifact.round ? `회차 ${esc(artifact.round)} ` : '';
      return ['pdf', 'html', 'json'].filter(ext => artifact[ext]).map(ext =>
        `<a target="_blank" rel="noopener" href="${esc(base + encodeURIComponent(artifact[ext]))}">${round}${ext.toUpperCase()} ↗</a>`);
    }).join('');
    const note = doc.note || doc.reason || (kind === 'status_report' ? '열람자료 변환본' : '법원 연결 원본 PDF');
    return `<article class="doc"><div><h3>${labels[kind] || esc(kind)}
      <span class="status ${esc(doc.status)}">${states[doc.status] || esc(doc.status)}</span></h3>
      <p>${esc(note)}</p></div><div>${links}</div></article>`;
  }).join('');
  const prices = (property.minimum_sale_prices || []).map(row => won(row.amount)).join(' / ') || '미확인';
  const failures = property.failed_auction_count == null ? '미확인' : `${esc(property.failed_auction_count)}회`;
  const photos = (collection.photos || []).filter(photo => photo.status === 'saved').map(photo => {
    const src = esc(base + photo.path);
    return `<a href="${src}" target="_blank" rel="noopener"><img src="${src}" alt="경매 물건 사진" loading="lazy"></a>`;
  }).join('');

  $('result').innerHTML = `<section class="card">
    <h2>${esc(property.court_name || request.court_name)} ${esc(property.case_number || request.case_number)} · 물건 ${esc(property.item_number || request.item_number)}</h2>
    <p class="small">수집 기준 ${esc(collection.finished_at)} · ${esc(collection.elapsed_seconds)}초</p>
    <div class="metrics">
      <div class="metric"><span>감정평가액</span><b>${won(property.appraisal_amount)}</b></div>
      <div class="metric"><span>공고 최저매각가격 · 회차별</span><b>${prices}</b></div>
      <div class="metric"><span>법원 제공 유찰횟수</span><b>${failures}</b></div>
    </div>
    ${documents}
    <div class="actions"><a class="button" href="${esc(collection.zip_url)}">수집 자료 ZIP 다운로드</a>
      <a target="_blank" rel="noopener" href="${esc(base + 'manifest.json')}">수집 기록 보기 ↗</a></div>
  </section><section class="card"><h2>현장 사진</h2><div class="photos">${photos || '<p>저장된 사진이 없습니다.</p>'}</div></section>`;
  $('result').classList.remove('hidden');
}

$('lookup').onclick = () => request('lookup');
$('collect').onclick = () => request('collect');
const sectionLabels = {condition:'물건 상태', rights:'권리관계', advantages:'장점', risks:'주의점'};
const toolLabels = {search_documents:'문서 검색', read_page:'페이지 읽기',
  lookup_law_article:'법령 조회', lookup_sales:'실거래가 조회', submit_report:'보고서 제출'};
const scopeLabels = {overall:'종합', rights:'권리관계', price:'가격 비교', documents:'문서'};
const runDateFormat = new Intl.DateTimeFormat('ko-KR', {timeZone: 'Asia/Seoul', dateStyle: 'medium', timeStyle: 'short'});

function runDate(result) {
  const stamp = Date.parse(result.finished_at || result.started_at);
  return Number.isFinite(stamp) ? runDateFormat.format(stamp) : '';
}

function reviewTable(entries) {
  const rows = entries.map(({label, row, evidence}) => `<tr><th scope="row">${esc(label)}<br><span class="small">${esc(row.status)}</span></th>
    <td>${esc(row.facts)}${evidence ? `<ul class="small">${evidence}</ul>` : ''}</td><td>${esc(row.effect)}</td><td>${esc(row.next_evidence || '—')}</td></tr>`).join('');
  return `<div class="review-table location-table document-check-table"><table>
    <thead><tr><th scope="col">검토 항목</th><th scope="col">확인 사실과 근거</th><th scope="col">판단에 주는 영향</th><th scope="col">다음 증빙</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function showCollection(result) {
  render(result);
  selectedRun = result;
  const request = result.request;
  $('active-case').textContent = `${request.case_number} · 물건 ${request.item_number}`;
  $('active-court').textContent = request.court_name;
  for (const id of ['documents-empty', 'purchase-empty', 'opinion-empty']) $(id).classList.add('hidden');
  for (const id of ['open-documents', 'document-actions', 'opinion']) $(id).classList.remove('hidden');
  purchaseReview.show(result.request, result.property);
  $('purchase-reviewed-facts').replaceChildren();
  $('analysis-reviewed-example').replaceChildren();
  if (result.purchase_example) {
    fetch(result.download_base + encodeURIComponent(result.purchase_example))
      .then(response => response.ok ? response.json() : Promise.reject())
      .then(example => {
        if (selectedRun !== result) return;
        purchaseReview.loadExample(example);
        const factLink = fact => `<a href="${esc(result.download_base + encodeURIComponent(fact.file))}#page=${esc(fact.page)}" target="_blank" rel="noopener">${esc(fact.label)} ${esc(fact.page)}쪽 ↗</a>`;
        const facts = example.facts.filter(fact => fact.topic !== 'market').map(fact => `<li>${esc(fact.statement)} ${factLink(fact)}</li>`);
        $('purchase-reviewed-facts').innerHTML = `<h3>원문과 직접 대조한 근거</h3><ul class="small">${facts.join('')}</ul>`;
        if (example.document_checks?.length) {
          const entries = example.document_checks.map(row => ({label: row.label, row,
            evidence: row.fact_indices.map(index => `<li>${factLink(example.facts[index])}</li>`).join('')}));
          $('analysis-reviewed-example').innerHTML = `<details class="analysis-guide"><summary>보람더하임 원문 대조 예시</summary>
            <p class="small">원문에서 직접 확인한 사례입니다.</p>${reviewTable(entries)}</details>`;
        }
      })
      .catch(() => {});
  }
  $('analysis-report').replaceChildren();
  $('analysis-message').textContent = '';
  $('analysis-latest-attempt').replaceChildren();
  $('analysis-latest-attempt').classList.add('hidden');
  $('ocr-result').replaceChildren();
  $('ocr-message').textContent = '';
  $('ocr-pages').value = '';
  displayedAnalysis = null;
  const hasPdf = Object.values(result.documents || {}).some(doc => doc.artifacts?.some(file => file.pdf));
  const hasAppraisal = result.documents?.appraisal?.artifacts?.some(file => file.pdf);
  $('analysis').classList.toggle('hidden', !hasPdf);
  $('analysis-empty').classList.toggle('hidden', hasPdf);
  $('ocr-controls').classList.toggle('hidden', !hasAppraisal);
  $('analysis-case').textContent = `${request.court_name} ${request.case_number} · 물건 ${request.item_number}`;
  if (hasPdf && result.analysis_file) {
    const files = [...new Set([result.completed_analysis_file, result.analysis_file].filter(Boolean))];
    Promise.allSettled(files.map(filename => fetchRunFile(result, filename))).then(responses => {
      if (selectedRun !== result || busy || displayedAnalysis) return;
      for (const response of responses) {
        if (response.status === 'fulfilled') showAnalysisResult(response.value, result.download_base);
      }
    });
  }
  if (hasAppraisal && result.ocr_file) fetchRunFile(result, result.ocr_file).then(items => {
    if (selectedRun === result) renderOcr(items, result.download_base);
  }).catch(() => {});
}

async function fetchRunFile(run, filename) {
  const response = await fetch(run.download_base + encodeURIComponent(filename));
  if (!response.ok) throw Error('저장된 자료를 읽지 못했습니다.');
  return response.json();
}

function showAnalysisResult(result, base) {
  const message = $('analysis-latest-attempt');
  const complete = result.state === 'complete';
  message.classList.toggle('hidden', complete);
  if (!complete) {
    const link = document.createElement('a');
    link.textContent = '이번 실행 기록';
    link.href = base + encodeURIComponent(result.file);
    const status = displayedAnalysis ? ' 아래에는 이전에 저장된 완료 보고서를 표시합니다.' : '';
    const attempt = [runDate(result), result.model].filter(Boolean).join(' · ');
    message.replaceChildren(`최근 실행 미완료 · ${attempt} · ${result.stop_reason}${status} `, link);
  }
  if (complete || !displayedAnalysis) renderAnalysis(result, base);
  if (complete) displayedAnalysis = result;
}

function renderOcr(items, base) {
  const rows = Object.entries(items).flatMap(([filename, data]) => Object.entries(data.pages || {}).map(([page, excerpts]) => {
    const link = `${base + encodeURIComponent(filename)}#page=${page}`;
    return `<li><a href="${esc(link)}" target="_blank" rel="noopener">PDF ${esc(page)}쪽 ↗</a>
      ${excerpts ? `<pre>${esc(excerpts)}</pre>` : ' · 검토용 발췌를 찾지 못했습니다.'}</li>`;
  }));
  $('ocr-result').innerHTML = rows.length ? `<details><summary>저장된 OCR 발췌</summary><ul class="small">${rows.join('')}</ul></details>` : '';
}

function renderAnalysis(result, base) {
  const report = result.report;
  function references(finding) {
    const documentLinks = finding.evidence.map(citation => {
      const document = result.documents[citation.document_id];
      const link = base + encodeURIComponent(document.file) + '#page=' + citation.page;
      const round = document.round == null ? '' : ` ${document.round}회차`;
      const origin = document.reviewed_pages?.includes(citation.page) ? ' · 원문 대조 발췌'
        : document.ocr_pages?.includes(citation.page) ? ' · OCR 발췌' : '';
      return `<li><a href="${esc(link)}" target="_blank" rel="noopener">${esc(document.name + round)} ${citation.page}쪽 ↗</a>${origin} · ${esc(citation.quote)}</li>`;
    });
    const sourceLinks = (finding.external_ids || []).map(id => {
      const source = result.external_sources?.[id];
      if (!source) return '';
      const context = source.kind === 'law'
        ? (source.effective_date ? `시행일 ${source.effective_date}` : '')
        : `조회 ${source.page}쪽 · 후보 ${source.matched_on_page}건`;
      return `<li><a href="${esc(source.url)}" target="_blank" rel="noopener">${esc(source.label)} ↗</a> · ${esc(source.provider)} ${esc(context)}</li>`;
    });
    return [...documentLinks, ...sourceLinks].join('');
  }
  const opinion = report?.assessment;
  const relevantEvidence = report ? [...report.rights, ...report.risks].slice(0, 4) : [];
  if (opinion) {
    purchaseReview.setDocumentReport(opinion, `<h3>Agent가 정리한 권리와 상태</h3>
      <p>${esc(opinion.statement)}</p><ul class="small">${references(opinion)}</ul>
      ${relevantEvidence.map(finding => `<p>${esc(finding.statement)}</p><ul class="small">${references(finding)}</ul>`).join('')}
      <p class="small"><a href="#analysis">문서 분석 전체 보기</a></p>`);
  }
  const assessment = opinion ? `<h3>${esc(scopeLabels[opinion.scope])} 의견 · ${esc(opinion.verdict)}</h3>
    <p>${esc(opinion.statement)}</p><ul class="small">${references(opinion)}</ul>` : '';
  const checked = report?.document_checks && result.document_check_labels
    ? `<h3>문서 교차검토표</h3>${reviewTable(Object.entries(result.document_check_labels).map(([key, label]) => {
        const row = report.document_checks[key];
        return {label, row, evidence: references(row)};
      }))}` : '';
  const findings = report ? Object.entries(sectionLabels).map(([key, label]) => {
    const items = report[key].map(finding => {
      const basis = finding.basis === 'documented' ? '자료 기재' : '근거에 따른 해석';
      return `<article class="doc"><div><span class="small">${basis}</span><p>${esc(finding.statement)}</p><ul class="small">${references(finding)}</ul></div></article>`;
    }).join('');
    return `<h3>${label}</h3>${items || '<p class="small">인용할 근거를 확보하지 못했습니다.</p>'}`;
  }).join('') : '';
  const unknowns = [...(report?.unknowns || []), ...result.limitations.filter(item =>
    item.kind !== 'scan_coverage' && item.reason !== 'PDF 텍스트를 추출하지 못했습니다.')];
  const checks = unknowns.map(item => `<li><strong>${esc(item.item)}</strong><p class="small">${esc(item.reason)} ${esc(item.check)}</p></li>`).join('');
  const coverage = Object.entries(result.read_pages).map(([id, pages]) => {
    const document = result.documents[id];
    const read = pages.length ? `Agent 열람 ${pages.join(', ')}쪽` : '이번 실행에서 열린 쪽 없음';
    const reviewed = document.reviewed_pages?.length ? ` · 원문 대조 ${document.reviewed_pages.join(', ')}쪽` : '';
    const ocr = document.ocr_pages?.length ? ` · OCR 발췌 사용 가능 ${document.ocr_pages.join(', ')}쪽` : '';
    const scan = document.unreadable_pages?.length ? ` · 텍스트 없는 쪽 ${document.unreadable_pages.length}개` : '';
    return `<li>${esc(document.name)} · 전체 ${document.pages}쪽 · ${esc(read + reviewed + ocr + scan)}</li>`;
  }).join('');
  const steps = result.tool_calls.map(step => `<li>
    <strong>${esc(toolLabels[step.tool] || step.tool)}</strong>
    <pre>입력 ${esc(JSON.stringify(step.input))}\n결과 ${esc(JSON.stringify(step.result))}</pre></li>`).join('');
  const exit = result.state === 'complete' ? '분석 완료' : '분석 미완료';
  const heading = result.state === 'complete' ? '마지막 완료 Gemini 보고서' : '미완료 실행 기록';
  $('analysis-report').innerHTML = `<h2 class="analysis-guide">${heading}</h2>
    <p class="small">${esc(runDate(result))} · ${exit} · ${esc(result.model)} · 모델 호출 ${result.model_calls}회 · ${result.elapsed_seconds}초<br>${esc(result.stop_reason)}</p>
    <p class="small">질문 ${esc(result.request)}</p>${assessment}${checked}${findings}
    <h3>확인하지 못한 사항</h3><ul>${checks || '<li>문서 분석 결과를 확보하지 못했습니다.</li>'}</ul>
    <h3>문서별 읽은 범위</h3><ul class="small">${coverage}</ul>
    <details><summary>도구 실행 기록</summary><ol>${steps}</ol></details>
    <p class="small"><a href="${esc(base + encodeURIComponent(result.file))}" download>분석 기록 JSON 다운로드</a></p>`;
}

$('analyze').onclick = async () => {
  if (busy || !selectedRun) return;
  const run = selectedRun;
  setBusy(true);
  $('analysis-message').textContent = '저장된 PDF에서 분석 근거를 찾고 있습니다.';
  try {
    const result = await runJob('analyze', {
      run_id: run.run_id, question: $('analysis-question').value, send_to_gemini: true,
    }, steps => {
      $('analysis-message').textContent = steps.at(-1) || '분석 진행 중';
    });
    showAnalysisResult(result, run.download_base);
    $('analysis-message').textContent = '';
  } catch (error) {
    $('analysis-message').textContent = error.message;
  } finally {
    setBusy(false);
  }
};

$('ocr-form').onsubmit = async event => {
  event.preventDefault();
  if (busy || !selectedRun) return;
  const run = selectedRun;
  const values = $('ocr-pages').value.trim().split(/[\s,]+/);
  if (values.length > 8 || values.some(value => !/^[1-9]\d{0,5}$/.test(value))) {
    $('ocr-message').textContent = '양의 쪽 번호를 쉼표로 구분해 최대 8개 입력하세요.';
    return;
  }
  setBusy(true);
  $('ocr-message').textContent = '선택한 PDF 쪽을 준비하고 있습니다.';
  try {
    await runJob('ocr', {run_id: run.run_id, pages: values.map(Number)}, steps => {
      $('ocr-message').textContent = steps.at(-1) || 'OCR 진행 중';
    });
    renderOcr(await fetchRunFile(run, run.ocr_file || 'ocr-excerpts.json'), run.download_base);
    $('ocr-message').textContent = '로컬 OCR을 완료했습니다. 발췌는 다음 AI 분석에서 사용할 수 있습니다.';
  } catch (error) {
    $('ocr-message').textContent = error.message;
  } finally {
    setBusy(false);
  }
};
fetch('/api/courts').then(response => response.json()).then(rows => {
  $('court').replaceChildren(...rows.map(court => new Option(court.name)));
}).catch(() => { $('message').textContent = '법원 목록을 불러오지 못했습니다.'; });

fetch('/api/latest').then(response => response.json()).then(collection => {
  if (!collection || selectedRun) return;
  showCollection(collection);
  $('message').textContent = '이전에 수집한 최근 결과를 불러왔습니다.';
}).catch(() => { $('message').textContent = '최근 수집 결과를 불러오지 못했습니다.'; });
