import { onlineManager } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { StockPanel } from "./stock-panel";

const API = "/api/v1/o/acme";
const LEVELS = `GET ${API}/inventory/levels/?limit=200&variant_id=v1`;
const LIST = `GET ${API}/inventory/reservations/?limit=200&variant_id=v1&warehouse_id=w1&status=ACTIVE`;
const RESERVE = `POST ${API}/inventory/reservations/`;
const RELEASE = `POST ${API}/inventory/reservations/r1/release/`;
const NOW = Date.parse("2026-11-15T15:00:00Z"); // las 10:00 en Lima
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: grants("inventory.view", "inventory.manage", "users.view"),
};
type Reply = { status: number; body: object };
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const level = (on_hand: number, reserved: number) =>
  page([
    {
      id: "l1",
      variant_id: "v1",
      warehouse_id: "w1",
      on_hand,
      reserved,
      available: on_hand - reserved,
    },
  ]);
const reservation = (id: string, quantity: number, expires_at: string, by: string | null) => ({
  ...{ id, variant_id: "v1", warehouse_id: "w1", quantity, status: "ACTIVE", expires_at },
  ...{ reason: `Cliente ${id}`, created_by_user_id: by, created_at: "" },
});
const LUIS = { id: "u2", first_name: "Luis", last_name: "Rojas", email: "l@a.pe" };
const CATALOG = {
  [`GET ${API}/warehouses/?limit=200`]: page([{ id: "w1", name: "Central", code: "CEN" }]),
  [`GET ${API}/products/?limit=200`]: page([{ id: "p1", name: "Collar", tracks_inventory: true }]),
  [`GET ${API}/products/p1/variants/?limit=200`]: page([{ id: "v1", name: "Collar", sku: "C-1" }]),
  [`GET ${API}/members/?limit=200`]: page([{ status: "ACTIVE", user: LUIS }]),
};
const posts = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls
    .filter(([, init]) => init?.method === "POST")
    .map(([url, init]) => ({ url: String(url), ...JSON.parse(String(init?.body)) }));
const form = (name = "Reservar stock") => screen.getByRole("form", { name });
const type = (label: string, value: string, name?: string) =>
  fireEvent.input(within(form(name)).getByLabelText(label), { target: { value } });
function press(name: string | RegExp, inside: HTMLElement = document.body) {
  const button = within(inside).getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
}
// Hasta la celda Collar en Central; con `reply`, «Reservar» abierto con 2 unidades y un motivo.
async function cell(who = tenant, reply?: () => Reply) {
  const api = mockApi({ ...CATALOG, ...routes, ...(reply ? { [RESERVE]: reply } : {}) });
  renderApp(
    <TenantProvider value={who}>
      <StockPanel />
    </TenantProvider>,
  );
  await waitFor(() => expect(screen.getByLabelText("Producto")).toHaveLength(2));
  fireEvent.change(screen.getByLabelText("Almacén"), { target: { value: "w1" } });
  fireEvent.change(screen.getByLabelText("Producto"), { target: { value: "p1" } });
  await screen.findByRole("heading", { name: "Collar en Central" });
  if (reply) await openWith("2", "Cliente");
  return api;
}
async function openWith(quantity: string, reason: string) {
  await waitFor(() => press("Reservar"));
  type("Unidades que se reservan", quantity);
  type("Motivo", reason);
}
let routes: Record<string, Reply | (() => Reply)> = {};
const figures = () =>
  within(screen.getByRole("heading", { name: "Collar en Central" }).parentElement!)
    .getAllByRole("definition")
    .map((one) => one.textContent);
