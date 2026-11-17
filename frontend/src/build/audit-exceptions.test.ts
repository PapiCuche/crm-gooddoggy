// @vitest-environment node
// SEC-DEV-03 (#928, ADR-042): la única excepción de `pnpm audit` que admite pnpm-workspace.yaml y
// su caducidad. Corre en `pnpm test` (CI «frontend checks» y `make check`), antes de la auditoría.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const WORKSPACE = fileURLToPath(new URL("../../pnpm-workspace.yaml", import.meta.url));
const FC5Q = "GHSA-fc5q-39xc-vjpf";
// Lo que autorizó el propietario el 2026-11-17 (#928): un aviso y su fecha de caducidad. Añadir un
// aviso o alargar la fecha pide una decisión nueva del propietario y un ADR que acote ADR-012.
const ALLOWED: Readonly<Record<string, string>> = { [FC5Q]: "2026-12-01" };

const GHSA = /GHSA(?:-[0-9a-z]{4}){3}/gi;
const EXPIRY = /#\s*caduca:\s*(\d{4}-\d{2}-\d{2})\b/;
// Otras formas de callar `pnpm audit` desde este archivo: no se admite ninguna.
const OTHER_KEYS = /^\s*(audit|auditLevel|ignoreCves|ignore)\s*:/;

/** La línea sin su comentario de YAML (en este archivo ningún valor lleva `#`). */
const code = (line: string) => line.replace(/(^|\s)#.*$/, "");

/** Avisos ignorados: los ids GHSA que aparecen fuera de los comentarios. */
function ignoredGhsas(yaml: string): string[] {
  return yaml.split("\n").flatMap((line) => code(line).match(GHSA) ?? []);
}

/** Lo que incumple la decisión del propietario a la fecha `now`, comparada en UTC. */
function problems(yaml: string, now: Date): string[] {
  const today = now.toISOString().slice(0, 10);
  const found: string[] = [];
  for (const line of yaml.split("\n")) {
    if (OTHER_KEYS.test(code(line))) found.push(`clave de auditoría no admitida: ${line.trim()}`);
    if (/CVE-\d{4}-\d+/i.test(code(line))) found.push(`CVE ignorado: ${line.trim()}`);
    for (const id of code(line).match(GHSA) ?? []) {
      const limit = ALLOWED[id];
      const expiry = EXPIRY.exec(line)?.[1];
      if (limit === undefined) found.push(`${id}: aviso ignorado sin decisión del propietario`);
      else if (expiry === undefined) found.push(`${id}: falta «# caduca: AAAA-MM-DD» en su línea`);
      else if (expiry > limit) found.push(`${id}: caduca el ${expiry}, después del ${limit}`);
      else if (today > expiry) {
        found.push(`${id}: la excepción caducó el ${expiry}; quítala (#929) o pide otra decisión`);
      }
    }
  }
  const ids = ignoredGhsas(yaml);
  if (ids.length > 1) found.push(`más de un aviso ignorado: ${ids.join(", ")}`);
  return found;
}

const entry = (line: string) =>
  `minimumReleaseAge: 1440\nauditConfig:\n  ignoreGhsas:\n    - ${line}\n`;
const FIXTURE = entry(`${FC5Q} # caduca: 2026-12-01`);
const at = (iso: string) => new Date(iso);

describe("excepciones de pnpm audit (pnpm-workspace.yaml)", () => {
  const yaml = readFileSync(WORKSPACE, "utf8");

  it("ignora exactamente un aviso, GHSA-fc5q, con su fecha de caducidad al lado", () => {
    expect(ignoredGhsas(yaml)).toEqual([FC5Q]);
    expect(yaml).toMatch(
      /^auditConfig:\n {2}ignoreGhsas:\n {4}- GHSA-fc5q-39xc-vjpf # caduca: 2026-12-01\n/m,
    );
  });

  it("cumple la decisión del propietario hoy: falla desde el día siguiente a la caducidad", () => {
    expect(problems(yaml, new Date())).toEqual([]);
  });

  it("vale hasta el último segundo del 2026-12-01 (UTC) y caduca el 2026-12-02", () => {
    expect(problems(FIXTURE, at("2026-11-17T17:50:00Z"))).toEqual([]);
    expect(problems(FIXTURE, at("2026-12-01T23:59:59.999Z"))).toEqual([]);
    expect(problems(FIXTURE, at("2026-12-02T00:00:00Z"))).toEqual([
      `${FC5Q}: la excepción caducó el 2026-12-01; quítala (#929) o pide otra decisión`,
    ]);
  });

  it("rechaza cualquier otro aviso, uno de más, una fecha más larga o una sin fecha", () => {
    const now = at("2026-11-20T00:00:00Z");
    expect(problems(entry("GHSA-g3hv-75wg-m249 # caduca: 2026-12-01"), now)).toHaveLength(1);
    expect(
      problems(`${FIXTURE}    - GHSA-g3hv-75wg-m249 # caduca: 2026-12-01\n`, now),
    ).toHaveLength(2);
    expect(problems(entry(`${FC5Q} # caduca: 2026-12-31`), now)).toHaveLength(1);
    expect(problems(entry(FC5Q), now)).toHaveLength(1);
    expect(
      problems(`auditConfig:\n  ignoreGhsas: [${FC5Q}, GHSA-g3hv-75wg-m249]\n`, now),
    ).not.toEqual([]);
  });

  it("rechaza las otras formas de ignorar avisos o de bajar el nivel", () => {
    const now = at("2026-11-20T00:00:00Z");
    expect(problems("auditConfig:\n  ignoreCves:\n    - CVE-2026-1234\n", now)).not.toEqual([]);
    expect(problems(`audit:\n  ignore:\n    - ${FC5Q} # caduca: 2026-12-01\n`, now)).not.toEqual(
      [],
    );
    expect(problems("auditLevel: critical\n", now)).not.toEqual([]);
  });

  it("no cuenta los avisos citados en comentarios, y sin excepción no hay nada que cumplir", () => {
    const now = at("2027-01-01T00:00:00Z");
    expect(problems("# GHSA-xjj6-f33q-929c (alto)\noverrides:\n  undici@7: 7.30.1\n", now)).toEqual(
      [],
    );
  });
});
