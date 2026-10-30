import { onlineManager } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
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
// Respuestas retenidas hasta que el test las suelte: "MÉTODO ruta" → cómo soltarla.
const gates = new Map<string, Promise<Response>>();
const held = new Map<string, (reply: { status: number; body?: unknown }) => void>();
const hold = (key: string) =>
  gates.set(
    key,
    new Promise<Response>((resolve) =>
      held.set(key, ({ status, body }) => resolve(new Response(JSON.stringify(body), { status }))),
    ),
  );
const release = (key: string, status: number, body?: unknown) => held.get(key)!({ status, body });
function screenOf(routes: Parameters<typeof mockApi>[0] = {}, ...codes: string[]) {
  const real = mockApi({ ...base, ...routes });
  const api = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const gate = gates.get(`${init?.method ?? "GET"} ${String(input)}`);
    return gate ? gate : real(input, init);
  });
  vi.stubGlobal("fetch", api);
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
  onlineManager.setOnline(true);
  vi.useRealTimers();
  held.clear();
  gates.clear();
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

const PCT = "Escribe un porcentaje mayor que 0 y hasta 100, con hasta 2 decimales.";
const AMOUNT = "Escribe un número mayor que 0, con hasta 2 decimales.";
const ROW = "Elige un objetivo que exista, sin repetir.";
const created = { [CREATE]: { status: 201, body: made } };
const rejected = (fields: unknown) => ({
  [CREATE]: { status: 400, body: { code: "VALIDATION_ERROR", fields } },
});
// Con el formulario lleno y una categoría elegida; solo falta enviar.
async function ready(routes: Parameters<typeof mockApi>[0] = created) {
  const api = screenOf(routes, "prices.manage");
  await open();
  await pick("ropa", "Ropa de abrigo");
  return api;
}
// Avanza el reloj simulado dentro de `act`: el temporizador del buscador cambia estado.
const tick = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));
const radio = (name: string) => fireEvent.click(screen.getByRole("radio", { name }));
const searches = (api: ReturnType<typeof mockApi>) =>
  urls(api).filter((url) => url.includes("/categories/?q="));

