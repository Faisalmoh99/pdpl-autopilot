// Phase-5 load test — POST /_loadexplain/{id} (ADR-0014 §1 target 3 / §7).
//
// Hits the LOAD-ONLY explain app (load/explain_app.py), which drives the REAL
// explanation orchestration (tenant read -> re-derive -> build_gap_context ->
// explain_gap: get -> [forced MISS] -> stub call -> GATE -> put) with an injected
// ~50ms async-latency stub standing in for Gemini. A fresh uuid4 prompt_version
// per request forces a cache MISS every time, so the stub call always runs and
// (in the BEFORE code) a pooled connection is held across it — the exact
// hold-time shape the probe simulated abstractly, now on the REAL path.
//
// Driven by load/pool_sweep.py in "explain" mode: the SAME pool-size sweep
// {5,10,15,25} that left readiness/checks FLAT.
//   - BEFORE the §7 fix (call INSIDE session_scope): throughput should TRACK pool
//     size -> POOL-bound, confirming the probe finding on the real path.
//   - AFTER the §7 fix (call OUTSIDE session_scope): throughput should go FLAT ->
//     event-loop-bound, the pool removed as the binding constraint.
//
// The `source is ai_verified` check proves the FULL miss path ran (gate PASSED,
// put happened); a `fallback` would mean the stub text was gate-rejected and put
// never ran — an incomplete miss path that would confound the measurement.

import http from 'k6/http';
import { check } from 'k6';
import { SharedArray } from 'k6/data';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';

const tenantIds = new SharedArray('tenant_ids', function () {
  return JSON.parse(open('../seed/tenant_ids.json'));
});

export const options = {
  scenarios: {
    soak: {
      executor: 'constant-vus',
      vus: Number(__ENV.SOAK_VUS || 30),
      duration: __ENV.SOAK_DURATION || '20s',
    },
  },
  // Report-only — we OBSERVE the knee, never abort at it (ADR-0014 §4).
  thresholds: {
    http_req_failed: [{ threshold: 'rate<1.0', abortOnFail: false }],
    http_req_duration: [{ threshold: 'p(95)<60000', abortOnFail: false }],
  },
};

export default function () {
  const id = tenantIds[Math.floor(Math.random() * tenantIds.length)];
  const res = http.post(`${BASE_URL}/_loadexplain/${id}`);
  check(res, {
    'status is 200': (r) => r.status === 200,
    'source is ai_verified': (r) => r.json('source') === 'ai_verified',
  });
}
