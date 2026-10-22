import { onlineManager } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { StockPanel } from "./stock-panel";

const API = "/api/v1/o/acme";
const LEVELS = `GET ${API}/inventory/levels/?limit=200&variant_id=v1`;
const MOVE = `POST ${API}/inventory/movements/`;
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants("inventory.view", "inventory.adjust"),
};
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const level = (on_hand: number) => ({
  ...{ id: "l1", variant_id: "v1", warehouse_id: "w1", on_hand, reserved: 2 },
  available: on_hand - 2,
});
const moved = (on_hand_after: number, movement_type = "RECEIPT") => ({
  ...{ id: "m1", variant_id: "v1", warehouse_id: "w1", movement_type, on_hand_after },
  ...{ quantity: 5, reserved_after: 2, reason: "x", created_by_user_id: "u1", created_at: "" },
});
const CATALOG = {
  [`GET ${API}/warehouses/?limit=200`]: page([
    { id: "w1", name: "Central", code: "CEN", is_active: true, branch_id: "b1" },
  ]),
  [`GET ${API}/products/?limit=200`]: page([{ id: "p1", name: "Collar", tracks_inventory: true }]),
  [`GET ${API}/products/p1/variants/?limit=200`]: page([{ id: "v1", name: "Collar", sku: "C-1" }]),
};

const screenOf = (who = tenant) =>
  renderApp(
    <TenantProvider value={who}>
      <StockPanel />
    </TenantProvider>,
  );
const posts = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls
    .filter(([, init]) => init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init?.body)));
