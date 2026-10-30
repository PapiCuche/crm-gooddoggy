import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { ListMargin, PriceList, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { ListPrices } from "./list-prices";
import { isCostQuery } from "./use-margins";

const API = "/api/v1/o/acme";
const MARGINS = `GET ${API}/pricing/price-lists/p1/margins/?limit=200`;
const STEP_UP = "POST /api/v1/auth/mfa/step-up/";
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const seller = (...extra: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants("prices.view", "products.view", ...extra),
});
const regular: PriceList = {
  ...{ id: "p1", code: "REGULAR", name: "Regular", kind: "RETAIL", currency: "PEN" },
  ...{ is_default: true, required_permission: "prices.view", ai_exposable: false },
  ...{ payment_condition: null, is_active: true, sort_order: 1 },
};
const variant = (id: string, sku: string) => ({
  ...{ id, sku, name: "Var", product_id: "pr1", product_name: "Laptop" },
});
const priceRow = (id: string, sku: string) => ({
  variant: variant(id, sku),
  current: {
    ...{ price_id: `c${id}`, amount: "4399.00", currency: "PEN", tax_included: true },
    ...{ starts_at: "2026-10-20T15:30:00Z", ends_at: null },
  },
  next: null,
  resolution: { found: true, reason: null, final_amount: "4399.00", valid_until: null },
});
const side = (amount: string) => ({ amount, currency: "PEN", tax_included: true });
const margin = (id: string, sku: string, more: Partial<ListMargin>): ListMargin => ({
  ...{ variant: variant(id, sku), price: side("4399.00"), cost: side("3500.00") },
  ...{ margin: { amount: "899.00", percent: "20.4" }, reason: null, ...more },
});
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const base = (skus: string[]) => ({
  [`GET ${API}/pricing/price-lists/?limit=200`]: page([regular]),
  [`GET ${API}/products/?limit=200`]: page([{ id: "pr1", name: "Laptop" }]),
  [`GET ${API}/pricing/price-lists/p1/prices/`]: page(skus.map((sku) => priceRow(sku, sku))),
});
const screenOf = (tenant: SelfContext) =>
  renderApp(
    <TenantProvider value={tenant}>
      <ListPrices />
    </TenantProvider>,
  );
const row = async (sku: string) =>
  (await screen.findByText(sku, { selector: "span.font-mono" })).closest("li") as HTMLElement;
const switchOf = () => screen.findByRole("switch", { name: "Ver costos y margen" });
const requested = (api: ReturnType<typeof mockApi>, part: string) =>
  api.mock.calls.filter(([url]) => String(url).includes(part));
// Lo que la caché guarda de costos: la consulta vacía de un interruptor apagado no cuenta.
const kept = (client: ReturnType<typeof renderApp>["client"]) =>
  client
    .getQueryCache()
    .findAll({ predicate: isCostQuery })
    .filter((q) => q.state.data !== undefined);

