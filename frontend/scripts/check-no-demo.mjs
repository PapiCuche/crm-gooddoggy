// F1-14: el build de producción no contiene el prototipo /demo (AGENTS.md §11). Lee la salida de
// `next build` en el directorio actual (rutas de App Router y de Pages Router) y termina con 1 si
// alguna ruta o archivo del servidor es de /demo. La lanza frontend/Dockerfile tras construir.
import { existsSync, readFileSync } from "node:fs";

const read = (path) => (existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : {});
const routes = [
  ...Object.values(read(".next/app-path-routes-manifest.json")),
  ...Object.keys(read(".next/server/pages-manifest.json")),
];
if (routes.length === 0) {
  console.error("Sin manifiestos de rutas en .next: ¿se ejecutó `next build`?");
  process.exit(1);
}
const leaked = routes.filter((route) => /^\/demo(\/|$)/.test(route));
for (const dir of [".next/server/app/demo", ".next/server/pages/demo"]) {
  if (existsSync(dir)) leaked.push(dir);
}
if (leaked.length > 0) {
  console.error(`El build de producción contiene /demo: ${leaked.join(", ")}`);
  process.exit(1);
}
console.log(`build sin /demo (${routes.length} rutas)`);