describe("PromotionForm: validación en el navegador", () => {
  it("Enter en el buscador no envía el formulario", async () => {
    const api = screenOf(created, "prices.manage");
    await open();
    const box = screen.getAllByRole("searchbox")[0]!;
    expect(fireEvent.keyDown(box, { key: "Enter" })).toBe(false); // cancelado: no envía
    expect(bodies(api)).toHaveLength(0);
  });

  it.each(["0", "100.01", "abc"])("porcentaje %s: error en el campo, sin envío", async (value) => {
    const api = await ready();
    type("Porcentaje de descuento (%)", value);
    submit();
    expect(await screen.findByText(PCT)).toBeVisible();
    expect(screen.getByLabelText("Porcentaje de descuento (%)")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(bodies(api)).toHaveLength(0);
  });

  it("el 100 % vale, también como descuento propio, que va como texto", async () => {
    const api = await ready();
    type("Porcentaje de descuento (%)", "100");
    type(/Descuento propio/, "100");
    submit();
    await waitFor(() => expect(bodies(api)).toHaveLength(1));
    expect(bodies(api)[0].value).toBe("100");
    expect(bodies(api)[0].items).toEqual([{ category_id: "k1", override_value: "100" }]);
  });

  it("un monto o precio fijo en 0 no vale", async () => {
    const api = await ready();
    radio("Monto (S/)");
    type("Monto que se descuenta (S/)", "0");
    submit();
    expect(await screen.findByText(AMOUNT)).toBeVisible();
    type("Monto que se descuenta (S/)", "250");
    submit();
    await waitFor(() => expect(bodies(api)).toHaveLength(1));
    expect(bodies(api)[0].value).toBe("250");
  });

  it.each([
    ["PERCENT_OFF", "101", PCT],
    ["PERCENT_OFF", "0", PCT],
    ["AMOUNT_OFF", "0", AMOUNT],
  ])("descuento propio de %s en %s: error en su campo", async (kind, value, message) => {
    const api = await ready();
    if (kind === "AMOUNT_OFF") {
      radio("Monto (S/)");
      type("Monto que se descuenta (S/)", "5");
    }
    const own = screen.getByLabelText(/Descuento propio/);
    fireEvent.change(own, { target: { value } });
    submit();
    expect(await screen.findByText(message)).toBeVisible();
    expect(own).toHaveAttribute("aria-invalid", "true");
    expect(own).toHaveFocus();
    expect(bodies(api)).toHaveLength(0);
  });

  it("el nombre se recorta al enviar y uno en blanco no vale", async () => {
    const api = await ready();
    type("Nombre", "   ");
    submit();
    expect(await screen.findByText("Escribe el nombre, de hasta 200 caracteres.")).toBeVisible();
    expect(bodies(api)).toHaveLength(0);
    type("Nombre", "  Lunes  ");
    submit();
    await waitFor(() => expect(bodies(api)).toHaveLength(1));
    expect(bodies(api)[0].name).toBe("Lunes");
  });

  it("termina debe ser después de empieza, ni igual ni antes", async () => {
    const api = await ready();
    for (const ends of ["2026-11-02T00:00", "2026-11-01T23:59"]) {
      type("Termina", ends);
      submit();
      expect(await screen.findByText(/Elige cuándo termina/)).toBeVisible();
      expect(bodies(api)).toHaveLength(0);
    }
    type("Termina", "2026-11-02T00:01");
    submit();
    await waitFor(() => expect(bodies(api)).toHaveLength(1));
  });

  it.each([
    ["Empieza", "0050-01-01T00:00", "Elige cuándo empieza."],
    ["Termina", "9999-12-31T23:59", "Elige cuándo termina, después del inicio."],
  ])("%s con el año de %s se rechaza en el campo", async (label, value, message) => {
    const api = await ready();
    type(label, value);
    submit();
    expect(await screen.findByText(message)).toBeVisible();
    expect(bodies(api)).toHaveLength(0);
  });

  it("listas de monedas distintas no se mezclan", async () => {
    const dollars = { ...list("l3", "Dólares"), currency: "USD" };
    const api = screenOf(
      { [`GET ${API}/pricing/price-lists/?limit=200`]: page([...lists, dollars]) },
      "prices.manage",
    );
    await open();
    await pick("ropa", "Ropa de abrigo");
    fireEvent.click(screen.getByLabelText("Dólares (USD)"));
    submit();
    expect(await screen.findByText(/Elige de 1 a 20 listas/)).toBeVisible();
    expect(bodies(api)).toHaveLength(0);
    fireEvent.click(screen.getByLabelText("Dólares (USD)"));
    submit();
    await waitFor(() => expect(bodies(api)).toHaveLength(1));
  });

  it("cambiar el tipo de una fila suelta el objetivo y su error", async () => {
    await ready();
    expect(screen.getByText("Categoría: Ropa de abrigo")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Agregar otro objetivo" }));
    submit();
    expect(await screen.findByText(ROW)).toBeVisible(); // la fila 2 sin objetivo
    fireEvent.change(screen.getAllByRole("searchbox")[0]!, { target: { value: "ca" } });
    fireEvent.change(screen.getByLabelText("Tipo del objetivo 2"), {
      target: { value: "brand_id" },
    });
    expect(screen.queryByText(ROW)).toBeNull();
    expect(screen.getAllByRole("searchbox")[0]).toHaveValue(""); // sin el texto anterior
    fireEvent.change(screen.getByLabelText("Tipo del objetivo 1"), {
      target: { value: "brand_id" },
    });
    expect(screen.queryByText("Categoría: Ropa de abrigo")).toBeNull();
    expect(screen.getAllByRole("searchbox")).toHaveLength(2);
  });

  it("no ofrece en una fila lo que ya eligió otra", async () => {
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Agregar otro objetivo" }));
    const second = screen.getAllByRole("searchbox")[0]!;
    fireEvent.change(second, { target: { value: "ropa" } });
    expect(await screen.findByText("Sin resultados.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Ropa de abrigo" })).toBeNull();
  });
});

describe("PromotionForm: listas, esperas y foco", () => {
  const LISTS = `GET ${API}/pricing/price-lists/?limit=200`;

  it("mientras cargan las listas no dice que no hay, y no deja enviar", async () => {
    hold(LISTS);
    const api = screenOf(created, "prices.manage");
    fireEvent.click(await screen.findByRole("button", { name: "Nueva promoción" }));
    expect(await screen.findByText("Cargando las listas de precios…")).toBeVisible();
    expect(screen.queryByText(/No hay listas de precios activas/)).toBeNull();
    submit();
    expect(bodies(api)).toHaveLength(0);
    expect(screen.queryByText(/Elige de 1 a 20 listas/)).toBeNull();
    release(LISTS, 200, { results: lists, next: null });
    expect(await screen.findByLabelText("Regular (PEN)")).toBeVisible();
  });

  it("si fallan las listas lo dice, ofrece reintentar y no habla de «no hay»", async () => {
    let reads = 0;
    const route = () =>
      reads++ ? { status: 200, body: { results: lists, next: null } } : { status: 404 };
    screenOf({ [LISTS]: route }, "prices.manage");
    fireEvent.click(await screen.findByRole("button", { name: "Nueva promoción" }));
    expect(await screen.findByText("No se pudieron cargar las listas de precios.")).toBeVisible();
    expect(screen.queryByText(/No hay listas de precios activas/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByLabelText("Regular (PEN)")).toBeVisible();
  });

  it("sin red, las listas no quedan cargando para siempre", async () => {
    onlineManager.setOnline(false);
    screenOf({}, "prices.manage");
    fireEvent.click(await screen.findByRole("button", { name: "Nueva promoción" }));
    expect(await screen.findByRole("button", { name: "Reintentar" })).toBeVisible();
  });

  it("espera 300 ms sin teclear antes de pedir", async () => {
    const api = screenOf(created, "prices.manage");
    await open();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const box = screen.getAllByRole("searchbox")[0]!;
    fireEvent.change(box, { target: { value: "ro" } });
    await tick(250);
    expect(searches(api)).toEqual([]);
    fireEvent.change(box, { target: { value: "rop" } }); // reinicia la espera
    await tick(250);
    expect(searches(api)).toEqual([]);
    await tick(100);
    expect(searches(api)).toEqual([`GET ${API}/categories/?q=rop&limit=8`]);
  });

  it("una respuesta vieja que llega tarde no pisa la nueva", async () => {
    const [old, current] = [`${API}/categories/?q=ro&limit=8`, `${API}/categories/?q=rop&limit=8`];
    hold(`GET ${old}`);
    hold(`GET ${current}`);
    const api = screenOf(created, "prices.manage");
    await open();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const box = screen.getAllByRole("searchbox")[0]!;
    fireEvent.change(box, { target: { value: "ro" } });
    await tick(350);
    fireEvent.change(box, { target: { value: "rop" } });
    await tick(350);
    expect(searches(api)).toHaveLength(2);
    release(`GET ${current}`, 200, { results: [{ id: "k2", name: "Ropa nueva" }], next: null });
    expect(await screen.findByRole("button", { name: "Ropa nueva" })).toBeVisible();
    release(`GET ${old}`, 200, { results: [{ id: "k9", name: "Ropa vieja" }], next: null });
    await tick(100);
    expect(screen.queryByRole("button", { name: "Ropa vieja" })).toBeNull();
    expect(screen.getByRole("button", { name: "Ropa nueva" })).toBeVisible();
  });

  it("el estado del buscador es siempre `status` y el error va aparte", async () => {
    screenOf({}, "prices.manage");
    await open();
    const box = screen.getAllByRole("searchbox")[0]!;
    submit(); // la fila sin objetivo
    const ids = box.getAttribute("aria-describedby")!.split(" ");
    expect(ids).toHaveLength(2);
    expect(document.getElementById(ids[0]!)).toHaveAttribute("role", "status");
    expect(document.getElementById(ids[1]!)).toHaveAttribute("role", "alert");
    expect(document.getElementById(ids[1]!)).toHaveTextContent(ROW);
  });

  it("un segundo envío con los mismos errores vuelve a llevar el foco", async () => {
    screenOf({}, "prices.manage");
    fireEvent.click(await screen.findByRole("button", { name: "Nueva promoción" }));
    await screen.findByLabelText("Regular (PEN)");
    submit();
    await waitFor(() => expect(screen.getByLabelText("Nombre")).toHaveFocus());
    screen.getByLabelText("Términos (opcional)").focus();
    submit();
    await waitFor(() => expect(screen.getByLabelText("Nombre")).toHaveFocus());
  });

  it("un error del servidor en un campo lleva el foco a ese campo", async () => {
    await ready(rejected({ value: ["mal"] }));
    screen.getByLabelText("Términos (opcional)").focus();
    submit();
    expect(await screen.findByText(PCT)).toBeVisible();
    await waitFor(() => expect(screen.getByLabelText("Porcentaje de descuento (%)")).toHaveFocus());
  });

  it("un error del servidor en una fila ya elegida lleva el foco a su mensaje", async () => {
    await ready(rejected({ items: { "0": { category_id: ["no existe"] } } }));
    screen.getByLabelText("Términos (opcional)").focus();
    submit();
    const message = await screen.findByText(ROW);
    expect(message).toHaveAttribute("data-problem");
    await waitFor(() => expect(message).toHaveFocus());
  });

  it("el error de `override_value` sale en el descuento propio de su fila", async () => {
    await ready(rejected({ items: { "0": { override_value: ["mal"] } } }));
    screen.getByLabelText("Términos (opcional)").focus();
    submit();
    expect(await screen.findByText(PCT)).toBeVisible();
    const own = screen.getByLabelText(/Descuento propio/);
    expect(own).toHaveAttribute("aria-invalid", "true");
    await waitFor(() => expect(own).toHaveFocus());
    expect(screen.queryByText(ROW)).toBeNull();
  });

  it("editar tras un fallo genérico limpia el aviso", async () => {
    const failed = { [CREATE]: { status: 500, body: { code: "INTERNAL_ERROR" } } };
    await ready(failed);
    submit();
    expect(await screen.findByText(/Algo salió mal de nuestro lado/)).toBeVisible();
    type("Nombre", "Martes");
    expect(screen.queryByText(/Algo salió mal de nuestro lado/)).toBeNull();
  });

  it("mientras se envía, los campos no cambian", async () => {
    hold(CREATE);
    const api = await ready();
    submit();
    await waitFor(() => expect(bodies(api)).toHaveLength(1));
    type("Nombre", "Otro");
    fireEvent.click(screen.getByRole("radio", { name: "Monto (S/)" }));
    expect(screen.getByLabelText("Nombre")).toHaveValue("Lunes");
    expect(screen.getByRole("radio", { name: "Porcentaje (%)" })).toBeChecked();
    release(CREATE, 201, made);
    expect(await screen.findByRole("dialog", { name: "Lunes" })).toBeVisible();
  });
});

describe("PromotionForm: prioridad, canales y visibilidad a la IA", () => {
  it("por omisión no se envía ninguno de los tres", async () => {
    const api = screenOf(created, "prices.manage");
    await open();
    expect(screen.getByText("Todos los canales")).toBeVisible();
    await pick("ropa", "Ropa de abrigo");
    submit();
    await waitFor(() => expect(bodies(api)).toHaveLength(1));
    const sent = bodies(api)[0];
    for (const key of ["priority", "channels", "ai_exposable"])
      expect(sent).not.toHaveProperty(key);
  });

  it("envía solo lo que se cambió: canales exactos, sin espacios y con mayúsculas", async () => {
    const api = await ready();
    type("Prioridad (opcional)", "10000");
    const draft = screen.getByLabelText("Agregar un canal");
    fireEvent.change(draft, { target: { value: "  tienda " } });
    expect(fireEvent.keyDown(draft, { key: "Enter" })).toBe(false); // agrega, no envía
    type("Agregar un canal", "Tienda");
    fireEvent.click(screen.getByRole("button", { name: "Agregar canal" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Visible a la IA" }));
    expect(screen.queryByText("Todos los canales")).toBeNull();
    submit();
    await waitFor(() => expect(bodies(api)).toHaveLength(1));
    expect(bodies(api)[0]).toMatchObject({
      ...{ priority: 10000, channels: ["tienda", "Tienda"], ai_exposable: true },
    });
  });

  it("Enter no agrega por composición (IME) ni repetido, y sí una vez", async () => {
    screenOf(created, "prices.manage");
    await open();
    const draft = screen.getByLabelText("Agregar un canal");
    type("Agregar un canal", "web");
    fireEvent.keyDown(draft, { key: "Enter", isComposing: true, keyCode: 229 });
    fireEvent.keyDown(draft, { key: "Enter", repeat: true });
    expect(screen.getByText("Todos los canales")).toBeVisible();
    expect(draft).toHaveValue("web");
    fireEvent.keyDown(draft, { key: "Enter" });
    expect(screen.getAllByRole("button", { name: /Quitar el canal/ })).toHaveLength(1);
  });

  it.each([
    ["0", undefined],
    ["-5", -5],
    ["-10000", -10000],
  ])("prioridad %s se envía como %s", async (value, sent) => {
    const api = await ready();
    type("Prioridad (opcional)", value);
    submit();
    await waitFor(() => expect(bodies(api)).toHaveLength(1));
    expect(bodies(api)[0].priority).toBe(sent);
  });

  it("quitar todos los canales vuelve a «Todos» y no envía lista vacía", async () => {
    const api = await ready();
    type("Agregar un canal", "web");
    fireEvent.click(screen.getByRole("button", { name: "Agregar canal" }));
    fireEvent.click(screen.getByRole("button", { name: "Quitar el canal web" }));
    expect(screen.getByText("Todos los canales")).toBeVisible();
    submit();
    await waitFor(() => expect(bodies(api)).toHaveLength(1));
    expect(bodies(api)[0]).not.toHaveProperty("channels");
  });

  it.each(["10001", "-10001", "1.5", "abc"])("prioridad %s no vale", async (value) => {
    const api = await ready();
    type("Prioridad (opcional)", value);
    submit();
    expect(await screen.findByText("Escribe un entero de -10000 a 10000.")).toBeVisible();
    expect(bodies(api)).toHaveLength(0);
  });

  it("no agrega canales vacíos, repetidos, largos ni más de 10", async () => {
    screenOf(created, "prices.manage");
    await open();
    const add = (name: string) => {
      type("Agregar un canal", name);
      fireEvent.click(screen.getByRole("button", { name: "Agregar canal" }));
    };
    const bad = /Agrega el canal que escribiste/;
    add("   ");
    expect(screen.getByText(bad)).toBeVisible();
    add("web");
    expect(screen.queryByText(bad)).toBeNull();
    add(" web ");
    expect(screen.getByText(bad)).toBeVisible();
    add("x".repeat(31));
    expect(screen.getByText(bad)).toBeVisible();
    add("x".repeat(30));
    expect(screen.queryByText(bad)).toBeNull();
    for (let n = 3; n <= 10; n++) add(`c${n}`);
    expect(screen.queryByText(bad)).toBeNull();
    add("c11");
    expect(screen.getByText(bad)).toBeVisible();
  });

  it("un canal escrito y sin agregar frena el envío", async () => {
    const api = await ready();
    type("Agregar un canal", "web");
    submit();
    expect(await screen.findByText(/Agrega el canal que escribiste/)).toBeVisible();
    expect(screen.getByLabelText("Agregar un canal")).toHaveFocus();
    expect(bodies(api)).toHaveLength(0);
  });

  it.each([
    ["priority", "Escribe un entero de -10000 a 10000.", "Prioridad (opcional)"],
    ["channels", "Agrega el canal que escribiste", "Agregar un canal"],
    ["ai_exposable", "Marca o desmarca «Visible a la IA».", "Visible a la IA"],
  ])("el error del servidor en %s sale en su campo", async (field, message, label) => {
    await ready(rejected({ [field]: ["mal"] }));
    screen.getByLabelText("Términos (opcional)").focus();
    submit();
    expect(await screen.findByText(new RegExp(message))).toBeVisible();
    await waitFor(() => expect(screen.getByLabelText(label)).toHaveFocus());
  });
});
