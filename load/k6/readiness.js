// Phase-5 load test — GET /tenants/{id}/readiness (ADR-0014 §1, target 1).
//
// The deterministic READ path: one `controls LEFT JOIN findings` query, a short
// transaction that releases its pooled connection quickly. Prediction (ADR-0014
// §1): a fast read cycles connections fast, so this may NOT knee cleanly at the
// 15-connection pool ceiling — the knee may appear only at high VUs, or the path
// may be CPU/query-bound before the pool. If it does not knee at the ceiling,
// that is itself a finding (fast reads are not pool-constrained).
//
// Run one scenario at a time so the signal is clean (ADR-0014 §4):
//   k6 run -e SCENARIO=baseline load/k6/readiness.js   # fixes p95_base
//   k6 run -e SCENARIO=ramp     load/k6/readiness.js   # locate the knee
//   k6 run -e SCENARIO=soak     load/k6/readiness.js   # drift/leak over time
//
// The KNEE rule is pre-registered (ADR-0014 §4), applied to the ramp output:
//   knee = first VU stage where p95 >= 2 * p95_base AND throughput has flattened
//   (req/s stops rising as VUs rise). Both conditions required.

import http from 'k6/http';
import { check } from 'k6';
import { SharedArray } from 'k6/data';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const SCENARIO = __ENV.SCENARIO || 'ramp';

// Loaded once per VU pool, shared (not re-parsed per iteration).
const tenantIds = new SharedArray('tenant_ids', function () {
  return JSON.parse(open('../seed/tenant_ids.json'));
});

const allScenarios = {
  // Baseline: low, steady concurrency to fix the reference p95 (p95_base).
  baseline: {
    executor: 'constant-vus',
    vus: 3,
    duration: '30s',
  },
  // Ramp: climb past the 15-connection pool ceiling from both sides.
  ramp: {
    executor: 'ramping-vus',
    startVUs: 1,
    stages: [
      { target: 5, duration: '30s' },
      { target: 10, duration: '30s' },
      { target: 15, duration: '30s' },
      { target: 20, duration: '30s' },
      { target: 30, duration: '30s' },
      { target: 50, duration: '30s' },
    ],
    gracefulRampDown: '5s',
  },
  // Soak: hold below the knee to surface drift/leaks over time.
  soak: {
    executor: 'constant-vus',
    vus: Number(__ENV.SOAK_VUS || 12),
    duration: __ENV.SOAK_DURATION || '3m',
  },
};

export const options = {
  scenarios: { [SCENARIO]: allScenarios[SCENARIO] },
  // Report-only thresholds — we OBSERVE the knee, never abort at it (ADR-0014
  // §4). abortOnFail:false keeps the run going so the full curve is captured.
  thresholds: {
    http_req_failed: [{ threshold: 'rate<1.0', abortOnFail: false }],
    http_req_duration: [{ threshold: 'p(95)<60000', abortOnFail: false }],
  },
};

export default function () {
  const id = tenantIds[Math.floor(Math.random() * tenantIds.length)];
  const res = http.get(`${BASE_URL}/tenants/${id}/readiness`);
  check(res, { 'status is 200': (r) => r.status === 200 });
}
