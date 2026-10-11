// Comprobación de la imagen en ejecución (F2-07): cabeceras de seguridad y nonce en el HTML.
// Uso: node scripts/check-security-headers.mjs http://127.0.0.1:3000
// La lanza la prueba de humo (infra/docker/smoke-image.sh, SMOKE_CHECK) en `make check` y en CI.
import { buildCsp } from "../src/lib/csp.ts";

const base = process.argv[2];
const FIXED = {
  "x-frame-options": "DENY",
  "x-content-type-options": "nosniff",
  "referrer-policy": "strict-origin-when-cross-origin",
  "strict-transport-security": "max-age=31536000; includeSubDomains; preload",
  "cross-origin-opener-policy": "same-origin",
  "permissions-policy": "camera=(), geolocation=(), microphone=(), payment=()",
};
// Rutas reales, la demo y dos 404 de Next (su HTML también lleva scripts).
const PAGES = {
  "/login": 200,
  "/recuperar": 200,
  "/o": 200,
  "/o/ci": 200,
  "/demo": 200,
  "/demo/workspace/inbox": 200,
  "/no-existe": 404,
  "/favicon.ico": 404,
};
const failures = [];

for (const [path, status] of Object.entries(PAGES)) {
  const fail = (message) => failures.push(`${path}: ${message}`);
  // Un cliente no puede elegir el nonce: la cabecera enviada se descarta.
  const response = await fetch(base + path, {
    headers: { "Content-Security-Policy": "script-src 'nonce-forjado'" },
  });
  if (response.status !== status) fail(`estado ${response.status}, se esperaba ${status}`);
  const csp = response.headers.get("content-security-policy") ?? "";
  const nonce = /'nonce-([A-Za-z0-9+/]{22}==)'/.exec(csp)?.[1];
  if (!nonce) fail("la CSP no lleva un nonce de 128 bits");
  else if (csp !== buildCsp(nonce)) fail(`CSP inesperada: ${csp}`);
  for (const [name, value] of Object.entries(FIXED)) {
    if (response.headers.get(name) !== value) fail(`${name}: ${response.headers.get(name)}`);
  }
  const html = await response.text();
  if (html.includes("forjado")) fail("el HTML usa el nonce enviado por el cliente");
  const tags = html.match(/<(script|style)\b[^>]*>/g) ?? [];
  if (!tags.some((tag) => tag.startsWith("<script"))) fail("el HTML no contiene scripts");
  for (const tag of tags) {
    if (!tag.includes(`nonce="${nonce}"`)) fail(`sin el nonce de la petición: ${tag.slice(0, 80)}`);
  }
}

if (failures.length > 0) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log(`cabeceras y nonce OK: ${Object.keys(PAGES).join(" ")}`);
