import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import { getProductsListQueryKey } from "@/lib/api/client";
import type { ListPrice, PriceList, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { ListPrices } from "./list-prices";

const API = "/api/v1/o/acme";
const LISTS = `GET ${API}/pricing/price-lists/?limit=200`;
const PRODUCTS = `GET ${API}/products/?limit=200`;
const PRICES = (list: string, query = "") =>
  `GET ${API}/pricing/price-lists/${list}/prices/${query}`;
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const seller: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants("prices.view", "products.view"),
};
const list = (id: string, name: string, more: Partial<PriceList> = {}): PriceList => ({
  ...{ id, code: name.toUpperCase(), name, kind: "RETAIL", currency: "PEN" },
  ...{ is_default: false, required_permission: "prices.view", ai_exposable: false },
  ...{ payment_condition: null, is_active: true, sort_order: 10, ...more },
});
const regular = list("p1", "Regular", { is_default: true, sort_order: 20 });
const cash = list("p2", "Contado", { sort_order: 5 });
const old = list("p3", "Antigua", { is_active: false, sort_order: 0 });
const priced: ListPrice = {
  variant: {
    id: "v1",
    sku: "LAP-15-512",
    name: "15 pulgadas",
    product_id: "pr1",
    product_name: "Laptop",
  },
  current: {
    ...{ price_id: "c1", amount: "4599.00", currency: "PEN", tax_included: true },
    ...{ starts_at: "2026-10-20T15:30:00Z", ends_at: "2026-11-05T05:00:00Z" },
  },
  next: { price_id: "n1", amount: "4299.00", starts_at: "2026-11-01T05:00:00Z" },
  resolution: {
    // Distintos del vigente y su fin: la pantalla enseña lo que resuelve el motor.
    ...{ found: true, reason: null, final_amount: "4589.00" },
    valid_until: "2026-11-01T05:00:00Z",
  },
};
const unpriced: ListPrice = {
  variant: { ...priced.variant, id: "v2", sku: "LAP-13", name: "13 pulgadas" },
  ...{ current: null, next: null },
  resolution: { found: false, reason: "NO_PRICE", final_amount: null, valid_until: null },
};
const unavailable: ListPrice = {
  ...priced,
  variant: { ...priced.variant, id: "v3", sku: "LAP-17", name: "17 pulgadas" },
  next: null,
  resolution: {
    found: false,
    reason: "VARIANT_UNAVAILABLE",
    final_amount: null,
    valid_until: null,
  },
};
const page = (results: unknown[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
const catalog = {
  [LISTS]: page([old, regular, cash]),
  [PRODUCTS]: page([
    { id: "pr1", name: "Laptop" },
    { id: "pr2", name: "Mouse" },
    { id: "pr3", name: "Baño y corte", tracks_inventory: false },
  ]),
};
const screenOf = (tenant: SelfContext = seller) =>
  renderApp(
    <TenantProvider value={tenant}>
      <ListPrices />
    </TenantProvider>,
  );
const rows = async () =>
  within(await screen.findByRole("list", { name: "Precios de una lista" })).getAllByRole(
    "listitem",
  );

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => vi.unstubAllGlobals());

describe("ListPrices", () => {
  it("se abre en la predeterminada y solo ofrece las listas activas, por orden", async () => {
    const api = mockApi({ ...catalog, [PRICES("p1")]: page([priced]) });
    screenOf();
    await rows();
    const select = screen.getByLabelText("Lista de precios");
    expect(select).toHaveValue("p1");
    const options = within(select)
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(options).toEqual(["Contado", "Regular (predeterminada)"]);
    expect(api.mock.calls.map(([url]) => String(url))).not.toContain(PRICES("p3").slice(4));
  });

  it("enseña lo que resuelve el motor con «IGV incl.», desde cuándo, hasta cuándo y el próximo, en hora de Lima", async () => {
    mockApi({ ...catalog, [PRICES("p1")]: page([priced]) });
    screenOf();
    const [row] = await rows();
    expect(row).toHaveTextContent("Laptop15 pulgadasLAP-15-512");
    expect(within(row!).getByText(/^S\/\s4,589\.00$/)).toBeVisible();
    expect(within(row!).getByText("IGV incl.")).toBeVisible();
    expect(row).not.toHaveTextContent("Precio en la lista");
    expect(row).toHaveTextContent("Vigente desde el 20/10/2026 10:30");
    expect(row).toHaveTextContent("Vale hasta el 01/11/2026 00:00");
    expect(row).toHaveTextContent(/Próximo cambio: S\/\s4,299\.00 desde el 01\/11 00:00/);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("«Sin precio» y el motivo de una variante que no está a la venta", async () => {
    mockApi({ ...catalog, [PRICES("p1")]: page([unpriced, unavailable]) });
    screenOf();
    const [first, second] = await rows();
    expect(first).toHaveTextContent("Sin precio");
    expect(first).not.toHaveTextContent("Vale");
    expect(second).toHaveTextContent(
      "No está a la venta: la variante o su producto no están activos",
    );
    expect(second).toHaveTextContent(/Precio en la lista: S\/\s4,599\.00 · Vigente desde/);
    expect(second).not.toHaveTextContent("IGV incl.");
  });

  it("filtra por producto, también en «Cargar más», y cambia de lista", async () => {
    const api = mockApi({
      ...catalog,
      [PRICES("p1")]: page([priced]),
      [PRICES("p1", "?product_id=pr2")]: page([unpriced], "abc"),
      [PRICES("p1", "?product_id=pr2&cursor=abc")]: page([unavailable]),
      [PRICES("p2", "?product_id=pr2")]: page([priced]),
    });
    screenOf();
    await rows();
    await screen.findByRole("option", { name: "Mouse" });
    fireEvent.change(screen.getByLabelText("Producto"), { target: { value: "pr2" } });
    expect(await screen.findByText("Sin precio")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByText(/No está a la venta/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("Lista de precios"), { target: { value: "p2" } });
    expect(await screen.findByText(/^S\/\s4,589\.00$/)).toBeVisible();
    const urls = api.mock.calls.map(([url]) => `GET ${String(url)}`);
    expect(urls).toContain(PRICES("p1", "?product_id=pr2&cursor=abc"));
    expect(urls).toContain(PRICES("p2", "?product_id=pr2"));
  });

  it("sin predeterminada se abre en la primera activa, y lee las listas de todas sus páginas", async () => {
    mockApi({
      ...catalog,
      [LISTS]: page([old, { ...regular, is_default: false }], "c2"),
      [`${LISTS}&cursor=c2`]: page([cash]),
      [PRICES("p2")]: page([priced]),
    });
    screenOf();
    await rows();
    const select = screen.getByLabelText("Lista de precios");
    expect(select).toHaveValue("p2");
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Contado", "Regular"]);
  });

  it("«Vale sin fecha de fin», y el próximo cambio de una variante aún sin precio", async () => {
    const open = { ...priced, next: null, resolution: { ...priced.resolution, valid_until: null } };
    const first = { ...unpriced, next: priced.next };
    mockApi({ ...catalog, [PRICES("p1")]: page([open, first]) });
    screenOf();
    const [one, two] = await rows();
    expect(one).toHaveTextContent("Vale sin fecha de fin");
    expect(one).not.toHaveTextContent("Próximo cambio");
    expect(two).toHaveTextContent(
      /^.*Sin precioPróximo cambio: S\/\s4,299\.00 desde el 01\/11 00:00$/,
    );
  });

  it("los productos tienen su propia clave: los del inventario (solo con stock) no los tapan", async () => {
    mockApi({ ...catalog, [PRICES("p1")]: page([priced]) });
    const { client } = screenOf();
    expect(await screen.findByRole("option", { name: "Baño y corte" })).toBeInTheDocument();
    expect(client.getQueryData([...getProductsListQueryKey("acme"), "all"])).toBeUndefined();
  });

  it("sin listas activas, o sin permiso para verlas, lo dice", async () => {
    mockApi({ ...catalog, [LISTS]: page([old]) });
    const none = screenOf();
    expect(await screen.findByText(/No hay ninguna lista de precios activa/)).toBeVisible();
    none.unmount();
    vi.unstubAllGlobals();
    mockApi({ ...catalog, [LISTS]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    screenOf();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver las listas de precios",
    );
  });
});
