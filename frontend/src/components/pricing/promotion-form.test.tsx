import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Promotions } from "./promotions";

const API = "/api/v1/o/acme";
const LIST = `GET ${API}/pricing/promotions/`;
const CREATE = `POST ${API}/pricing/promotions/`;
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants("prices.view", "products.view", ...codes),
});
const made = {
  ...{ id: "n1", name: "Lunes", description: "", terms: "", discount_type: "PERCENT_OFF" },
  ...{ value: "10", currency: "PEN", priority: 0, status: "DRAFT", ai_exposable: false },
  ...{ starts_at: "2026-11-02T05:00:00Z", ends_at: "2026-11-09T04:59:00Z", channels: null },
  ...{ price_list_ids: ["l1"], items: [{ category_id: "k1" }] },
};
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const list = (id: string, name: string) => ({ id, name, currency: "PEN", is_active: true });
const lists = [{ ...list("l1", "Regular"), is_default: true }, list("l2", "Mayorista")];
const SEARCH = `GET ${API}/categories/?q=ropa&limit=8`;
const base = {
  [LIST]: page([]),
  [`GET ${API}/pricing/price-lists/?limit=200`]: page(lists),
  [SEARCH]: page([{ id: "k1", name: "Ropa de abrigo" }]),
  [`GET ${API}/variants/?q=cama&limit=8`]: page([
    { id: "v1", sku: "CAM-1", name: "Cama M", product_id: "p1", product_name: "Cama" },
  ]),
};
const urls = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.map(([url, init]) => `${init?.method ?? "GET"} ${String(url)}`);
const bodies = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls
    .filter(([, init]) => init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init?.body)));
function screenOf(routes: Parameters<typeof mockApi>[0] = {}, ...codes: string[]) {
  const api = mockApi({ ...base, ...routes });
  renderApp(
    <TenantProvider value={tenant(...codes)}>
      <Promotions />
    </TenantProvider>,
  );
  return api;
}
const type = (label: string | RegExp, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
async function open() {
  fireEvent.click(await screen.findByRole("button", { name: "Nueva promoción" }));
  await waitFor(() => expect(screen.getByLabelText("Regular (PEN)")).toBeInTheDocument());
  const form = await screen.findByRole("dialog", { name: "Nueva promoción" });
  type("Nombre", "Lunes");
  type("Porcentaje de descuento (%)", "10");
  type("Empieza", "2026-11-02T00:00");
  type("Termina", "2026-11-08T23:59");
  return form;
}
async function pick(text: string, found: string) {
  const inputs = screen.getAllByRole("searchbox");
  fireEvent.change(inputs[inputs.length - 1]!, { target: { value: text } });
  fireEvent.click(await screen.findByRole("button", { name: found }));
}
const submit = () => fireEvent.click(screen.getByRole("button", { name: "Crear promoción" }));

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
  cleanup();
});

