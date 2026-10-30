import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Promotion, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Promotions } from "./promotions";

const API = "/api/v1/o/acme";
const LIST = `GET ${API}/pricing/promotions/`;
const ONE = (id: string, verb = "") => `${API}/pricing/promotions/${id}/${verb ? `${verb}/` : ""}`;
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants("prices.view", "products.view", ...codes),
});
const promo = (id: string, name: string, more: Partial<Promotion> = {}): Promotion => ({
  ...{ id, name, description: "Liquidación\nde octubre", terms: "Hasta agotar stock" },
  ...{ discount_type: "PERCENT_OFF", value: "10.00", currency: "PEN", priority: 5 },
  ...{ starts_at: "2026-11-01T02:00:00Z", ends_at: "2026-11-15T14:30:00Z", status: "DRAFT" },
  ...{ ai_exposable: true, channels: null, version: 1, created_at: "2026-10-30T10:00:00Z" },
  ...{ created_by_user_id: null, price_list_ids: ["l1"], ...more },
  items: more.items ?? [{ brand_id: "b1" }, { variant_id: "v1", override_value: "50.00" }],
});
const draft = promo("d1", "Cyber");
const live = promo("p1", "Fiestas", {
  ...{ status: "PUBLISHED", discount_type: "AMOUNT_OFF", value: "50.00", version: 2 },
  items: [{ product_id: "zz-desconocido" }],
});
const dead = promo("c1", "Antigua", {
  ...{ status: "CANCELLED", discount_type: "FIXED_PRICE", value: "4399.00", channels: ["web"] },
  items: [{ category_id: "k1" }],
});
const page = (results: unknown[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
const base = {
  [`GET ${API}/pricing/price-lists/?limit=200`]: page([{ id: "l1", name: "Regular" }]),
  [`GET ${API}/brands/?limit=200`]: page([{ id: "b1", name: "Acme Pet" }]),
  [`GET ${API}/products/?limit=200`]: page([{ id: "pr1", name: "Laptop" }]),
  [`GET ${API}/products/pr1/variants/?limit=200`]: page([
    { id: "v1", sku: "LAP-15", name: "Laptop 15 pulgadas" },
  ]),
};
const urls = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.map(([url, init]) => `${init?.method ?? "GET"} ${String(url)}`);
function screenOf(routes: Parameters<typeof mockApi>[0], ...codes: string[]) {
  const api = mockApi({ ...base, ...routes });
  renderApp(
    <TenantProvider value={tenant(...codes)}>
      <Promotions />
    </TenantProvider>,
  );
  return api;
}
async function panelOf(name: string) {
  const open = await screen.findByRole("button", {
    name: `Ver el detalle de la promoción ${name}`,
  });
  open.focus();
  fireEvent.click(open);
  return { open, panel: await screen.findByRole("dialog", { name }) };
}

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
  vi.useFakeTimers({ toFake: ["Date"] }); // «ahora» fijo: la vigencia del borrador empieza después
  vi.setSystemTime(new Date("2026-10-30T12:00:00Z"));
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  cleanup();
});

