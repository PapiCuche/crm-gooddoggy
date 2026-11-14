import { vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Quote, QuoteDetail, SelfContext } from "@/lib/api/model";
import { renderApp } from "@/test-utils";

export const NOW = Date.parse("2026-11-11T15:00:00Z");
export const BASE = "/api/v1/o/acme/quotes/";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: [],
};
export const quote = (id: string, changes: Partial<Quote> = {}) =>
  ({
    ...{ id, number: `COT-00000${id}`, revision: 1, status: "SENT", currency: "PEN" },
    ...{ contact: { id: "c1", display_name: "Rosa Díaz" }, opportunity_id: null },
    ...{ tax_mode: "INCLUDED", tax_rate: "0.1800", subtotal: "423.73", discount_total: "20.00" },
    ...{ tax_total: "76.27", total: "500.00", valid_until: "2026-11-30T05:00:00Z" },
    ...{ created_at: "2026-11-05T10:00:00Z", updated_at: "x", version: 1, ...changes },
  }) as Quote;
export const line = (id: string, changes: Record<string, unknown> = {}) => ({
  ...{ id, sort_order: 1, variant_id: "v1", product_name: "Collar", variant_name: "Talla M" },
  ...{ sku: "COL-M", quantity: 2, list_unit_price: "130.00", promotion_id: null },
  ...{ promotion_name: null, unit_price: "130.00", discount_type: "NONE" },
  ...{ discount_value: "0.00", discount_amount: "0.00", final_unit_price: "130.00" },
  ...{ line_total: "260.00", currency: "PEN", ...changes },
});
export const detail = (changes: Record<string, unknown> = {}) =>
  ({
    ...quote("1"),
    items: [line("l1")],
    notes_to_customer: "",
    internal_notes: "",
    ...changes,
  }) as unknown as QuoteDetail;

type Reply = { status: number; body?: unknown };
// El servidor de la prueba: responde según la ruta y los filtros que pidió el navegador.
export function serve(handler: (url: URL) => Reply) {
  const urls: URL[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://x");
      urls.push(url);
      const { status, body } = handler(url);
      return new Response(body === undefined ? null : JSON.stringify(body), { status });
    }),
  );
  return urls;
}
export const page = (...results: unknown[]) => ({ status: 200, body: { results, next: null } });
export const withTenant = (node: React.ReactNode, ...codes: string[]) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions: codes.map((code) => ({ code, scopes: [] })) }}>
      {node}
    </TenantProvider>,
  );