beforeAll(() => {
  // jsdom no implementa el `<dialog>` modal: se refleja `open` y el evento `close`.
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.open = false;
    this.dispatchEvent(new Event("close"));
  };
});
beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("ListPrices con costos y margen", () => {
  it("sin product_cost.view no hay interruptor, ni columnas, ni una petición de costos", async () => {
    const api = mockApi({ ...base(["LAP-1"]), [MARGINS]: page([margin("LAP-1", "LAP-1", {})]) });
    // `product_cost.manage` solo no basta: lo que abre la pantalla es ver.
    screenOf(seller("prices.manage", "product_cost.manage"));
    expect(await row("LAP-1")).toHaveTextContent("S/ 4,399.00");
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByText("Margen %")).toBeNull();
    expect(screen.queryByText(/Registrar costo/)).toBeNull();
    expect(requested(api, "margins")).toHaveLength(0);
    expect(requested(api, "costs")).toHaveLength(0);
  });

  it("con el permiso, apagado no pide nada; al activarlo enseña costo y margen de la fila", async () => {
    const api = mockApi({ ...base(["LAP-1"]), [MARGINS]: page([margin("LAP-1", "LAP-1", {})]) });
    screenOf(seller("product_cost.view"));
    const item = await row("LAP-1");
    const toggle = await switchOf();
    expect(toggle).not.toBeChecked();
    expect(item).not.toHaveTextContent("Costo");
    expect(requested(api, "margins")).toHaveLength(0);

    fireEvent.click(toggle);
    expect(toggle).toBeChecked();
    expect(await within(item).findByText("S/ 3,500.00")).toBeVisible();
    expect(item).toHaveTextContent(/Margen S\/\s*S\/\s899\.00/);
    expect(item).toHaveTextContent(/Margen %\s*20\.4\s?%/);
    expect(item).not.toHaveTextContent("Pérdida");
    expect(screen.getByText(/importes con IGV incluido/)).toBeVisible();
    expect(requested(api, "margins")).toHaveLength(1);
  });

  it("STEP_UP_REQUIRED abre el diálogo y, con el código, repite la petición", async () => {
    let asked = 0;
    const api = mockApi({
      ...base(["LAP-1"]),
      [MARGINS]: () => {
        asked += 1;
        return asked === 1
          ? { status: 403, body: { code: "STEP_UP_REQUIRED" } }
          : page([margin("LAP-1", "LAP-1", {})]);
      },
      [STEP_UP]: { status: 200, body: { step_up_expires_in: 900 } },
    });
    screenOf(seller("product_cost.view"));
    const item = await row("LAP-1");
    fireEvent.click(await switchOf());
    await screen.findByRole("dialog", { name: "Confirma que eres tú" });
    fireEvent.change(screen.getByLabelText(/^Código/), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verificar" }));
    expect(await within(item).findByText("S/ 3,500.00")).toBeVisible();
    expect(requested(api, "margins")).toHaveLength(2);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cancelar el código deja el interruptor apagado y no enseña costos", async () => {
    mockApi({
      ...base(["LAP-1"]),
      [MARGINS]: { status: 403, body: { code: "STEP_UP_REQUIRED" } },
    });
    const { client } = screenOf(seller("product_cost.view"));
    const item = await row("LAP-1");
    const toggle = await switchOf();
    fireEvent.click(toggle);
    fireEvent.click(await screen.findByRole("button", { name: "Cancelar" }));
    await waitFor(() => expect(toggle).not.toBeChecked());
    expect(await screen.findByText(/costos siguen ocultos/)).toBeVisible();
    expect(item).not.toHaveTextContent("Costo");
    expect(kept(client)).toHaveLength(0);
  });

  it("un margen negativo se marca con texto y con color, y el -0.0 y los porcentajes enormes se normalizan", async () => {
    const loss = margin("LAP-1", "LAP-1", { margin: { amount: "-900.00", percent: "-25.7" } });
    const tiny = margin("LAP-2", "LAP-2", { margin: { amount: "-0.01", percent: "-0.0" } });
    const huge = margin("LAP-3", "LAP-3", {
      margin: { amount: "-999.00", percent: "-999999999999800.0" },
    });
    mockApi({
      ...base(["LAP-1", "LAP-2", "LAP-3"]),
      [MARGINS]: page([loss, tiny, huge]),
    });
    screenOf(seller("product_cost.view"));
    const [first, second, third] = await Promise.all([row("LAP-1"), row("LAP-2"), row("LAP-3")]);
    fireEvent.click(await switchOf());
    await within(first).findByText("S/ 3,500.00");
    expect(first).toHaveTextContent(/-S\/\s900\.00 · Pérdida/);
    expect(within(first).getByText(/Pérdida/)).toHaveClass("text-danger");
    expect(first).toHaveTextContent(/-25\.7\s?%/);
    expect(second).toHaveTextContent(/Margen %\s*0\.0\s?%/);
    expect(second).not.toHaveTextContent("-0.0");
    expect(third).toHaveTextContent("< -999 %");
    expect(third).not.toHaveTextContent("999999");
  });

  it("sin margen, el motivo en español en lugar de las cifras", async () => {
    const reasons = {
      NO_PRICE: "Sin margen: la variante no tiene precio",
      VARIANT_UNAVAILABLE: "Sin margen: no está a la venta",
      NO_COST: "Sin margen: falta registrar su costo",
      CURRENCY_MISMATCH: "Sin margen: el precio y el costo están en monedas distintas",
      TAX_MISMATCH: "Sin margen: uno incluye IGV y el otro no",
    } as const;
    const codes = Object.keys(reasons) as (keyof typeof reasons)[];
    const skus = codes.map((_, index) => `R-${index}`);
    const none = (reason: string) => ({
      price: null,
      margin: null,
      reason,
      cost: reason === "NO_COST" ? null : side("1.00"),
    });
    mockApi({
      ...base(skus),
      [MARGINS]: page(
        codes.map((c, i) => margin(skus[i]!, skus[i]!, none(c) as Partial<ListMargin>)),
      ),
    });
    screenOf(seller("product_cost.view"));
    const items = await Promise.all(skus.map(row));
    fireEvent.click(await switchOf());
    for (const [index, code] of codes.entries()) {
      await waitFor(() => expect(items[index]).toHaveTextContent(reasons[code]));
    }
    expect(items[2]).toHaveTextContent(/Costo\s*—/);
    expect(items[0]).not.toHaveTextContent(/Margen %/);
  });

  it("apagar el interruptor quita los costos de la caché de TanStack Query, no solo de la vista", async () => {
    mockApi({ ...base(["LAP-1"]), [MARGINS]: page([margin("LAP-1", "LAP-1", {})]) });
    const { client } = screenOf(seller("product_cost.view"));
    const item = await row("LAP-1");
    const toggle = await switchOf();
    fireEvent.click(toggle);
    await within(item).findByText("S/ 3,500.00");
    expect(kept(client)).toHaveLength(1);

    fireEvent.click(toggle);
    expect(item).not.toHaveTextContent("3,500.00");
    expect(kept(client)).toHaveLength(0);
    expect(
      JSON.stringify(
        client
          .getQueryCache()
          .getAll()
          .map((q) => q.state.data),
      ),
    ).not.toContain("3500");
  });

  it("a los 15 minutos los costos salen de la caché y la vista, y a los 14 aún no", async () => {
    mockApi({ ...base(["LAP-1"]), [MARGINS]: page([margin("LAP-1", "LAP-1", {})]) });
    const { client } = screenOf(seller("product_cost.view"));
    const item = await row("LAP-1");
    const toggle = await switchOf();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"], shouldAdvanceTime: true });
    fireEvent.click(toggle);
    await within(item).findByText("S/ 3,500.00");

    await act(() => vi.advanceTimersByTimeAsync(14 * 60_000));
    expect(toggle).toBeChecked();
    expect(kept(client)).toHaveLength(1);
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(toggle).not.toBeChecked();
    expect(kept(client)).toHaveLength(0);
    expect(item).not.toHaveTextContent("3,500.00");
  });

  it("al salir de la pantalla no queda ningún costo en la caché", async () => {
    mockApi({ ...base(["LAP-1"]), [MARGINS]: page([margin("LAP-1", "LAP-1", {})]) });
    const view = screenOf(seller("product_cost.view"));
    const item = await row("LAP-1");
    fireEvent.click(await switchOf());
    await within(item).findByText("S/ 3,500.00");
    view.unmount();
    expect(kept(view.client)).toHaveLength(0);
  });

  it("un 403 que no es de step-up se explica con un mensaje", async () => {
    const denied = { status: 403, body: { code: "PERMISSION_DENIED" } };
    mockApi({ ...base(["LAP-1"]), [MARGINS]: denied });
    screenOf(seller("product_cost.view"));
    fireEvent.click(await switchOf());
    expect(await screen.findByRole("alert")).toHaveTextContent(/No tienes permiso para ver/);
  });
});

