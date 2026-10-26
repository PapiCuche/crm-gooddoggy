import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import { getProductsListQueryKey } from "@/lib/api/client";
import type { ListPrice, PriceList, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { ListPrices } from "./list-prices";
import { startProblem } from "./price-form";

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
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

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

const POST = `POST ${API}/pricing/price-lists/p1/prices/`;
const owner = { ...seller, permissions: grants("prices.view", "products.view", "prices.manage") };
const created = (amount: string, starts_at = "2026-10-26T15:00:00Z") => ({
  status: 201,
  body: { id: "x1", variant_id: "v1", price_list_id: "p1", amount, starts_at },
});
const form = () => screen.getByRole("form");
const field = (label: string) => within(form()).getByLabelText(label);
const AMOUNT = "Importe en soles, IGV incluido";
function press(name: string | RegExp) {
  const button = screen.getByRole("button", { name });
  button.focus(); // como una pulsación real: el botón recibe el foco antes del clic
  fireEvent.click(button);
}
const fill = (values: Record<string, string>) => {
  for (const [label, value] of Object.entries(values)) {
    fireEvent.input(field(label), { target: { value } });
  }
};
const posted = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls
    .filter(([, init]) => init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init?.body)));
const gets = (api: ReturnType<typeof mockApi>, url: string) =>
  api.mock.calls.filter(
    ([called, init]) => init?.method === "GET" && `GET ${String(called)}` === url,
  ).length;
async function opened(routes: Parameters<typeof mockApi>[0] = {}, row: ListPrice = priced) {
  const api = mockApi({ ...catalog, [PRICES("p1")]: page([row]), ...routes });
  screenOf(owner);
  await rows();
  press("Cambiar precio de Laptop 15 pulgadas (LAP-15-512)");
  return api;
}