describe("Promotions", () => {
  it("lista nombre, estado con texto, descuento, vigencia en hora de Lima, listas y prioridad", async () => {
    screenOf({ [LIST]: page([draft, live, dead]) }, "prices.manage");
    const items = within(await screen.findByRole("list", { name: "Promociones" })).getAllByRole(
      "listitem",
    );
    expect(items[0]).toHaveTextContent(
      "Cyber10 %Vigencia: 31/10/2026 21:00 a 15/11/2026 09:30 (zona America/Lima)Listas: RegularBorradorPrioridad 5",
    );
    expect(items[1]).toHaveTextContent(/S\/\s50\.00 menos.*Publicada/);
    expect(items[2]).toHaveTextContent(/Precio fijo S\/\s4,399\.00.*Cancelada/);
  });

  it("sin promociones, con filtro vacío, con error y reintento", async () => {
    screenOf({ [LIST]: page([]) });
    expect(await screen.findByText("No hay promociones que mostrar.")).toBeVisible();
    cleanup();
    let fail = true;
    screenOf({
      [LIST]: () => (fail ? { status: 500, body: { code: "INTERNAL_ERROR" } } : page([live])),
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByText("Fiestas")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Estado"), { target: { value: "DRAFT" } });
    expect(await screen.findByText("No hay promociones que mostrar.")).toBeVisible();
  });

  it("el detalle da términos, objetivos por nombre con «descuento propio», listas y canales", async () => {
    screenOf({ [LIST]: page([draft, dead]) }, "prices.manage");
    const { open, panel } = await panelOf("Cyber");
    expect(await within(panel).findByText("Marca: Acme Pet")).toBeVisible();
    expect(
      within(panel).getByText(/Variante: Laptop 15 pulgadas \(LAP-15\) · descuento propio: 50 %/),
    ).toBeVisible();
    expect(panel).toHaveTextContent(/Hasta agotar stock.*Todos los canales.*Visible a la IA\s*Sí/);
    fireEvent.click(within(panel).getByRole("button", { name: "Cerrar" }));
    expect(open).toHaveFocus();
  });

  it("sin `prices.manage` no hay acciones; un objetivo sin nombre muestra un código corto", async () => {
    screenOf({ [LIST]: page([live]) });
    const { panel } = await panelOf("Fiestas");
    expect(await within(panel).findByText("Producto: #zz-desco")).toBeVisible();
    expect(await within(panel).findByText(/No se pudieron leer algunos nombres/)).toBeVisible();
    expect(within(panel).queryByRole("button", { name: /Publicar|Cancelar/ })).toBeNull();
  });

  it("publicar pide confirmación con su consecuencia y muestra el estado nuevo", async () => {
    const api = screenOf(
      {
        [LIST]: page([draft]),
        [`POST ${ONE("d1", "publish")}`]: {
          status: 200,
          body: { ...draft, status: "PUBLISHED", version: 2 },
        },
      },
      "prices.manage",
    );
    const { panel } = await panelOf("Cyber");
    expect(within(panel).queryByRole("button", { name: "Sí, publicar" })).toBeNull();
    fireEvent.click(within(panel).getByRole("button", { name: "Publicar" }));
    expect(panel).toHaveTextContent(
      "Empezará a aplicarse a los precios desde 31/10/2026 21:00 y rige hasta 15/11/2026 09:30 (zona America/Lima). Una vez publicada, sus términos no se pueden cambiar.",
    );
    expect(urls(api).some((url) => url.startsWith("POST"))).toBe(false);
    fireEvent.click(within(panel).getByRole("button", { name: "Volver" }));
    expect(within(panel).getByRole("button", { name: "Publicar" })).toBeVisible();
    fireEvent.click(within(panel).getByRole("button", { name: "Publicar" }));
    const yes = within(panel).getByRole("button", { name: "Sí, publicar" });
    fireEvent.click(yes);
    fireEvent.click(yes); // el segundo clic no envía otra vez
    expect(await within(panel).findByText("Promoción publicada.")).toBeInTheDocument();
    expect(urls(api).filter((url) => url.startsWith("POST"))).toHaveLength(1);
    await waitFor(() => expect(urls(api).filter((url) => url === LIST)).toHaveLength(2));
    expect(panel).toHaveTextContent("Publicada");
    expect(within(panel).queryByRole("button", { name: "Publicar" })).toBeNull();
    expect(within(panel).getByRole("button", { name: "Cancelar promoción" })).toBeVisible();
  });

  it("con un filtro activo, el panel sigue abierto aunque la fila salga de la vista", async () => {
    let published = false;
    screenOf(
      {
        [LIST]: () => page(published ? [live] : [draft, live]),
        [`POST ${ONE("d1", "publish")}`]: () => {
          published = true;
          return { status: 200, body: { ...draft, status: "PUBLISHED", version: 2 } };
        },
      },
      "prices.manage",
    );
    expect(await screen.findByText("Fiestas")).toBeVisible(); // la publicada se ve sin filtro
    fireEvent.change(screen.getByLabelText("Estado"), { target: { value: "DRAFT" } });
    await waitFor(() => expect(screen.queryByText("Fiestas")).toBeNull());
    const { panel } = await panelOf("Cyber");
    for (const name of ["Publicar", "Sí, publicar"]) {
      fireEvent.click(within(panel).getByRole("button", { name }));
    }
    expect(await within(panel).findByText("Promoción publicada.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("list", { name: "Promociones" })).toBeNull());
    expect(panel).toBeVisible();
    expect(panel).toHaveTextContent("Publicada");
    fireEvent.click(within(panel).getByRole("button", { name: "Cerrar" }));
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();
  });

  it("cancelar dice que es inmediato e irreversible y deja sin acciones", async () => {
    screenOf(
      {
        [LIST]: page([live]),
        [`POST ${ONE("p1", "cancel")}`]: {
          status: 200,
          body: { ...live, status: "CANCELLED", version: 3 },
        },
      },
      "prices.manage",
    );
    const { panel } = await panelOf("Fiestas");
    expect(within(panel).queryByRole("button", { name: "Publicar" })).toBeNull();
    fireEvent.click(within(panel).getByRole("button", { name: "Cancelar promoción" }));
    expect(panel).toHaveTextContent("Dejará de aplicarse de inmediato y no se puede reactivar.");
    fireEvent.click(within(panel).getByRole("button", { name: "Sí, cancelar promoción" }));
    expect(await within(panel).findByText("Promoción cancelada.")).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: /Publicar|Cancelar promoción/ })).toBeNull();
  });

  it.each([
    ["publish", 409, "PROMOTION_NOT_DRAFT", "ya se publicó o se canceló"],
    ["publish", 409, "PROMOTION_ENDED", "ya terminó"],
    ["cancel", 409, "PROMOTION_ALREADY_CANCELLED", "ya estaba cancelada"],
    ["cancel", 404, "NOT_FOUND", "No encontramos"],
  ])(
    "el %s con %s (%s) se explica y se vuelve a leer el estado",
    async (verb, status, code, text) => {
      screenOf(
        {
          [LIST]: page([draft]),
          [`POST ${ONE("d1", verb)}`]: { status, body: { code } },
          [`GET ${ONE("d1")}`]: {
            status: 200,
            body: { ...draft, status: "CANCELLED", version: 4 },
          },
        },
        "prices.manage",
      );
      const { panel } = await panelOf("Cyber");
      const [ask, yes] =
        verb === "publish"
          ? ["Publicar", "Sí, publicar"]
          : ["Cancelar promoción", "Sí, cancelar promoción"];
      for (const name of [ask, yes]) fireEvent.click(within(panel).getByRole("button", { name }));
      expect(await within(panel).findByRole("alert")).toHaveTextContent(text);
      await waitFor(() => expect(panel).toHaveTextContent("Cancelada"));
      expect(
        within(panel).queryByRole("button", { name: /Publicar|Cancelar promoción/ }),
      ).toBeNull();
    },
  );
});
