// F1-14: el build de producción no contiene el prototipo /demo (AGENTS.md §11). Lee la salida de
// `next build` en el directorio actual y termina con 1 si alguna ruta o archivo del servidor es de
// /demo. El manifiesto del App Router es obligatorio (sin él, o vacío, falla: un cambio de Next no
// puede dejar pasar /demo); el del Pages Router es opcional. La lanza frontend/Dockerfile.
import { existsSync, readFileSync } from "node:fs";

const json = (path) => JSON.parse(readFileSync(path, "utf8"));
const fail = (message) => {
  console.error(message);
  process.exit(1);
};
let app;
try {
  app = Object.values(json(".next/app-path-routes-manifest.json"));
} catch (error) {
  fail(`Sin manifiesto de rutas del App Router (${error.code ?? error.name}): ¿cambió Next?`);
}
if (app.length === 0) fail("El manifiesto de rutas del App Router está vacío");
const pages = ".next/server/pages-manifest.json";
const routes = [...app, ...(existsSync(pages) ? Object.keys(json(pages)) : [])];
const leaked = routes.filter((route) => /^\/demo(\/|$)/.test(route));
for (const dir of [".next/server/app/demo", ".next/server/pages/demo"]) {
  if (existsSync(dir)) leaked.push(dir);
}
if (leaked.length > 0) fail(`El build de producción contiene /demo: ${leaked.join(", ")}`);
console.log(`build sin /demo (${routes.length} rutas)`);