describe("PriceForm", () => {
  it("sin `prices.manage` no hay botones; con él, uno por fila", async () => {
    mockApi({ ...catalog, [PRICES("p1")]: page([priced, unpriced]) });
    screenOf(owner);
    expect(await screen.findAllByRole("button", { name: /^Cambiar precio de/ })).toHaveLength(2);
  });

  it("comprueba el importe antes de enviar y enseña la diferencia con el vigente", async () => {
    const api = await opened();
    expect(field(AMOUNT)).toHaveFocus();
    expect(field(AMOUNT)).toHaveAttribute("inputmode", "decimal");
    for (const wrong of ["4399.555", "0", "4.399,50", "4399.", "1234567890123"]) {
      fill({ [AMOUNT]: wrong, Motivo: "Promoción" });
      press("Guardar precio");
      expect(await within(form()).findByText(/con punto y como mucho 2 decimales\./)).toBeVisible();
    }
    fill({ [AMOUNT]: "4399.5" });
    expect(form()).toHaveTextContent(/Diferencia con el vigente: -S\/\s199\.50 \(-4\.3\s?%\)/);
    fill({ [AMOUNT]: "4800" });
    expect(form()).toHaveTextContent(/Diferencia con el vigente: \+S\/\s201\.00 \(\+4\.4\s?%\)/);
    fill({ Motivo: "ab" });
    press("Guardar precio");
    expect(await within(form()).findByText(/De 3 a 500|de 3 a 500/)).toBeVisible();
    expect(posted(api)).toEqual([]);
  });

  it("guarda desde ahora con el importe en string, una vez por pulsación, y vuelve a pedir la página", async () => {
    const api = await opened({ [POST]: created("4399.50") });
    fill({ [AMOUNT]: "4399.5", Motivo: "  Promoción  " });
    // Enter mantenido no se repite.
    const held = new KeyboardEvent("keydown", {
      key: "Enter",
      repeat: true,
      bubbles: true,
      cancelable: true,
    });
    field(AMOUNT).dispatchEvent(held);
    expect(held.defaultPrevented).toBe(true);
    fireEvent.submit(form());
    fireEvent.submit(form());
    expect(
      await screen.findByText(/Precio de LAP-15-512 guardado: S\/\s4,399\.50 desde ahora\./),
    ).toBeVisible();
    expect(posted(api)).toEqual([{ variant_id: "v1", amount: "4399.5", reason: "Promoción" }]);
    expect(gets(api, PRICES("p1"))).toBe(2);
    expect(screen.queryByRole("form")).toBeNull();
  });

  it("programa en la hora de la organización y no acepta una fecha pasada", async () => {
    const api = await opened({ [POST]: created("4299.00", "2099-11-01T05:00:00Z") });
    fill({ [AMOUNT]: "4299", Motivo: "Campaña" });
    fireEvent.click(within(form()).getByLabelText("Una fecha y hora futura"));
    press("Programar precio");
    expect(await within(form()).findByText("Elige la fecha y la hora.")).toBeVisible();
    fireEvent.change(field("Fecha y hora"), { target: { value: "2020-01-01T10:00" } });
    press("Programar precio");
    expect(await within(form()).findByText("Tiene que ser una fecha y hora futura.")).toBeVisible();
    expect(posted(api)).toEqual([]);
    fireEvent.change(field("Fecha y hora"), { target: { value: "2099-11-01T00:00" } });
    press("Programar precio");
    expect(
      await screen.findByText(/programado: S\/\s4,299\.00 desde el 01\/11\/2099 00:00\./),
    ).toBeVisible();
    expect(posted(api)[0]).toMatchObject({ amount: "4299", starts_at: "2099-11-01T05:00:00.000Z" });
  });

  it.each([
    ["PRICE_UNCHANGED", "Es el precio que ya rige en ese momento"],
    ["PRICE_ALREADY_SCHEDULED", "Ya hay un precio que empieza en ese instante"],
    ["PRICE_CONFLICT", "Otro cambio de precio de esta variante en esta lista llegó antes"],
  ])("un 409 %s se explica con su texto y vuelve a pedir la tabla", async (code, text) => {
    const api = await opened({ [POST]: { status: 409, body: { code } } });
    fill({ [AMOUNT]: "4599", Motivo: "Sin cambio" });
    press("Guardar precio");
    expect(await within(form()).findByRole("alert")).toHaveTextContent(text);
    // El de «ya rige» es del importe: va junto a él.
    expect(field(AMOUNT).getAttribute("aria-invalid")).toBe(
      code === "PRICE_UNCHANGED" ? "true" : null,
    );
    await waitFor(() => expect(gets(api, PRICES("p1"))).toBe(2));
  });

  it("un 400 se enseña junto a su campo; el de `starts_at`, en el formulario", async () => {
    const replies = [
      { status: 400, body: { code: "VALIDATION_ERROR", fields: { reason: ["x"] } } },
      { status: 400, body: { code: "VALIDATION_ERROR", fields: { starts_at: ["x"] } } },
    ];
    await opened({ [POST]: () => replies.shift()! });
    fill({ [AMOUNT]: "4399", Motivo: "Promoción" });
    press("Guardar precio");
    expect(await within(form()).findByText(/El motivo tiene de 3 a 500/)).toBeVisible();
    fill({ Motivo: "Promoción!" });
    press("Guardar precio");
    expect(
      await within(form()).findByText("Tiene que ser una fecha y hora futura."),
    ).toHaveAttribute("role", "alert");
  });

  it("un 404 cierra el formulario, lo explica y vuelve a pedir listas y precios", async () => {
    const api = await opened({ [POST]: { status: 404, body: { code: "NOT_FOUND" } } });
    fill({ [AMOUNT]: "4399", Motivo: "Promoción" });
    press("Guardar precio");
    expect(await screen.findByRole("alert")).toHaveTextContent(/ya no está disponible/);
    expect(screen.queryByRole("form")).toBeNull();
    expect(screen.getByRole("heading", { name: "Precios de una lista" })).toHaveFocus();
    await waitFor(() => expect(gets(api, PRICES("p1"))).toBe(2));
    expect(gets(api, LISTS)).toBe(2);
  });

  it("cuenta en céntimos, sin `float`, y cancelar devuelve el foco al botón", async () => {
    await opened({}, { ...priced, current: { ...priced.current!, amount: "4.36" } });
    fill({ [AMOUNT]: "4.35" });
    expect(form()).toHaveTextContent(/Diferencia con el vigente: -S\/\s0\.01 \(-0\.2\s?%\)/);
    press("Cancelar");
    expect(screen.queryByRole("form")).toBeNull();
    expect(screen.getByRole("button", { name: /^Cambiar precio de/ })).toHaveFocus();
  });

  it("lee «Desde» en hora de Lima y compara con el precio que regirá entonces", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-26T15:00:00Z")); // 10:00 en Lima
    const api = await opened({ [POST]: created("4199.00", "2026-10-26T17:00:00Z") });
    fill({ [AMOUNT]: "4199", Motivo: "abc" });
    fireEvent.click(within(form()).getByLabelText("Una fecha y hora futura"));
    expect(field("Fecha y hora")).toHaveAttribute("max", "9999-12-31T23:59");
    // Después del próximo cambio (01/11 00:00), la base es el programado.
    fireEvent.change(field("Fecha y hora"), { target: { value: "2026-11-02T10:00" } });
    expect(form()).toHaveTextContent(
      /Diferencia con el programado que regirá entonces: -S\/\s100\.00 \(-2\.3\s?%\)/,
    );
    // Dos horas más tarde en Lima: futuro, aunque leído en UTC ya habría pasado.
    fireEvent.change(field("Fecha y hora"), { target: { value: "2026-10-26T12:00" } });
    expect(form()).toHaveTextContent(/Diferencia con el vigente: -S\/\s400\.00/);
    press("Programar precio");
    expect(
      await screen.findByText(/programado: S\/\s4,199\.00 desde el 26\/10\/2026 12:00\./),
    ).toBeVisible();
    expect(posted(api)).toEqual([
      { variant_id: "v1", amount: "4199", reason: "abc", starts_at: "2026-10-26T17:00:00.000Z" },
    ]);
  });

  it("un «Desde» ilegible (año de cinco cifras) no se envía", () => {
    const now = new Date("2026-10-26T15:00:00Z");
    expect(startProblem("20266-01-01T10:00", "America/Lima", now)).toBe("atInvalid");
    expect(startProblem("", "America/Lima", now)).toBe("atRequired");
    expect(startProblem("2026-10-26T09:59", "America/Lima", now)).toBe("atPast");
    expect(startProblem("2026-10-26T10:01", "America/Lima", now)).toBeNull();
  });
});
