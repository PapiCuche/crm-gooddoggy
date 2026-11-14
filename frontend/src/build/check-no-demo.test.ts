// @vitest-environment node
// F1-14: la comprobación de la salida de `next build` que hace fallar la imagen con /demo dentro.
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SCRIPT = fileURLToPath(new URL("../../scripts/check-no-demo.mjs", import.meta.url));
const APP = ".next/app-path-routes-manifest.json";
const PAGES = ".next/server/pages-manifest.json";

function check(files: Record<string, unknown>, dirs: string[] = []) {
  const cwd = mkdtempSync(join(tmpdir(), "no-demo-"));
  mkdirSync(join(cwd, ".next/server"), { recursive: true });
  for (const dir of dirs) mkdirSync(join(cwd, dir), { recursive: true });
  for (const [path, body] of Object.entries(files)) {
    writeFileSync(join(cwd, path), JSON.stringify(body));
  }
  return spawnSync(process.execPath, [SCRIPT], { cwd, encoding: "utf8" }).status;
}

describe("check-no-demo", () => {
  it("pasa con un build sin /demo, con o sin manifiesto del Pages Router", () => {
    const app = { "/page": "/", "/o/[slug]/page": "/o/[slug]" };
    expect(check({ [APP]: app, [PAGES]: { "/500": "pages/500.html" } })).toBe(0);
    expect(check({ [APP]: app })).toBe(0);
  });

  it("falla con /demo en cualquiera de los dos routers o en el servidor", () => {
    expect(check({ [APP]: { "/(proto)/demo/page": "/demo" } })).toBe(1);
    expect(check({ [APP]: { "/page": "/" }, [PAGES]: { "/demo/x": "pages/demo/x.js" } })).toBe(1);
    expect(check({ [APP]: { "/page": "/" } }, [".next/server/app/demo"])).toBe(1);
  });

  it("falla cerrado si falta el manifiesto del App Router o está vacío", () => {
    expect(check({ [PAGES]: { "/500": "pages/500.html" } }, [".next/server/app/(g)/demo"])).toBe(1);
    expect(check({ [APP]: {}, [PAGES]: { "/500": "pages/500.html" } })).toBe(1);
    expect(check({})).toBe(1);
  });

  it("no confunde una ruta que solo empieza por demo", () => {
    expect(check({ [APP]: { "/demos/page": "/demos", "/o/demo/page": "/o/demo" } })).toBe(0);
  });
});
