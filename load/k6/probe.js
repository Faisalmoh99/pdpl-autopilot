// Phase-5 load test — hold-time probe (ADR-0014 §4/§7).
//
// Hits GET /_loadprobe/hold on the LOAD-ONLY probe app (load/probe_app.py),
// which holds a pooled connection across a pure asyncio.sleep — isolating
// connection HOLD-TIME as the single variable. Driven by load/pool_sweep.py in
// "probe" mode: the SAME pool-size sweep {5,10,15,25} that left readiness and
// checks FLAT should make THIS path's throughput TRACK pool size, because a long
// hold-time makes the 15-connection pool (not the event loop) the binding
// resource. Same conn=15 saturation; opposite sweep response = the full proof
// that hold-time alone decides which resource binds.

import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';

export const options = {
  scenarios: {
    soak: {
      executor: 'constant-vus',
      vus: Number(__ENV.SOAK_VUS || 30),
      duration: __ENV.SOAK_DURATION || '20s',
    },
  },
  thresholds: {
    http_req_failed: [{ threshold: 'rate<1.0', abortOnFail: false }],
    http_req_duration: [{ threshold: 'p(95)<60000', abortOnFail: false }],
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/_loadprobe/hold`);
  check(res, { 'status is 200': (r) => r.status === 200 });
}
