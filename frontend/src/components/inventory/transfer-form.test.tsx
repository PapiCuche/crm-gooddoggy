import { onlineManager } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { StockPanel } from "./stock-panel";

const API = "/api/v1/o/acme";
const LEVELS = `GET ${API}/inventory/levels/?limit=200&variant_id=v1`;
const MOVE = `POST ${API}/inventory/transfers/`;
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: grants("inventory.view", "inventory.transfer"),
};
type Reply = { status: number; body: object };
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const level = (warehouse_id: string, on_hand: number, reserved = 0) => ({
  ...{ id: `l-${warehouse_id}`, variant_id: "v1", warehouse_id, on_hand, reserved },
  available: on_hand - reserved,
});
const leg = (warehouse_id: string, quantity: number, on_hand_after: number) => ({
  ...{ id: `m-${warehouse_id}`, variant_id: "v1", warehouse_id, quantity, on_hand_after },
  movement_type: quantity < 0 ? "TRANSFER_OUT" : "TRANSFER_IN",
  ...{ reserved_after: 0, reason: "x", transfer_group_id: "g1", created_at: "" },
});
const warehouse = (id: string, name: string, code: string, is_active = true) => ({
  ...{ id, name, code, is_active, branch_id: "b1" },
});
const CATALOG = {
  [`GET ${API}/warehouses/?limit=200`]: page([
    warehouse("w1", "Central", "CEN"),
    warehouse("w2", "Norte", "NOR"),
    warehouse("w3", "Sur", "SUR", false),
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
const form = () => screen.getByRole("form", { name: "Transferir stock" });
const field = (label: string) => within(form()).getByLabelText(label);
const type = (label: string, value: string) => fireEvent.input(field(label), { target: { value } });
const choose = (value: string) =>
  fireEvent.change(field("Almacén de destino"), { target: { value } });
function press(name: string, inside: HTMLElement = document.body) {
  const button = within(inside).getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
}
// Hasta la celda Collar en Central, con su saldo en pantalla.
async function cell(who = tenant) {
  screenOf(who);
  await waitFor(() => expect(screen.getByLabelText("Producto")).toHaveLength(2));
  fireEvent.change(screen.getByLabelText("Almacén"), { target: { value: "w1" } });
  fireEvent.change(screen.getByLabelText("Producto"), { target: { value: "p1" } });
  await screen.findByRole("heading", { name: "Collar en Central" });
}
async function opened() {
  await cell();
  press("Transferir a otro almacén");
  await waitFor(() => expect(field("Unidades que se transfieren")).toHaveFocus());
}
const figures = (heading: string) =>
  within(screen.getByRole("heading", { name: heading }).parentElement!)
    .getAllByRole("definition")
    .map((one) => one.textContent);

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("TransferForm", () => {
  it("sin `inventory.transfer` no se ofrece", async () => {
    mockApi({ ...CATALOG, [LEVELS]: page([level("w1", 10)]) });
    await cell({ ...tenant, permissions: grants("inventory.view", "inventory.adjust") });
    expect(screen.getByRole("button", { name: "Registrar movimiento" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Transferir a otro almacén" })).toBeNull();
  });

  it("transfiere lo disponible a otro almacén activo y refresca los dos saldos", async () => {
    let levels = [level("w1", 10, 2), level("w2", 1)];
    const api = mockApi({
      ...CATALOG,
      [LEVELS]: () => page(levels),
      [MOVE]: () => {
        levels = [level("w1", 7, 2), level("w2", 4)];
        return {
          status: 201,
          body: { transfer_group_id: "g1", legs: [leg("w1", -3, 7), leg("w2", 3, 4)] },
        };
      },
    });
    await opened();
    // Destino: ni el origen ni un almacén desactivado; con uno solo, ya elegido.
    expect(
      [...(field("Almacén de destino") as HTMLSelectElement).options].map((one) => one.text),
    ).toEqual(["Elige el almacén de destino", "Norte (NOR)"]);
    expect(field("Almacén de destino")).toHaveValue("w2");
    expect(field("Unidades que se transfieren")).toHaveAccessibleDescription(/hasta 8/);
    for (const wrong of ["0", "9", "1.5", "abc"]) {
      type("Unidades que se transfieren", wrong);
      type("Motivo", "Reponer");
      press("Transferir", form());
      expect(field("Unidades que se transfieren")).toHaveAccessibleDescription(
        "Escribe un número entero desde 1 hasta 8.",
      );
    }
    type("Unidades que se transfieren", "3");
    type("Motivo", "");
    press("Transferir", form());
    expect(field("Motivo")).toHaveAccessibleDescription("Escribe el motivo.");
    type("Motivo", "  Reponer Norte  ");
    choose("");
    press("Transferir", form());
    expect(field("Almacén de destino")).toHaveAccessibleDescription(
      "Elige a qué almacén van las unidades.",
    );
    expect(posts(api)).toHaveLength(0);
    choose("w2");
    press("Transferir", form());
    fireEvent.submit(form()); // otra vez, enseguida: una sola transferencia
    expect(
      await screen.findByText(
        "Transferidas 3 unidades de Collar: ahora hay 7 en Central y 4 en Norte.",
      ),
    ).toBeVisible();
    expect(posts(api)).toEqual([
      {
        ...{ variant_id: "v1", from_warehouse_id: "w1", to_warehouse_id: "w2", quantity: 3 },
        ...{ reason: "Reponer Norte", idempotency_key: expect.stringMatching(/^[0-9a-f]{32}$/) },
      },
    ]);
    expect(figures("Collar en Central")).toEqual(["7", "2", "5"]);
    const everywhere = screen.getByRole("list", { name: "En todos los almacenes" });
    expect(within(everywhere).getAllByRole("listitem")[1]).toHaveTextContent("Norte (NOR)");
    expect(within(everywhere).getAllByRole("listitem")[1]).toHaveTextContent(/Existencias4/);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Transferir a otro almacén" })).toHaveFocus(),
    );
    press("Transferir a otro almacén"); // la misma otra vez es otra transferencia: clave nueva
    type("Unidades que se transfieren", "1");
    type("Motivo", "Reponer Norte");
    press("Transferir", form());
    await waitFor(() => expect(posts(api)).toHaveLength(2));
    expect(posts(api)[1].idempotency_key).not.toBe(posts(api)[0].idempotency_key);
  });

  it("tras un fallo, la clave sigue hasta que la API da la transferencia por hecha", async () => {
    let reply: Reply = { status: 500, body: {} };
    const api = mockApi({ ...CATALOG, [LEVELS]: page([level("w1", 10)]), [MOVE]: () => reply });
    await opened();
    type("Unidades que se transfieren", "1");
    type("Motivo", "Reponer");
    onlineManager.setOnline(false);
    api.mockRejectedValueOnce(new TypeError("Failed to fetch")); // sin respuesta: ¿llegó?
    press("Transferir", form());
    expect(await within(form()).findByRole("alert")).toHaveTextContent("No hay conexión");
    onlineManager.setOnline(true);
    press("Transferir", form());
    await waitFor(() => expect(within(form()).getByRole("alert")).toHaveTextContent("Algo salió"));
    reply = {
      status: 200,
      body: { transfer_group_id: "g1", legs: [leg("w1", -1, 9), leg("w2", 1, 1)] },
    };
    press("Transferir", form()); // 200: la de antes, devuelta sin repetirla
    expect(await screen.findByText(/Transferida 1 unidad de Collar/)).toBeVisible();
    const [first, ...rest] = posts(api).map((body) => body.idempotency_key);
    expect(rest).toEqual([first, first]);
  });

  it("los errores se dicen por `code`, junto a la cantidad o en el formulario", async () => {
    let reply: Reply = { status: 409, body: { code: "INSUFFICIENT_STOCK" } };
    const api = mockApi({ ...CATALOG, [LEVELS]: page([level("w1", 10)]), [MOVE]: () => reply });
    await opened();
    type("Unidades que se transfieren", "5");
    type("Motivo", "Reponer");
    const again = async (next: Reply, text: RegExp | string, where = () => form()) => {
      reply = next;
      press("Transferir", form());
      await waitFor(() =>
        expect(within(where()).getByText(text, { exact: typeof text !== "string" })).toBeVisible(),
      );
    };
    await again(reply, /Ya no hay tantas unidades disponibles/);
    await waitFor(() => expect(field("Unidades que se transfieren")).toHaveFocus());
    await again({ status: 409, body: { code: "STOCK_LIMIT" } }, /no puede guardar tantas/);
    await again({ status: 409, body: { code: "WAREHOUSE_INACTIVE" } }, /está desactivado/);
    await again({ status: 409, body: { code: "IDEMPOTENCY_KEY_REUSED" } }, /sí se registró/);
    const bad = { code: "VALIDATION_ERROR", fields: { to_warehouse_id: ["x"] } };
    await again({ status: 400, body: bad }, /ya no existen/);
    await again(
      { status: 403, body: { code: "PERMISSION_DENIED" } },
      /No tienes permiso para transferir/,
    );
    await again({ status: 500, body: { code: "INTERNAL_ERROR" } }, /Algo salió/);
    // Inactivo y referencia perdida: los almacenes se vuelven a pedir.
    expect(api.mock.calls.filter(([url]) => String(url).includes("warehouses"))).toHaveLength(3);
    const keys = posts(api).map((body) => body.idempotency_key);
    expect(new Set(keys.slice(0, 4)).size).toBe(1);
    expect(keys[4]).not.toBe(keys[3]); // tras `IDEMPOTENCY_KEY_REUSED`, una clave nueva
    press("Cancelar", form());
    expect(screen.getByRole("button", { name: "Transferir a otro almacén" })).toHaveFocus();
  });

  it("sin nada disponible o sin otro almacén activo, dice por qué no se puede", async () => {
    mockApi({ ...CATALOG, [LEVELS]: page([level("w1", 4, 4)]) });
    await cell();
    const button = screen.getByRole("button", { name: "Transferir a otro almacén" });
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveAccessibleDescription(/lo reservado no se mueve/);
    fireEvent.click(button);
    expect(screen.queryByRole("form", { name: "Transferir stock" })).toBeNull();
  });
});
