// Mismo tráfico hacia ACTIVE (versión estable) y PREVIEW (versión nueva).
// Cada solicitud queda etiquetada con target y version en el JSON de salida,
// para que el analizador pueda comparar ambas versiones.
import http from 'k6/http';
import { check, sleep } from 'k6';

const DURATION = __ENV.DURATION || '60s';
const PATHS = ['/', '/vets.html', '/owners?lastName=', '/owners/1', '/owners/find', '/owners?lastName=Davis'];

function scenario(target) {
  return { executor: 'constant-arrival-rate', rate: 5, timeUnit: '1s', duration: DURATION,
           preAllocatedVUs: 5, maxVUs: 20, exec: target };
}

export const options = {
  scenarios: { active: scenario('active'), preview: scenario('preview') },
  thresholds: { 'http_req_failed{target:active}': ['rate<0.01'] },
};

function hit(base, target, version) {
  const path = PATHS[Math.floor(Math.random() * PATHS.length)];
  const res = http.get(`${base}${path}`, { tags: { target, version, route: path } });
  check(res, { 'status 200': (r) => r.status === 200 }, { target, version });
}

export function active()  { hit(__ENV.ACTIVE_URL,  'active',  __ENV.ACTIVE_VERSION  || 'unknown'); }
export function preview() { hit(__ENV.PREVIEW_URL, 'preview', __ENV.PREVIEW_VERSION || 'unknown'); }