// Los márgenes se contestan cuando el test lo suelta; la respuesta se decide al pedirla.
function gated(api: ReturnType<typeof mockApi>) {
  const gates: (() => void)[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const reply = api(input, init);
      if (String(input).includes("/margins/")) await new Promise<void>((go) => gates.push(go));
      return reply;
    }),
  );
  return gates;
}
const withCost = (amount: string) => page([margin("LAP-1", "LAP-1", { cost: side(amount) })]);
const inCache = (client: ReturnType<typeof renderApp>["client"], text: string) =>
  JSON.stringify(
    client
      .getQueryCache()
      .getAll()
      .map((q) => q.state.data),
  ).includes(text);

describe("ListPrices: costos que no deben quedarse", () => {
  it("al perder product_cost.view quita los costos y recuperarlo exige activar el interruptor", async () => {
    mockApi({ ...base(["LAP-1"]), [MARGINS]: page([margin("LAP-1", "LAP-1", {})]) });
    let setTenant: (next: SelfContext) => void = () => {};
    function Harness() {
      const [tenant, set] = useState(seller("product_cost.view"));
      setTenant = set;
      return (
        <TenantProvider value={tenant}>
          <ListPrices />
        </TenantProvider>
      );
    }
    const { client } = renderApp(<Harness />);
    const remove = vi.spyOn(client, "removeQueries");
    const item = await row("LAP-1");
    fireEvent.click(await switchOf());
    await within(item).findByText("S/ 3,500.00");
    act(() => setTenant(seller()));
    expect(item).not.toHaveTextContent("3,500.00");
    expect(screen.queryByRole("switch")).toBeNull();
    expect(remove).toHaveBeenCalled();
    expect(inCache(client, "3500")).toBe(false);
    act(() => setTenant(seller("product_cost.view")));
    expect(await switchOf()).not.toBeChecked();
    expect(item).not.toHaveTextContent("3,500.00");
  });

  it("sin los tres permisos de la ruta de márgenes no hay interruptor", async () => {
    mockApi({ ...base(["LAP-1"]) });
    const tenant = seller("product_cost.view");
    screenOf({ ...tenant, permissions: grants("product_cost.view", "prices.view") });
    await row("LAP-1");
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it("con la caché llena pero el interruptor apagado, la vista no enseña costos", async () => {
    mockApi({ ...base(["LAP-1"]) });
    const { client } = screenOf(seller("product_cost.view"));
    const item = await row("LAP-1");
    const key = ["/api/v1/o/acme/pricing/price-lists/p1/margins/", {}, "all"];
    client.setQueryData(key, [margin("LAP-1", "LAP-1", {})]);
    await switchOf();
    expect(item).not.toHaveTextContent("3,500.00");
    expect(item).not.toHaveTextContent("Margen");
  });

  it("un reloj que salta 20 minutos (portátil dormido) apaga al volver a la pestaña", async () => {
    mockApi({ ...base(["LAP-1"]), [MARGINS]: page([margin("LAP-1", "LAP-1", {})]) });
    const { client } = screenOf(seller("product_cost.view"));
    const item = await row("LAP-1");
    const toggle = await switchOf();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"], shouldAdvanceTime: true });
    fireEvent.click(toggle);
    await within(item).findByText("S/ 3,500.00");
    vi.setSystemTime(Date.now() + 20 * 60_000); // los temporizadores no corrieron
    expect(toggle).toBeChecked();
    await act(async () => void document.dispatchEvent(new Event("visibilitychange")));
    expect(toggle).not.toBeChecked();
    expect(inCache(client, "3500")).toBe(false);
  });

  it("una respuesta que llega tarde, tras apagar, no vuelve a llenar la caché", async () => {
    const gates = gated(mockApi({ ...base(["LAP-1"]), [MARGINS]: withCost("3500.00") }));
    const { client } = screenOf(seller("product_cost.view"));
    const item = await row("LAP-1");
    const toggle = await switchOf();
    fireEvent.click(toggle);
    await waitFor(() => expect(gates).toHaveLength(1));
    fireEvent.click(toggle);
    await act(async () => gates[0]!());
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(inCache(client, "3500")).toBe(false);
    expect(item).not.toHaveTextContent("3,500.00");
  });

  it("apagar y encender deprisa, con respuestas en desorden, se queda con la última", async () => {
    let n = 0;
    const gates = gated(
      mockApi({ ...base(["LAP-1"]), [MARGINS]: () => withCost(n++ === 0 ? "1111.00" : "2222.00") }),
    );
    const { client } = screenOf(seller("product_cost.view"));
    const item = await row("LAP-1");
    const toggle = await switchOf();
    fireEvent.click(toggle);
    await waitFor(() => expect(gates).toHaveLength(1));
    fireEvent.click(toggle);
    fireEvent.click(toggle);
    await waitFor(() => expect(gates).toHaveLength(2));
    await act(async () => gates[1]!());
    await within(item).findByText("S/ 2,222.00");
    await act(async () => gates[0]!()); // la vieja llega después
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(item).toHaveTextContent("2,222.00");
    expect(inCache(client, "1111")).toBe(false);
  });

  it("al cambiar de lista con el interruptor activo, los costos de la anterior se van de la caché", async () => {
    const other = { ...regular, id: "p2", code: "MAYOR", name: "Mayorista", is_default: false };
    mockApi({
      ...base(["LAP-1"]),
      [`GET ${API}/pricing/price-lists/?limit=200`]: page([regular, other]),
      [`GET ${API}/pricing/price-lists/p2/prices/`]: page([priceRow("LAP-1", "LAP-1")]),
      [MARGINS]: withCost("1234.00"),
      [MARGINS.replace("p1", "p2")]: withCost("5678.00"),
    });
    const { client } = screenOf(seller("product_cost.view"));
    const item = await row("LAP-1");
    fireEvent.click(await switchOf());
    await within(item).findByText("S/ 1,234.00");
    fireEvent.change(screen.getByLabelText("Lista de precios"), { target: { value: "p2" } });
    await within(await row("LAP-1")).findByText("S/ 5,678.00");
    await waitFor(() => expect(inCache(client, "1234")).toBe(false)); // `gcTime: 0`
  });

  it("si la segunda página pide el código, no se ven costos a medias; y repetirlo no es «sin permiso»", async () => {
    const cursor = `${MARGINS}&cursor=c2`;
    mockApi({
      ...base(["LAP-1", "LAP-2"]),
      [MARGINS]: { status: 200, body: { results: [margin("LAP-1", "LAP-1", {})], next: "c2" } },
      [cursor]: { status: 403, body: { code: "STEP_UP_REQUIRED" } },
      [STEP_UP]: { status: 200, body: { step_up_expires_in: 900 } },
    });
    const { client } = screenOf(seller("product_cost.view"));
    const [first] = [await row("LAP-1"), await row("LAP-2")];
    fireEvent.click(await switchOf());
    fireEvent.change(await screen.findByLabelText(/^Código/), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verificar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /confirmar de nuevo tu segundo factor/,
    );
    expect(first).not.toHaveTextContent("3,500.00");
    expect(inCache(client, "3500")).toBe(false);
  });
});
