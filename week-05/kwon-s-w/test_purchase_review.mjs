import assert from 'node:assert/strict';
import test from 'node:test';
import {calculatePurchase, purchaseOpinion} from './web/purchase-review.js';

const costs = {acquisition: '900', repair: '600', handover: '0', other: '500'};
const inputs = {...costs, budget: '48000', bid1: '42000', bid2: '47000'};
const trade = {date: '2026-10-01', area: '84.99', source: '국토교통부 · 8층'};
const asOf = {today: new Date('2026-11-28T00:00:00Z')};

test('user-selected comparisons and equal costs yield the expected scenario figures', () => {
  const result = calculatePurchase(inputs, [{...trade, price: '49000'}, {...trade, price: '52000'}], asOf);
  assert.equal(result.median, 50500);
  assert.deepEqual(result.priceRange, [49000, 52000]);
  assert.equal(result.bidLimit, 46000);
  assert.deepEqual(result.scenarios, [
    {number: 1, bid: 42000, subtotal: 44000, meetsMinimum: true, budgetLeft: 4000, comparableGap: 6500},
    {number: 2, bid: 47000, subtotal: 49000, meetsMinimum: true, budgetLeft: -1000, comparableGap: 1500},
  ]);
});

test('auction minimum, fresh transactions and rights state gate the opinion', () => {
  const checked = {...inputs, registry: 'reviewed', occupancy: 'reviewed', liabilities: 'reviewed'};
  const caseData = {...asOf, minimum: 420_000_000};
  const report = {verdict: '검토 가능'};
  const old = calculatePurchase(checked, [{...trade, date: '2024-07-30', price: '49000'}], caseData);
  assert.equal(purchaseOpinion(old, report).verdict, '판단 보류');
  const current = calculatePurchase(checked, [{...trade, price: '49000'}], caseData);
  assert.equal(purchaseOpinion(current, {verdict: '주의 필요'}).verdict, '판단 보류');
  assert.equal(purchaseOpinion(current, report).verdict, '조건 충족 시 추가 검토');
  const raisedMinimum = calculatePurchase(checked, [{...trade, price: '49000'}], {...caseData, minimum: 430_000_000});
  assert.equal(purchaseOpinion(raisedMinimum, report).verdict, '가격 조건 재검토');
});

test('unknown costs stay unknown instead of counting as confirmed zero', () => {
  const result = calculatePurchase({...inputs, handover: ''}, [{...trade, price: '49000'}], asOf);
  assert.deepEqual(result.missingCosts, ['handover']);
  assert.equal(result.scenarios[0].subtotal, 44000);
  assert.equal(result.scenarios[0].comparableGap, null);
  assert.equal(result.scenarios[0].budgetLeft, null);
  assert.equal(result.bidLimit, null);
});

test('incomplete comparisons do not contribute and unresolved rights stay visible', () => {
  const result = calculatePurchase({...inputs, registry: 'issue', occupancy: 'reviewed'}, [
    {...trade, price: '49000', source: ' '}, {...trade, price: '51000'},
  ], asOf);
  assert.equal(result.tradeCount, 1);
  assert.equal(result.median, 51000);
  assert.deepEqual(result.checks, ['최신 등기 쟁점 있음', '낙찰자 인수 부담 미확인']);
});

test('document review keeps rights unknowns without transactions or bid costs', () => {
  const result = calculatePurchase({registry: 'issue', occupancy: 'reviewed', liabilities: 'reviewed'}, [], asOf);
  const opinion = purchaseOpinion(result, {verdict: '검토 가능'}, {comparisonEnabled: false});
  assert.equal(opinion.verdict, '판단 보류');
  assert.deepEqual(opinion.blockers.map(item => item.item), ['최신 등기 쟁점 있음']);
  assert.equal(purchaseOpinion({...result, checks: []}, {verdict: '검토 가능'}, {comparisonEnabled: false}).verdict, '서류 검토 가능');
});
