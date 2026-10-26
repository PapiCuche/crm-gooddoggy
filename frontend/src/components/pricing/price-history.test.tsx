import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { ListPrice, Price, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { ListPrices } from "./list-prices";

const API = "/api/v1/o/acme";
const HISTORY = `GET ${API}/pricing/variants/v1/price-history/?price_list_id=p1`;
const MEMBERS = `GET ${API}/members/?limit=200`;
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants("prices.view", "products.view", ...codes),
});
const row: ListPrice = {
  variant: {
    id: "v1",
    sku: "LAP-15",
    name: "15 pulgadas",
    product_id: "pr1",
    product_name: "Laptop",
  },
  current: {
    ...{ price_id: "h2", amount: "4399.00", currency: "PEN", tax_included: true },
    ...{ starts_at: "2026-10-26T14:00:00Z", ends_at: null },
  },
  next: null,
  resolution: { found: true, reason: null, final_amount: "4399.00", valid_until: null },
};
const change = (id: string, more: Partial<Price>): Price => ({
  ...{ id, variant_id: "v1", price_list_id: "p1", amount: "4399.00", previous_amount: null },
  ...{ currency: "PEN", tax_included: true, ends_at: null, change_reason: "x" },
  ...{ starts_at: "2026-10-26T14:00:00Z", created_at: "2026-10-26T14:00:00Z" },
  ...{ created_by_user_id: null, ...more },
});
const newest = change("h3", {
  ...{ amount: "4299.00", previous_amount: "4399.00", change_reason: "Campaña <b>día 1</b>" },
  ...{ created_at: "2026-10-26T15:00:00Z", starts_at: "2099-11-01T05:00:00Z" },
  created_by_user_id: "u2",
});
const middle = change("h2", {
  ...{ amount: "4399.00", previous_amount: "4599.00", change_reason: "Promoción" },
  created_by_user_id: "u1",
});
const first = change("h1", {
  ...{ amount: "4599.00", change_reason: "Precio inicial", created_by_user_id: "u9" },
  // 21/10 en UTC, todavía 20/10 en Lima.
  ...{ created_at: "2026-10-21T02:00:00Z", starts_at: "2026-10-21T02:00:00Z" },
});
const seeded = change("h0", { amount: "10.00", currency: "USD", change_reason: "Importado" });
const page = (results: unknown[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
const base = {
  [`GET ${API}/pricing/price-lists/?limit=200`]: page([
    { id: "p1", name: "Regular", is_default: true, is_active: true, sort_order: 0 },
  ]),
  [`GET ${API}/products/?limit=200`]: page([]),
  [`GET ${API}/pricing/price-lists/p1/prices/`]: page([row]),
  [MEMBERS]: page([
    {
      id: "m2",
      user: { id: "u2", email: "maria@acme.pe", first_name: "María", last_name: "Pérez" },
    },
  ]),
};
const showModal = vi.fn(function (this: HTMLDialogElement) {
  this.open = true;
});
async function opened(routes: Parameters<typeof mockApi>[0], ...codes: string[]) {
  const api = mockApi({ ...base, ...routes });
  renderApp(
    <TenantProvider value={tenant(...codes)}>
      <ListPrices />
    </TenantProvider>,
  );
  const open = await screen.findByRole("button", {
    name: "Historial de Laptop 15 pulgadas (LAP-15)",
  });
  open.focus();
  fireEvent.click(open);
  return { api, open, panel: screen.getByRole("dialog", { name: "Historial de precios" }) };
}
const urls = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.map(([url]) => `GET ${String(url)}`);

beforeAll(() => {
  // jsdom no implementa el `<dialog>` modal: se refleja `open` y el evento `close`.
  HTMLDialogElement.prototype.showModal = showModal;
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.open = false;
    this.dispatchEvent(new Event("close"));
  };
});
afterEach(() => vi.unstubAllGlobals());

describe("PriceHistory", () => {
  it("solo con `prices.manage` hay «Historial» en la fila", async () => {
    mockApi(base);
    renderApp(
      <TenantProvider value={tenant("users.view")}>
        <ListPrices />
      </TenantProvider>,
    );
    expect(await screen.findByText("LAP-15")).toBeVisible();
    expect(screen.queryByRole("button", { name: /^Historial/ })).toBeNull();
  });

  it("abre un panel modal con los cambios de la lista, del más nuevo al más antiguo, y su autor", async () => {
    const { api, panel } = await opened(
      { [HISTORY]: page([newest, middle, first], "c2"), [`${HISTORY}&cursor=c2`]: page([seeded]) },
      "prices.manage",
      "users.view",
    );
    expect(showModal).toHaveBeenCalled();
    expect(within(panel).getByRole("heading", { name: "Historial de precios" })).toHaveFocus();
    expect(panel).toHaveTextContent("Laptop 15 pulgadas (LAP-15) · Lista Regular");
    const list = await within(panel).findByRole("list", { name: /^Cambios de precio/ });
    const [one, two, three] = within(list).getAllByRole("listitem");
    expect(one).toHaveTextContent(
      /^26\/10\/2026 · María Pérez · S\/\s4,399\.00 → S\/\s4,299\.00 · Campaña <b>día 1<\/b>Programado: rige desde el 01\/11\/2099 00:00$/,
    );
    expect(two).toHaveTextContent(
      /^26\/10\/2026 · Tú · S\/\s4,599\.00 → S\/\s4,399\.00 · Promoción$/,
    );
    expect(three).toHaveTextContent(
      /^20\/10\/2026 · Otro miembro · Sin precio → S\/\s4,599\.00 · Precio inicial$/,
    );
    const more = within(panel).getByRole("button", { name: "Cargar más" });
    more.focus();
    fireEvent.click(more);
    const last = await within(panel).findByText(/Sistema/);
    // Cada cambio con su moneda; el foco pasa a lo que llegó (el botón ya no está).
    expect(last).toHaveTextContent(/Sistema · Sin precio → USD\s10\.00 · Importado/);
    await waitFor(() => expect(last.closest("li")).toHaveFocus());
    expect(urls(api)).toContain(`${HISTORY}&cursor=c2`);
    expect(panel).not.toHaveTextContent(/costo|margen/i);
  });

  it("sin `users.view` no pide el directorio: «Tú» o «Otro miembro»", async () => {
    const { api, panel } = await opened({ [HISTORY]: page([newest, middle]) }, "prices.manage");
    const [one, two] = within(await within(panel).findByRole("list")).getAllByRole("listitem");
    expect(one).toHaveTextContent("· Otro miembro ·");
    expect(two).toHaveTextContent("· Tú ·");
    expect(urls(api)).not.toContain(MEMBERS);
  });

  it("cerrar (botón o Escape) quita el panel y devuelve el foco a «Historial»", async () => {
    const { open, panel } = await opened({ [HISTORY]: page([]) }, "prices.manage");
    expect(await within(panel).findByText(/todavía no tiene cambios de precio/)).toBeVisible();
    fireEvent.click(within(panel).getByRole("button", { name: "Cerrar el historial" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(open).toHaveFocus();
    fireEvent.click(open);
    fireEvent(screen.getByRole("dialog"), new Event("close")); // Escape cierra el `<dialog>`
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(open).toHaveFocus();
  });

  it.each([
    [403, "PERMISSION_DENIED", "No tienes permiso para ver el historial de precios o el catálogo."],
    [404, "NOT_FOUND", "La variante o la lista ya no está disponible."],
  ])("un %i se explica", async (status, code, text) => {
    const { panel } = await opened({ [HISTORY]: { status, body: { code } } }, "prices.manage");
    expect(await within(panel).findByRole("alert")).toHaveTextContent(text);
  });

  it("sin sesión (401) queda el aviso de carga, sin error", async () => {
    const reply = { status: 401, body: { code: "NOT_AUTHENTICATED" } };
    const { api, panel } = await opened({ [HISTORY]: reply }, "prices.manage");
    await waitFor(() => expect(urls(api)).toContain(HISTORY));
    await new Promise((resolve) => setTimeout(resolve, 50)); // la respuesta ya llegó
    expect(within(panel).getByRole("status")).toHaveTextContent("Cargando el historial…");
    expect(within(panel).queryByRole("alert")).toBeNull();
  });

  it("pulsar dentro del panel no lo cierra; en el fondo, sí; Enter mantenido no lo reabre", async () => {
    const { open, panel } = await opened({ [HISTORY]: page([]) }, "prices.manage");
    const box = { left: 100, right: 500, top: 0, bottom: 800 };
    panel.getBoundingClientRect = () =>
      ({ ...box, x: 100, y: 0, width: 400, height: 800 }) as DOMRect;
    fireEvent.click(panel, { clientX: 300, clientY: 700 }); // bajo las filas, dentro
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.click(panel, { clientX: 50, clientY: 300 }); // el fondo, a la izquierda
    expect(screen.queryByRole("dialog")).toBeNull();
    const held = new KeyboardEvent("keydown", {
      key: "Enter",
      repeat: true,
      bubbles: true,
      cancelable: true,
    });
    open.dispatchEvent(held);
    expect(held.defaultPrevented).toBe(true);
  });

  it("«Programado» cuando rige en otro minuto (en Lima) que el que se fijó", async () => {
    const same = change("s1", {
      created_at: "2026-10-26T15:00:10Z",
      starts_at: "2026-10-26T15:00:50Z",
    });
    const next = change("s2", {
      created_at: "2026-10-26T15:00:50Z",
      starts_at: "2026-10-26T15:01:05Z",
    });
    const { panel } = await opened({ [HISTORY]: page([next, same]) }, "prices.manage");
    const [one, two] = within(await within(panel).findByRole("list")).getAllByRole("listitem");
    expect(one).toHaveTextContent("Programado: rige desde el 26/10/2026 10:01");
    expect(two).not.toHaveTextContent("Programado");
  });

  it("un error se reintenta, y el de la página siguiente se avisa", async () => {
    const failed = { status: 500, body: { code: "INTERNAL_ERROR" } };
    let up = false; // falla (también sus reintentos automáticos) hasta que vuelve el servidor
    const { panel } = await opened(
      { [HISTORY]: () => (up ? page([middle], "c2") : failed), [`${HISTORY}&cursor=c2`]: failed },
      "prices.manage",
    );
    expect(await within(panel).findByRole("alert")).toHaveTextContent(/Algo salió mal/);
    up = true;
    fireEvent.click(within(panel).getByRole("button", { name: "Reintentar" }));
    expect(await within(panel).findByText(/Promoción/)).toBeVisible();
    fireEvent.click(within(panel).getByRole("button", { name: "Cargar más" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent(/Algo salió mal/);
    expect(within(panel).getByText(/Promoción/)).toBeVisible();
  });

  it("mientras llega el directorio, el autor es «…»", async () => {
    const api = mockApi({ ...base, [HISTORY]: page([newest]) });
    // El directorio no responde todavía.
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
      `GET ${String(input)}` === MEMBERS ? new Promise(() => {}) : api(input, init),
    );
    renderApp(
      <TenantProvider value={tenant("prices.manage", "users.view")}>
        <ListPrices />
      </TenantProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /^Historial de/ }));
    const panel = screen.getByRole("dialog");
    expect(await within(panel).findByRole("listitem")).toHaveTextContent("26/10/2026 · … · ");
  });
});
