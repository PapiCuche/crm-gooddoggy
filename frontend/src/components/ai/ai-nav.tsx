"use client";

import { useTenant } from "@/components/app-shell/tenant-context";
import { SectionNav } from "@/components/catalog/catalog-nav";

import { MANAGE } from "./ai-switch";

const PAGES = [
  { key: "provider", path: "/ia" },
  { key: "handoff", path: "/ia/traspaso" },
  { key: "budget", path: "/ia/presupuesto" },
] as const;

// Las páginas de la IA: el proveedor con el interruptor (F8-04, F8-06), el texto de traspaso
// (F8-09c) y el presupuesto (F8-12a). Las tres piden `ai.manage`; el Supervisor (solo `ai.kill_switch`) tiene una página y
// no ve pestañas. Es comodidad: la API decide en cada petición.
export function AiNav() {
  const { permissions } = useTenant();
  return permissions.some((grant) => grant.code === MANAGE) ? (
    <SectionNav section="ai" pages={PAGES} />
  ) : null;
}