const form = () => screen.getByRole("form", { name: "Nuevo movimiento" });
const field = (label: string) => within(form()).getByLabelText(label);
const type = (label: string, value: string) => fireEvent.input(field(label), { target: { value } });
function press(name: string, inside: HTMLElement = document.body) {
  const button = within(inside).getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
}
// Hasta la celda Collar en Central, con el formulario abierto.
async function opened(who = tenant) {
  screenOf(who);
  await waitFor(() => expect(screen.getByLabelText("Producto")).toHaveLength(2));
  fireEvent.change(screen.getByLabelText("Almacén"), { target: { value: "w1" } });
  fireEvent.change(screen.getByLabelText("Producto"), { target: { value: "p1" } });
  await screen.findByRole("button", { name: "Registrar movimiento" });
  press("Registrar movimiento");
}

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("MovementForm", () => {
  it("sin `inventory.adjust` no se ofrece", async () => {
    mockApi({ ...CATALOG, [LEVELS]: page([level(10)]) });
    screenOf({ ...tenant, permissions: grants("inventory.view") });
    await waitFor(() => expect(screen.getByLabelText("Producto")).toHaveLength(2));
    fireEvent.change(screen.getByLabelText("Almacén"), { target: { value: "w1" } });
    fireEvent.change(screen.getByLabelText("Producto"), { target: { value: "p1" } });
    expect(await screen.findByRole("heading", { name: "Collar en Central" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Registrar movimiento" })).toBeNull();
  });

  it("registra una entrada con su motivo, refresca el saldo y anuncia lo que hay ahora", async () => {
    let onHand = 10;
    const api = mockApi({
      ...CATALOG,
      [LEVELS]: () => page([level(onHand)]),
      [MOVE]: () => {
        onHand = 15;
        return { status: 201, body: moved(15) };
      },
    });
    await opened();
    expect(field("Unidades que entran")).toHaveFocus();
    expect(field("Unidades que entran")).toHaveAttribute("inputmode", "numeric");
    expect(field("Entrada: llegan unidades")).toBeChecked();
    for (const wrong of ["abc", "0", "1.5", "-3", "1000001"]) {
      type("Unidades que entran", wrong);
      type("Motivo", "Compra");
      press("Registrar entrada", form());
      expect(field("Unidades que entran")).toHaveAccessibleDescription(/desde 1 hasta 1000000/);
      expect(field("Unidades que entran")).toHaveFocus();
    }
    type("Unidades que entran", "5");
    expect(field("Unidades que entran")).not.toHaveAccessibleDescription(/hasta 1000000/);
    type("Motivo", "  Compra a Kong  ");
    const reply = api.getMockImplementation()!;
    let release = () => {};
    api.mockImplementationOnce(async (input, init) => {
      await new Promise<void>((done) => (release = done)); // el registro tarda: se suelta a mano
      return reply(input, init);
    });
    press("Registrar entrada", form());
    fireEvent.submit(form()); // otra vez, enseguida: un solo movimiento
    await waitFor(() => expect(posts(api)).toHaveLength(1));
    fireEvent.click(field("Salida: se van unidades")); // mientras se registra, no cambia
    expect(field("Entrada: llegan unidades")).toBeChecked();
    release();
    expect(await screen.findByText("Entrada registrada: ahora hay 15 en Central.")).toBeVisible();
    const here = screen.getByRole("heading", { name: "Collar en Central" }).parentElement!;
    expect(
      within(here)
        .getAllByRole("definition")
        .map((cell) => cell.textContent),
    ).toEqual(["15", "2", "13"]);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Registrar movimiento" })).toHaveFocus(),
    );
    expect(posts(api)).toHaveLength(1);
    const [body] = posts(api);
    expect(body).toEqual({
      ...{ variant_id: "v1", warehouse_id: "w1", movement_type: "RECEIPT", quantity: 5 },
      ...{ reason: "Compra a Kong", idempotency_key: expect.stringMatching(/^[0-9a-f]{32}$/) },
    });
    press("Registrar movimiento"); // el mismo movimiento otra vez es otro: clave nueva
    type("Unidades que entran", "5");
    type("Motivo", "Compra a Kong");
    press("Registrar entrada", form());
    await waitFor(() => expect(posts(api)).toHaveLength(2));
    expect(posts(api)[1].idempotency_key).not.toBe(body.idempotency_key);
  });

  it("un ajuste acepta cero; reintentar el mismo envío repite la clave, cambiarlo no", async () => {
    let reply: { status: number; body: object } = { status: 500, body: {} };
    const api = mockApi({ ...CATALOG, [LEVELS]: page([level(10)]), [MOVE]: () => reply });
    await opened();
    fireEvent.click(field("Ajuste: lo que se contó"));
    expect(field("Unidades contadas")).toHaveAccessibleDescription(/desde 0/);
    type("Unidades contadas", "0");
    type("Motivo", "Conteo");
    onlineManager.setOnline(false);
    api.mockRejectedValueOnce(new TypeError("Failed to fetch")); // sin respuesta: ¿llegó?
    press("Registrar ajuste", form());
    expect(await within(form()).findByRole("alert")).toHaveTextContent("No hay conexión");
    onlineManager.setOnline(true);
    press("Registrar ajuste", form());
    await waitFor(() => expect(within(form()).getByRole("alert")).toHaveTextContent("Algo salió"));
    type("Motivo", "Conteo semanal");
    reply = { status: 200, body: moved(0, "ADJUSTMENT") }; // la API devuelve el ya registrado
    press("Registrar ajuste", form());
    expect(await screen.findByText("Ajuste registrado: ahora hay 0 en Central.")).toBeVisible();
    const [first, second, third] = posts(api);
    expect(first).toMatchObject({ movement_type: "ADJUSTMENT", quantity: 0 });
    expect(second.idempotency_key).toBe(first.idempotency_key);
    expect(third.idempotency_key).not.toBe(first.idempotency_key);
  });

  it("los errores se dicen por `code`, junto a la cantidad o en el formulario", async () => {
    let reply: { status: number; body: object } = {
      status: 409,
      body: { code: "INSUFFICIENT_STOCK" },
    };
    const api = mockApi({ ...CATALOG, [LEVELS]: page([level(10)]), [MOVE]: () => reply });
    await opened();
    fireEvent.click(field("Salida: se van unidades"));
    type("Unidades que salen", "50");
    type("Motivo", "Venta");
    const again = async (next: typeof reply, text: RegExp, where = () => form()) => {
      reply = next;
      press("Registrar salida", form());
      await waitFor(() => expect(within(where()).getByRole("alert")).toHaveTextContent(text));
    };
    await again(reply, /No hay stock suficiente/);
    expect(field("Unidades que salen")).toHaveFocus();
    await again({ status: 409, body: { code: "WAREHOUSE_INACTIVE" } }, /está desactivado/);
    await again({ status: 409, body: { code: "IDEMPOTENCY_KEY_REUSED" } }, /coincidía con otro/);
    const bad = { code: "VALIDATION_ERROR", fields: { warehouse_id: ["x"] } };
    await again({ status: 400, body: bad }, /ya no existen/);
    await waitFor(() =>
      expect(api.mock.calls.filter(([url]) => String(url).includes("warehouses"))).toHaveLength(2),
    );
    await again(
      { status: 403, body: { code: "PERMISSION_DENIED" } },
      /No tienes permiso para registrar/,
    );
    const keys = posts(api).map((body) => body.idempotency_key);
    expect(keys[3]).not.toBe(keys[2]); // tras `IDEMPOTENCY_KEY_REUSED`, una clave nueva
    expect(keys[2]).toBe(keys[1]);
    const held = within(form()).getByRole("button", { name: "Registrar salida" });
    expect(fireEvent.keyDown(held, { key: "Enter", repeat: true })).toBe(false);
    press("Cancelar", form());
    expect(screen.getByRole("button", { name: "Registrar movimiento" })).toHaveFocus();
  });
});