describe("PromotionForm", () => {
  it("el botón solo se ofrece con `prices.manage`", async () => {
    screenOf();
    expect(await screen.findByText("No hay promociones que mostrar.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Nueva promoción" })).toBeNull();
  });

  it.each([
    ["PERCENT_OFF", "Porcentaje (%)", "Porcentaje de descuento (%)", "10"],
    ["AMOUNT_OFF", "Monto (S/)", "Monto que se descuenta (S/)", "12.50"],
    ["FIXED_PRICE", "Precio fijo (S/)", "Precio fijo (S/)", "4399"],
  ])("%s: arma el cuerpo con la hora de Lima en UTC", async (kind, radio, label, value) => {
    const api = screenOf({ [CREATE]: { status: 201, body: made } }, "prices.manage");
    await open();
    fireEvent.click(screen.getByRole("radio", { name: radio }));
    fireEvent.change(screen.getByRole("textbox", { name: label }), { target: { value } });
    fireEvent.click(screen.getByLabelText("Mayorista (PEN)")); // dos listas
    type("Términos (opcional)", "Hasta agotar stock");
    await pick("ropa", "Ropa de abrigo");
    submit();
    await waitFor(() => expect(bodies(api)).toHaveLength(1));
    expect(bodies(api)[0]).toEqual({
      ...{ name: "Lunes", discount_type: kind, value, currency: "PEN" },
      ...{ starts_at: "2026-11-02T05:00:00.000Z", ends_at: "2026-11-09T04:59:00.000Z" },
      ...{ terms: "Hasta agotar stock" },
      ...{ price_list_ids: ["l1", "l2"], items: [{ category_id: "k1" }] },
    });
  });

  it("sin nada elegido avisa en el navegador y no envía", async () => {
    const api = screenOf({}, "prices.manage");
    fireEvent.click(await screen.findByRole("button", { name: "Nueva promoción" }));
    await screen.findByLabelText("Regular (PEN)");
    submit();
    expect(await screen.findByText("Escribe el nombre, de hasta 200 caracteres.")).toBeVisible();
    expect(screen.getByText("Elige cuándo empieza.")).toBeVisible();
    expect(screen.getByText("Elige un objetivo que exista, sin repetir.")).toBeVisible();
    expect(screen.getByLabelText("Nombre")).toHaveFocus();
    expect(bodies(api)).toHaveLength(0);
  });

  it("filas de objetivos: agregar, quitar, descuento propio y error del servidor en su fila", async () => {
    const fields = { name: ["x"], items: { "1": { variant_id: ["no existe"] } } };
    const api = screenOf(
      { [CREATE]: { status: 400, body: { code: "VALIDATION_ERROR", fields } } },
      "prices.manage",
    );
    await open();
    await pick("ropa", "Ropa de abrigo");
    fireEvent.click(screen.getByRole("button", { name: "Agregar otro objetivo" }));
    fireEvent.change(screen.getByLabelText("Tipo del objetivo 2"), {
      target: { value: "variant_id" },
    });
    await pick("cama", "Cama · Cama M (CAM-1)");
    fireEvent.change(screen.getAllByLabelText(/Descuento propio/)[0]!, { target: { value: "15" } });
    submit();
    expect(await screen.findByText("Elige un objetivo que exista, sin repetir.")).toBeVisible();
    expect(screen.getByText("Escribe el nombre, de hasta 200 caracteres.")).toBeVisible();
    expect(bodies(api)[0].items).toEqual([
      { category_id: "k1", override_value: "15" },
      { variant_id: "v1" },
    ]);
    fireEvent.click(screen.getByRole("button", { name: "Quitar el objetivo 2" }));
    expect(screen.queryByLabelText("Tipo del objetivo 2")).toBeNull();
    expect(screen.queryByText("Elige un objetivo que exista, sin repetir.")).toBeNull();
    expect(screen.getByRole("button", { name: "Quitar el objetivo 1" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("al crear: cierra, avisa, relee la lista y abre el detalle del borrador", async () => {
    const api = screenOf({ [CREATE]: { status: 201, body: made } }, "prices.manage");
    await open();
    await pick("ropa", "Ropa de abrigo");
    const send = screen.getByRole("button", { name: "Crear promoción" });
    fireEvent.click(send);
    fireEvent.click(send); // el segundo envío no sale: una sola escritura
    const panel = await screen.findByRole("dialog", { name: "Lunes" });
    expect(within(panel).getByRole("button", { name: "Publicar" })).toBeVisible();
    expect(screen.getByText("Promoción creada en borrador.")).toBeVisible();
    expect(screen.queryByRole("dialog", { name: "Nueva promoción" })).toBeNull();
    await waitFor(() => expect(urls(api).filter((url) => url === LIST)).toHaveLength(2));
    expect(bodies(api)).toHaveLength(1);
  });

  it("el buscador espera a que se deje de escribir y pide pocos resultados", async () => {
    const api = screenOf({}, "prices.manage");
    await open();
    const box = screen.getAllByRole("searchbox")[0]!;
    for (const text of ["r", "ro", "rop", "ropa"])
      fireEvent.change(box, { target: { value: text } });
    expect(await screen.findByRole("button", { name: "Ropa de abrigo" })).toBeVisible();
    const searches = urls(api).filter((url) => url.includes("/categories/?q="));
    expect(searches).toEqual([SEARCH]); // una sola petición, con `limit=8`
  });
});
