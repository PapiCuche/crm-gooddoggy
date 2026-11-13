// F1-14: el build de producción no contiene el prototipo /demo (AGENTS.md §11). Lee la salida de
// `next build` en el directorio actual y termina con 1 si alguna ruta o archivo del servidor es de
// /demo. La lanza frontend/Dockerfile tras construir con CRM_BUILD_PROFILE=production.
import { existsSync, readFileSync } from "node:fs";

const routes = Object.values(
  JSON.parse(readFileSync(".next/app-path-routes-manifest.json", "utf8")),
);
const leaked = routes.filter((route) => route === "/demo" || route.startsWith("/demo/"));
if (leaked.length > 0 || existsSync(".next/server/app/demo")) {
  console.error(
    `El build de producción contiene /demo: ${leaked.join(", ") || ".next/server/app/demo"}`,
  );
  process.exit(1);
}
console.log(`build sin /demo (${routes.length} rutas)`);
