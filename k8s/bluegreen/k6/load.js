// Mismo tráfico hacia ACTIVE (versión estable) y PREVIEW (versión nueva).
// Se usa en dos lugares:
//   - Dentro del clúster, como parte del análisis previo a la promoción (ventana).
//   - Desde la máquina, con `bg.sh load`.
// Cada solicitud queda etiquetada con target (active/preview), version y route en el
// JSON de salida, para que el analizador AIOps pueda comparar ambas versiones.
import http from 'k6/http';
import { check } from 'k6';

const DURATION = __ENV.DURATION || '60s';
const RATE = Number(__ENV.RATE || 5);           // solicitudes por segundo a CADA versión
const FUNCTIONAL_SHARE = 0.1;                   // 10% de las iteraciones registran una visita
const ACTIVE_URL = __ENV.ACTIVE_URL || 'http://localhost:8080';
const PREVIEW_URL = __ENV.PREVIEW_URL || 'http://localhost:8081';

const BROWSE = ['/', '/vets.html', '/owners?lastName=', '/owners/1', '/owners/find', '/owners?lastName=Davis'];
// Dueño -> mascota de los datos de ejemplo. Se reparten las visitas para que
// ninguna ficha crezca demasiado y sesgue la latencia.
const OWNER_PET = [[1, 1], [2, 2], [3, 3], [4, 5], [5, 6], [6, 7], [7, 9], [8, 10], [9, 11], [10, 12]];

function scenario(exec) {
  return { executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: DURATION,
           preAllocatedVUs: 5, maxVUs: 30, exec };
}

export const options = {
  scenarios: { active: scenario('active'), preview: scenario('preview') },
  // Límites mínimos para aprobar PREVIEW (el análisis inteligente lo hace el analizador AIOps)
  thresholds: {
    'http_req_failed{target:preview}': ['rate<0.01'],
    'checks{target:preview}': ['rate>0.99'],
  },
};

export function setup() {
  const version = (base) => {
    try { return JSON.parse(http.get(`${base}/actuator/info`, { tags: { target: 'setup' } }).body).app.version; }
    catch (e) { return 'unknown'; }
  };
  const v = { active: version(ACTIVE_URL), preview: version(PREVIEW_URL) };
  console.log(`active=${v.active} preview=${v.preview} duration=${DURATION} rate=${RATE}/s`);
  return v;
}

function tomorrow() {
  return new Date(Date.now() + 86400000).toISOString().slice(0, 10);
}

function run(base, target, version) {
  if (Math.random() < FUNCTIONAL_SHARE) {
    // Flujo funcional: registrar una visita y comprobar que aparece en la ficha del dueño
    const [owner, pet] = OWNER_PET[Math.floor(Math.random() * OWNER_PET.length)];
    const description = `k6 ${target} ${version} ${Date.now()}-${__VU}-${__ITER}`;
    const tags = { target, version, route: 'POST /owners/{id}/pets/{id}/visits/new' };
    const res = http.post(`${base}/owners/${owner}/pets/${pet}/visits/new`,
                          { date: tomorrow(), description }, { tags });
    check(res, { 'visita registrada': (r) => r.status === 200 && r.body.includes(description) }, tags);
  } else {
    const path = BROWSE[Math.floor(Math.random() * BROWSE.length)];
    const tags = { target, version, route: path };
    const res = http.get(`${base}${path}`, { tags });
    check(res, { 'status 200': (r) => r.status === 200 }, tags);
  }
}

export function active(v)  { run(ACTIVE_URL,  'active',  v.active); }
export function preview(v) { run(PREVIEW_URL, 'preview', v.preview); }
