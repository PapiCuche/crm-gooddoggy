import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext } from "@/lib/api/model";
import { renderApp } from "@/test-utils";

import { StockPanel } from "./stock-panel";

const API = "/api/v1/o/acme";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: ["inventory.view", "inventory.adjust"].map((code) => ({ code, scopes: [] })),
};
const page = (results: unknown[]) => ({ results, next: null });
const READS: Record<string, unknown> = {
  [`${API}/warehouses/?limit=200`]: page([
    { id: "w1", name: "Central", code: "CEN", is_active: true },
    { id: "w2", name: "Surco", code: "SUR", is_active: true },
  ]),
  [`${API}/products/?limit=200`]: page([
    { id: "p1", name: "Collar", tracks_inventory: true },
    { id: "p2", name: "Baño", tracks_inventory: true },
  ]),
  [`${API}/products/p1/variants/?limit=200`]: page([{ id: "v1", name: "Collar", sku: "C-1" }]),
  [`${API}/products/p2/variants/?limit=200`]: page([
    { id: "v2", name: "Baño M", sku: "B-M" },
    { id: "v3", name: "Baño L", sku: "B-L" },
  ]),
};

// Entradas de Collar en Central, con la idempotencia de `stock.move`: la misma clave con lo
// mismo devuelve el de entonces (200); con otra cosa, `IDEMPOTENCY_KEY_REUSED`.
type Sent = { idempotency_key: string; quantity: number; warehouse_id: string };
function fakeServer() {
  const state = { onHand: 0, lose: 0, hold: null as Promise<void> | null, posts: [] as Sent[] };
  const byKey = new Map<string, { same: string; after: number }>();
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url in READS) return json(200, READS[url]);
      if (url === `${API}/inventory/levels/?limit=200&variant_id=v1`) {
        const row = { id: "l1", variant_id: "v1", warehouse_id: "w1", reserved: 0 };
        return json(200, page([{ ...row, on_hand: state.onHand, available: state.onHand }]));
      }
      if (init?.method !== "POST") return json(404, { code: "NOT_FOUND" });
      if (state.hold) await state.hold;
      const body = JSON.parse(String(init.body)) as Sent;
      state.posts.push(body);
      const same = JSON.stringify({ ...body, idempotency_key: "" });
      const moved = (after: number) => ({ ...body, id: "m1", on_hand_after: after });
      const seen = byKey.get(body.idempotency_key);
      if (seen && seen.same !== same) return json(409, { code: "IDEMPOTENCY_KEY_REUSED" });
      if (seen) return json(200, moved(seen.after));
      state.onHand += body.quantity;
      byKey.set(body.idempotency_key, { same, after: state.onHand });
      if (state.lose-- > 0) throw new TypeError("Failed to fetch"); // registrado, sin respuesta
      return json(201, moved(state.onHand));
    }),
  );
  return state;
}

const form = () => screen.getByRole("form", { name: "Nuevo movimiento" });
const type = (label: string, value: string) =>
  fireEvent.input(within(form()).getByLabelText(label), { target: { value } });
function press(name: string, inside: HTMLElement = document.body) {
  const button = within(inside).getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
}
const choose = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
async function receipt(quantity: string, reason: string) {
  await screen.findByRole("button", { name: "Registrar movimiento" });
  press("Registrar movimiento");
  type("Unidades que entran", quantity);
  type("Motivo", reason);
}
async function opened() {
  renderApp(
    <TenantProvider value={tenant}>
      <StockPanel />
    </TenantProvider>,
  );
  await waitFor(() => expect(screen.getByLabelText("Producto")).toHaveLength(3));
  choose("Almacén", "w1");
  choose("Producto", "p1");
}
const lost = async () =>
  expect(await within(form()).findByRole("alert")).toHaveTextContent("No hay conexión");
const announced = async (on_hand: number) =>
  expect(
    await screen.findByText(`Entrada registrada: ahora hay ${on_hand} de Collar en Central.`),
  ).toBeVisible();

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => vi.unstubAllGlobals());

describe("MovementForm: la clave frente a una respuesta perdida", () => {
  it("reenviar lo mismo devuelve el ya registrado (200) y no cuenta dos veces", async () => {
    const api = fakeServer();
    await opened();
    await receipt("5", "Compra");
    api.lose = 1;
    press("Registrar entrada", form());
    await lost();
    press("Registrar entrada", form());
    await announced(5);
    expect(api.onHand).toBe(5);
    expect(api.posts[1]!.idempotency_key).toBe(api.posts[0]!.idempotency_key);
  });

  it("corregir algo y reenviar tampoco: la API lo dice, el saldo se relee e insistir es otro", async () => {
    const api = fakeServer();
    await opened();
    await receipt("5", "Compar");
    api.lose = 1;
    press("Registrar entrada", form());
    await lost();
    type("Motivo", "Compra"); // corrige una errata
    press("Registrar entrada", form());
    expect(await within(form()).findByRole("alert")).toHaveTextContent(/sí se registró/);
    expect(api.onHand).toBe(5);
    const here = () => screen.getByRole("heading", { name: "Collar en Central" }).parentElement!;
    await waitFor(() =>
      expect(within(here()).getAllByRole("definition")[0]).toHaveTextContent("5"),
    );
    press("Registrar entrada", form());
    await announced(10);
    const keys = api.posts.map((sent) => sent.idempotency_key);
    expect([keys[1] === keys[0], keys[2] === keys[0]]).toEqual([true, false]);
  });

  it("la clave sigue al cancelar y al cambiar de producto y volver", async () => {
    const api = fakeServer();
    await opened();
    await receipt("5", "Compra");
    api.lose = 1;
    press("Registrar entrada", form());
    await lost();
    press("Cancelar", form());
    choose("Producto", "p2"); // varias variantes: el formulario se desmonta
    await waitFor(() => expect(screen.queryByRole("button", { name: /Registrar/ })).toBeNull());
    choose("Producto", "p1");
    await receipt("5", "Compra");
    press("Registrar entrada", form());
    await announced(5);
    expect(api.posts[1]!.idempotency_key).toBe(api.posts[0]!.idempotency_key);
  });

  it("cambiar de almacén mientras se registra: el anuncio nombra la celda registrada", async () => {
    const api = fakeServer();
    await opened();
    await receipt("5", "Compra");
    let release = () => {};
    api.hold = new Promise<void>((done) => (release = done));
    press("Registrar entrada", form());
    await waitFor(() => expect(form()).toHaveAttribute("aria-busy", "true"));
    choose("Almacén", "w2");
    release();
    await announced(5);
    expect(api.posts[0]!.warehouse_id).toBe("w1");
  });
});
