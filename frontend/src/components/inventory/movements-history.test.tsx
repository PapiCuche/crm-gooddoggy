import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { MovementsHistory } from "./movements-history";

const API = "/api/v1/o/acme";
const LEDGER = `GET ${API}/inventory/movements/?variant_id=v1`;
const EVERYTHING = `GET ${API}/inventory/movements/`;
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "inventory.view", scopes: [] }],
};
const page = (results: unknown[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
const movement = (id: string, movement_type: string, quantity: number, after: number) => ({
  ...{ id, variant_id: "v1", warehouse_id: "w1", movement_type, quantity, reason: `Motivo ${id}` },
  variant: { id: "v1", name: "Collar", sku: "C-1", product_id: "p1", is_deleted: false },
  ...{ on_hand_after: after, reserved_after: 0, created_by_user_id: "u1" },
  created_at: "2026-10-22T15:30:00Z",
});
const CATALOG = {
  [`GET ${API}/warehouses/?limit=200`]: page([
    { id: "w1", name: "Central", code: "CEN", is_active: true },
    { id: "w2", name: "Surco", code: "SUR", is_active: false },
  ]),
  [`GET ${API}/products/?limit=200`]: page([{ id: "p1", name: "Collar", tracks_inventory: true }]),
  [`GET ${API}/products/p1/variants/?limit=200`]: page([{ id: "v1", name: "Collar", sku: "C-1" }]),
};

const screenOf = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <MovementsHistory />
    </TenantProvider>,
  );
const ledgerCalls = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.map(([url]) => String(url)).filter((url) => url.includes("movements"));
const choose = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

afterEach(() => vi.unstubAllGlobals());

describe("MovementsHistory", () => {
  it("sin elegir nada, todo el ledger; cada fila nombra su variante, también si se borró", async () => {
    const gone = { id: "v9", name: "Bolsa", sku: "B-9", product_id: "p9", is_deleted: true };
    const api = mockApi({
      ...CATALOG,
      [EVERYTHING]: page([
        { ...movement("m5", "RECEIPT", 2, 2), variant_id: "v9", variant: gone },
        movement("m2", "ISSUE", -3, 12),
      ]),
      [LEDGER]: page([movement("m2", "ISSUE", -3, 12), movement("m1", "RECEIPT", 15, 15)]),
    });
    screenOf();
    const everything = await screen.findByRole("list", { name: "Movimientos" });
    const [bag, collar] = within(everything).getAllByRole("listitem");
    expect(within(bag!).getByText("Bolsa · B-9, borrada del catálogo")).toBeVisible();
    expect(within(collar!).getByText("Collar · C-1")).toBeVisible();
    expect(screen.getByLabelText("Almacén")).toHaveDisplayValue("Todos los almacenes");
    expect(ledgerCalls(api)).toEqual([`${API}/inventory/movements/`]); // una sola petición
    await waitFor(() => expect(screen.getByLabelText("Producto")).toHaveLength(2));
    choose("Producto", "p1");
    expect(await screen.findByText("Motivo m1")).toBeVisible();
    const list = screen.getByRole("list", { name: "Movimientos" });
    const [issue, receipt] = within(list).getAllByRole("listitem");
    for (const text of ["Salida", "Motivo m2", "Central (CEN)", "−3", "Quedan 12"]) {
      expect(within(issue!).getByText(text)).toBeVisible();
    }
    expect(issue).toHaveTextContent("Cambio en existencias: -3");
    expect(within(issue!).getByText("−3").parentElement).not.toHaveClass("text-success");
    expect(within(receipt!).getByText("+15").parentElement).toHaveClass("text-success");
    expect(within(receipt!).getByText("Entrada")).toBeVisible();
    expect(receipt!.querySelector("time")).toHaveAttribute("dateTime", "2026-10-22T15:30:00Z");
    expect(screen.getByText("2 movimientos en la lista")).toBeInTheDocument();
  });

  it("filtra por almacén y «Cargar más» lleva los mismos filtros", async () => {
    const both = `GET ${API}/inventory/movements/?variant_id=v1&warehouse_id=w2`;
    const api = mockApi({
      ...CATALOG,
      [EVERYTHING]: page([]),
      [LEDGER]: page([]),
      [both]: page([{ ...movement("m3", "ADJUSTMENT", 0, 4), warehouse_id: "w2" }], "c2"),
      [`${both}&cursor=c2`]: page([{ ...movement("m4", "RECEIPT", 4, 4), warehouse_id: "w2" }]),
    });
    screenOf();
    await waitFor(() => expect(screen.getByLabelText("Producto")).toHaveLength(2));
    expect(await screen.findByText("Todavía no hay movimientos con estos filtros.")).toBeVisible();
    choose("Producto", "p1");
    await waitFor(() =>
      expect(ledgerCalls(api)).toContain(`${API}/inventory/movements/?variant_id=v1`),
    );
    choose("Almacén", "w2");
    expect(await screen.findByText("Ajuste")).toBeVisible();
    const list = screen.getByRole("list", { name: "Movimientos" });
    expect(within(list).getByText("Surco (SUR), inactivo")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByText("Motivo m4")).toBeVisible();
    expect(ledgerCalls(api).at(-1)).toBe(
      "/api/v1/o/acme/inventory/movements/?variant_id=v1&warehouse_id=w2&cursor=c2",
    );
  });

  it("sin permiso, la API decide y la pantalla lo explica", async () => {
    mockApi({ ...CATALOG, [EVERYTHING]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    screenOf();
    await waitFor(() => expect(screen.getByLabelText("Producto")).toHaveLength(2));
    expect(await screen.findByText(/No tienes permiso para ver el inventario/)).toBeVisible();
    expect(screen.getByLabelText("Producto")).toBeVisible(); // los controles siguen
  });
});