const rows = () =>
  within(screen.getByRole("list", { name: "Reservas activas" }))
    .getAllByRole("listitem")
    .map((one) => one.textContent);

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("Reservas de la celda", () => {
  it("se ven por vencimiento, con quién y si ya vencieron; sin `inventory.manage`, sin acciones", async () => {
    routes = {
      [LEVELS]: level(10, 6),
      [LIST]: page([
        reservation("r3", 1, "2026-11-15T14:00:00Z", "u2"), // ya pasó: el job aún no la cerró
        reservation("r2", 2, "2026-11-20T15:00:00Z", "u9"),
        reservation("r1", 3, "2026-11-16T15:00:00Z", "u1"),
      ]),
    };
    await cell({ ...tenant, permissions: grants("inventory.view") });
    await screen.findByRole("list", { name: "Reservas activas" });
    expect(rows()).toEqual([
      "1 unidadCliente r3Venció el 15 nov. 2026, 9:00 a. m.: el sistema la libera en unos minutos.Reservó: otro miembro",
      "3 unidadesCliente r1Vence el 16 nov. 2026, 10:00 a. m.Reservó: tú",
      "2 unidadesCliente r2Vence el 20 nov. 2026, 10:00 a. m.Reservó: otro miembro",
    ]);
    expect(screen.queryByRole("button", { name: /Liberar|Reservar/ })).toBeNull();
  });

  it("reserva lo disponible, anuncia lo reservado y lo disponible, y libera con confirmación", async () => {
    let reserved = 0;
    let list: unknown[] = [];
    routes = {
      [LEVELS]: () => level(10, reserved),
      [LIST]: () => page(list),
      [RESERVE]: () => {
        reserved = 4;
        list = [reservation("r1", 4, "2026-11-22T15:00:00.000Z", "u2")];
        return { status: 201, body: list[0]! };
      },
      [RELEASE]: () => {
        [reserved, list] = [0, []];
        return { status: 200, body: { ...reservation("r1", 4, "", "u2"), status: "RELEASED" } };
      },
    };
    const api = await cell();
    expect(
      await screen.findByText("No hay unidades reservadas de esta variante en este almacén."),
    ).toBeVisible();
    press("Reservar");
    type("Motivo", "Pedido de Luis");
    for (const wrong of ["0", "11"]) {
      type("Unidades que se reservan", wrong);
      press("Reservar", form());
      expect(within(form()).getByLabelText("Unidades que se reservan")).toHaveAccessibleDescription(
        "Escribe un número entero desde 1 hasta 10.",
      );
    }
    type("Unidades que se reservan", "4");
    fireEvent.change(within(form()).getByLabelText("Vence"), { target: { value: "7" } });
    press("Reservar", form());
    fireEvent.submit(form()); // otra vez, enseguida: una sola reserva
    expect(
      await screen.findByText(
        "Reservadas 4 unidades de Collar en Central (vence el 22 nov. 2026, 10:00 a. m.). Ahora: reservado 4, disponible 6.",
      ),
    ).toBeVisible();
    expect(posts(api)).toEqual([
      {
        ...{ url: RESERVE.slice(5), variant_id: "v1", warehouse_id: "w1", quantity: 4 },
        ...{ reason: "Pedido de Luis", expires_at: "2026-11-22T15:00:00.000Z" },
        idempotency_key: expect.stringMatching(/^[0-9a-f]{32}$/),
      },
    ]);
    expect(figures()).toEqual(["10", "4", "6"]);
    expect(rows()).toEqual([
      "4 unidadesCliente r1Vence el 22 nov. 2026, 10:00 a. m.Reservó: Luis RojasLiberar: 4 unidades, Cliente r1",
    ]);
    await waitFor(() => expect(screen.getByRole("button", { name: "Reservar" })).toHaveFocus());

    press(/^Liberar/);
    expect(
      screen.getByRole("heading", { name: /¿Liberar la reserva de 4 unidades para «Cliente r1»/ }),
    ).toBeVisible();
    press("Cancelar", form("Liberar reserva"));
    const title = screen.getByRole("heading", { name: "Reservas activas" });
    await waitFor(() => expect(title).toHaveFocus());
    press(/^Liberar/);
    type("Motivo de la liberación", "Ya no lo quiere", "Liberar reserva");
    press("Liberar", form("Liberar reserva"));
    expect(
      await screen.findByText(
        "Reserva de 4 unidades de Collar liberada en Central. Ahora: reservado 0, disponible 10.",
      ),
    ).toBeVisible();
    expect(posts(api)[1]).toEqual({ url: RELEASE.slice(5), reason: "Ya no lo quiere" });
    expect(figures()).toEqual(["10", "0", "10"]);
    await waitFor(() => expect(title).toHaveFocus());
  });

  it("la clave y el vencimiento siguen hasta que la API da la reserva por hecha", async () => {
    let reply: Reply = { status: 500, body: {} };
    routes = { [LEVELS]: level(5, 0), [LIST]: page([]) };
    const api = await cell(tenant, () => reply);
    fireEvent.change(within(form()).getByLabelText("Vence"), { target: { value: "day" } });
    type("Día del vencimiento", "2026-12-15"); // pasados 29 días
    press("Reservar", form());
    expect(within(form()).getByLabelText("Día del vencimiento")).toHaveAccessibleDescription(
      "Elige un día desde hoy hasta el 2026-12-14.",
    );
    fireEvent.change(within(form()).getByLabelText("Vence"), { target: { value: "3" } });
    onlineManager.setOnline(false);
    api.mockRejectedValueOnce(new TypeError("Failed to fetch")); // sin respuesta: ¿llegó?
    press("Reservar", form());
    expect(await within(form()).findByRole("alert")).toHaveTextContent("No hay conexión");
    onlineManager.setOnline(true);
    vi.setSystemTime(NOW + 60_000); // un minuto después: el mismo instante, no otro
    press("Reservar", form());
    await waitFor(() => expect(within(form()).getByRole("alert")).toHaveTextContent("Algo salió"));
    reply = { status: 200, body: reservation("r1", 2, "2026-11-21T04:59:00.000Z", "u1") };
    press("Reservar", form());
    expect(await screen.findByText(/Reservadas 2 unidades de Collar en Central/)).toBeVisible();
    const sent = posts(api).map(({ idempotency_key, expires_at }) => [idempotency_key, expires_at]);
    expect(sent).toEqual([sent[0], sent[0], sent[0]]); // a 3 días de la primera, no de cada envío
    expect(sent[0]![1]).toBe(new Date(NOW + 3 * 86_400_000).toISOString());
    await openWith("2", "Otro"); // tras el éxito, otra reserva: clave nueva; el día, a las 23:59
    fireEvent.change(within(form()).getByLabelText("Vence"), { target: { value: "day" } });
    type("Día del vencimiento", "2026-11-20");
    press("Reservar", form());
    await waitFor(() => expect(posts(api)).toHaveLength(4));
    expect(posts(api)[3]).toMatchObject({ expires_at: "2026-11-21T04:59:00.000Z" });
    expect(posts(api)[3].idempotency_key).not.toBe(sent[0]![0]);
  });

  it("los errores se dicen por `code`", async () => {
    let reply: Reply = { status: 409, body: { code: "INSUFFICIENT_STOCK" } };
    routes = { [LEVELS]: level(5, 0), [LIST]: page([]) };
    const api = await cell(tenant, () => reply);
    const again = async (next: Reply, text: RegExp) => {
      reply = next;
      press("Reservar", form());
      await waitFor(() => expect(within(form()).getByText(text)).toBeVisible());
    };
    await again(reply, /Ya no hay tantas unidades disponibles/);
    await again({ status: 409, body: { code: "WAREHOUSE_INACTIVE" } }, /no admite reservas/);
    await again({ status: 409, body: { code: "VARIANT_INACTIVE" } }, /La variante no está activa/);
    await again({ status: 409, body: { code: "TOO_MANY_RESERVATIONS" } }, /máximo de reservas/);
    await again({ status: 409, body: { code: "IDEMPOTENCY_KEY_REUSED" } }, /sí se registró/);
    const late = { code: "VALIDATION_ERROR", fields: { expires_at: ["x"] } };
    await again({ status: 400, body: late }, /El vencimiento ya no vale/);
    await again(
      { status: 403, body: { code: "PERMISSION_DENIED" } },
      /No tienes permiso para reservar/,
    );
    await again({ status: 500, body: { code: "INTERNAL_ERROR" } }, /Algo salió/);
    const keys = posts(api).map((body) => body.idempotency_key);
    expect(new Set(keys.slice(0, 5)).size).toBe(1);
    expect(keys[5]).not.toBe(keys[4]); // tras `IDEMPOTENCY_KEY_REUSED`, una clave nueva
  });

  it("liberar una que ya venció lo dice y vuelve a pedir las reservas", async () => {
    let list = [
      reservation("r1", 2, "2026-11-16T15:00:00Z", "u1"),
      reservation("r9", 1, "2026-11-15T14:00:00Z", "u1"), // vencida: sin «Liberar» (c1)
    ];
    routes = {
      [LEVELS]: level(5, 2),
      [LIST]: () => page(list),
      [RELEASE]: () => ((list = []), { status: 409, body: { code: "RESERVATION_NOT_ACTIVE" } }),
    };
    const api = await cell();
    await screen.findByRole("button", { name: /^Liberar/ });
    expect(screen.getAllByRole("button", { name: /^Liberar/ })).toHaveLength(1);
    press(/^Liberar/);
    type("Motivo de la liberación", "Ya no", "Liberar reserva");
    press("Liberar", form("Liberar reserva"));
    expect(await screen.findByText(/Esa reserva ya venció o se consumió/)).toBeVisible();
    expect(
      await screen.findByText("No hay unidades reservadas de esta variante en este almacén."),
    ).toBeVisible();
    expect(api.mock.calls.filter(([url]) => String(url) === LIST.slice(4))).toHaveLength(2);
  });
});
