// Phase-5 load test — POST /tenants/{id}/checks (ADR-0014 §1, target 2).
//
// The deterministic WRITE path: run_check opens ONE session_scope transaction
// and, per active control, reads + writes (check_run row, audit rows, and on a
// status change a finding transition). It holds its pooled connection for the
// WHOLE transaction — far longer than the readiness read. Prediction (ADR-0014
// §1): this is where the 15-connection pool should actually exhaust, so the
// SIGNATURE differs from readiness — errors/queueing appear near ~15 VU, p95
// climbs WHILE throughput flattens or drops, and (the decisive proof, captured
// out-of-band by the sweep driver) pdpl_app's server-side connection count pegs
// at the pool ceiling. That contrast — CPU ceiling on the read vs pool
// exhaustion on the write — is the whole lesson.
//
// Same VU stages as readiness.js. Driven one constant level at a time by
// load/checks_sweep.py, which samples pg_stat_activity alongside each level.

import http from 'k6/http';
import { check } from 'k6';
import { SharedArray } from 'k6/data';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const SCENARIO = __ENV.SCENARIO || 'ramp';

const tenantIds = new SharedArray('tenant_ids', function () {
  return JSON.parse(open('../seed/tenant_ids.json'));
});

const allScenarios = {
  baseline: { executor: 'constant-vus', vus: 3, duration: '30s' },
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
  soak: {
    executor: 'constant-vus',
    vus: Number(__ENV.SOAK_VUS || 12),
    duration: __ENV.SOAK_DURATION || '3m',
  },
};

export const options = {
  scenarios: { [SCENARIO]: allScenarios[SCENARIO] },
  // Report-only — we OBSERVE the knee, never abort at it (ADR-0014 §4). On the
  // write path a non-zero http_req_failed rate is itself a signal (pool-checkout
  // timeouts), so it must NOT abort the run.
  thresholds: {
    http_req_failed: [{ threshold: 'rate<1.0', abortOnFail: false }],
    http_req_duration: [{ threshold: 'p(95)<60000', abortOnFail: false }],
  },
};

export default function () {
  const id = tenantIds[Math.floor(Math.random() * tenantIds.length)];
  // POST with no body — trigger_check takes none (kind defaults to "manual").
  const res = http.post(`${BASE_URL}/tenants/${id}/checks`);
  check(res, { 'status is 201': (r) => r.status === 201 });
}
