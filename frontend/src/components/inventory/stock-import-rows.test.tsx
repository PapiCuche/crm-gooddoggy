import { onlineManager } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { StockImport, StockImportRow } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { StockImportRows } from "./stock-import-rows";

const URL_ = "/api/v1/o/acme/inventory/imports/i1/rows/";
const ROWS = `GET ${URL_}`;
const job: StockImport = {
  id: "i1",
  status: "VALIDATED",
  file_name: "stock.csv",
  total_rows: 3,
  valid_rows: 2,
  error_rows: 1,
  new_count: 1,
  update_count: 1,
  unchanged_count: 0,
  duplicate_of: null,
  encoding: "utf-8",
  ignored_columns: [],
  created_at: "2026-10-23T10:00:00Z",
  applied_at: null,
  result: null,
};
const row = (
  n: number,
  action: StockImportRow["action"],
  errors: string[] = [],
): StockImportRow => ({
  id: `r${n}`,
  row_number: n,
  action,
  sku: `SKU${n}`,
  almacen: "ALM1",
  cantidad: action === "ERROR" ? "x" : "5",
  quantity: action === "ERROR" ? null : 5,
  on_hand_seen: action === "ERROR" ? null : 3,
  errors,
});
const page = (results: StockImportRow[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
const urls = (api: ReturnType<typeof mockApi>) => api.mock.calls.map(([url]) => String(url));
const view = (onDenied = vi.fn(), shown: StockImport = job) => {
  renderApp(<StockImportRows slug="acme" job={shown} onDenied={onDenied} />);
  return onDenied;
};
const items = async () =>
  within(await screen.findByRole("list", { name: "Filas del archivo" })).getAllByRole("listitem");
const options = () =>
  within(screen.getByLabelText("Mostrar"))
    .getAllByRole("option")
    .map((option) => option.textContent);

const press = (name: string) => {
  const button = screen.getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
  return button;
};

afterEach(() => onlineManager.setOnline(true));

describe("StockImportRows", () => {
  it("enseña cada fila: línea, SKU y almacén, saldo visto y nuevo (o lo que vino), acción y errores", async () => {
    mockApi({
      [ROWS]: page([
        row(2, "UPDATE"),
        row(3, "ERROR", ["SKU: no existe.", "Cantidad: no es un entero."]),
      ]),
    });
    view();
    expect(screen.getByText("Cargando filas…")).toBeVisible();
    const [update, error] = await items();
    expect(update).toHaveTextContent(
      "Línea 2SKU2 · ALM1Saldo visto 3 · cantidad nueva 5Cambia el saldo",
    );
    expect(error).toHaveTextContent(
      "Línea 3SKU3 · ALM1Cantidad en el archivo: «x»SKU: no existe.Cantidad: no es un entero.Con error",
    );
    expect(within(error!).getByText("Con error")).toHaveClass("text-danger");
    expect(screen.getByRole("status")).toHaveTextContent("2 filas en la lista");
    expect(options()).toEqual(["Todas", "Celdas nuevas", "Cambian", "Sin cambios", "Con error"]);
    expect(screen.queryByRole("button", { name: "Cargar más" })).toBeNull();
  });

  it("el filtro pide a la API solo las filas de esa acción, también al cargar más", async () => {
    const api = mockApi({
      [ROWS]: page([row(2, "CREATE"), row(3, "ERROR", ["SKU: falta."])]),
      [`${ROWS}?action=ERROR`]: page([row(3, "ERROR", ["SKU: falta."])], "c2"),
      [`${ROWS}?action=ERROR&cursor=c2`]: page([row(9, "ERROR", ["Almacén: no existe."])]),
      [`${ROWS}?action=UNCHANGED`]: page([]),
    });
    view();
    await items();
    fireEvent.change(screen.getByLabelText("Mostrar"), { target: { value: "ERROR" } });
    await waitFor(async () => expect(await items()).toHaveLength(1));
    expect(screen.getByRole("status")).toHaveTextContent("1 fila en la lista");
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByText("Almacén: no existe.")).toBeVisible();
    expect(urls(api)).toEqual([URL_, `${URL_}?action=ERROR`, `${URL_}?action=ERROR&cursor=c2`]);
    fireEvent.change(screen.getByLabelText("Mostrar"), { target: { value: "UNCHANGED" } });
    expect(await screen.findByText("Ninguna fila con esa acción.")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Ninguna fila en la lista");
  });

  it("«Cargar más» sigue el cursor, añade las filas y lleva el foco a la primera nueva", async () => {
    let more: { status: number; body: unknown } = { status: 500, body: { code: "INTERNAL_ERROR" } };
    const api = mockApi({
      [ROWS]: page([row(2, "CREATE")], "c2"),
      [`${ROWS}?cursor=c2`]: () => more,
    });
    view();
    await items();
    const button = screen.getByRole("button", { name: "Cargar más" });
    button.focus();
    fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    expect(await items()).toHaveLength(1);
    more = { status: 200, body: { results: [row(3, "UPDATE"), row(4, "UNCHANGED")], next: null } };
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    await waitFor(async () => expect(await items()).toHaveLength(3));
    const [, fresh] = await items();
    await waitFor(() => expect(fresh).toHaveFocus());
    expect(screen.getByRole("status")).toHaveTextContent("3 filas en la lista");
    expect(screen.queryByRole("button", { name: "Cargar más" })).toBeNull();
    expect(new Set(urls(api))).toEqual(new Set([URL_, `${URL_}?cursor=c2`]));
  });

  it("si la lista falla se dice y se reintenta; sin sesión no enseña error", async () => {
    let reply: { status: number; body: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    mockApi({ [ROWS]: () => reply });
    view();
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    reply = page([row(2, "CREATE")]);
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await items()).toHaveLength(1);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("un 401 lo trata `Providers`: queda el aviso de carga, sin error", async () => {
    const api = mockApi({ [ROWS]: { status: 401, body: { code: "NOT_AUTHENTICATED" } } });
    const onDenied = view();
    await waitFor(() => expect(api).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(screen.getByText("Cargando filas…")).toBeVisible();
    expect(onDenied).not.toHaveBeenCalled();
  });

  it("un 403 avisa a la pantalla para que se cierre", async () => {
    mockApi({ [ROWS]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    const onDenied = view();
    await waitFor(() => expect(onDenied).toHaveBeenCalled());
  });

  it("aplicada: ofrece STALE y marca las filas aplicadas", async () => {
    const stale = { ...row(4, "STALE"), errors: ["El saldo cambió desde la previsualización."] };
    mockApi({ [ROWS]: page([row(2, "CREATE"), row(3, "UNCHANGED"), stale]) });
    view(vi.fn(), { ...job, status: "APPLIED" });
    const [created, unchanged, gone] = await items();
    expect(within(created!).getByText("Aplicada · Celda nueva")).toHaveClass("text-success");
    expect(unchanged).toHaveTextContent("Sin cambios");
    expect(unchanged).not.toHaveTextContent("Aplicada");
    expect(within(gone!).getByText("STALE")).toHaveClass("text-danger");
    expect(gone).toHaveTextContent("El saldo cambió desde la previsualización.");
    expect(options()).toContain("STALE");
  });
  it("sin red, «Cargar más» queda ocupado desde la pulsación hasta la respuesta", async () => {
    const api = mockApi({
      [ROWS]: page([row(2, "CREATE")], "c2"),
      [`${ROWS}?cursor=c2`]: page([row(3, "UPDATE")]),
    });
    view();
    await items();
    onlineManager.setOnline(false); // la petición espera a la red
    const button = press("Cargar más");
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveTextContent("Cargando más…");
    fireEvent.click(button); // ocupado: no cuenta
    onlineManager.setOnline(true);
    await waitFor(async () => expect(await items()).toHaveLength(2));
    expect(urls(api).filter((url) => url.endsWith("?cursor=c2"))).toHaveLength(1);
  });

  it("sin red, «Reintentar» queda ocupado; al abrir la lista el foco va al título", async () => {
    let reply: { status: number; body: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    mockApi({ [ROWS]: () => reply });
    view();
    await screen.findByRole("alert");
    reply = page([row(2, "CREATE")]);
    onlineManager.setOnline(false);
    const button = press("Reintentar");
    await waitFor(() => expect(button).toHaveAttribute("aria-disabled", "true"));
    expect(button).toHaveTextContent("Cargando filas…");
    onlineManager.setOnline(true);
    expect(await items()).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "Filas del archivo" })).toHaveFocus();
  });

  it("la misma fila nueva en otra lista también recibe el foco", async () => {
    mockApi({
      [ROWS]: page([row(2, "CREATE")], "c2"),
      [`${ROWS}?cursor=c2`]: page([row(52, "CREATE")], "c3"),
      [`${ROWS}?action=CREATE`]: page([row(2, "CREATE")], "k2"),
      [`${ROWS}?action=CREATE&cursor=k2`]: page([row(52, "CREATE")]),
    });
    view();
    await items();
    press("Cargar más");
    await waitFor(async () => expect((await items())[1]).toHaveFocus());
    fireEvent.change(screen.getByLabelText("Mostrar"), { target: { value: "CREATE" } });
    await screen.findByRole("button", { name: "Cargar más" });
    press("Cargar más");
    await waitFor(async () => expect(await items()).toHaveLength(2));
    expect(screen.queryByRole("button", { name: "Cargar más" })).toBeNull(); // última página
    await waitFor(async () => expect((await items())[1]).toHaveFocus());
  });

  it("si el foco ya se fue a otra parte, la página nueva no se lo quita", async () => {
    mockApi({
      [ROWS]: page([row(2, "CREATE")], "c2"),
      [`${ROWS}?cursor=c2`]: page([row(3, "UPDATE")]),
    });
    view();
    await items();
    press("Cargar más");
    const select = screen.getByLabelText("Mostrar");
    select.focus(); // antes de que llegue la respuesta
    await waitFor(async () => expect(await items()).toHaveLength(2));
    expect(select).toHaveFocus();
  });

  it("un 401 al cargar más no enseña error: lo trata `Providers`", async () => {
    const api = mockApi({
      [ROWS]: page([row(2, "CREATE")], "c2"),
      [`${ROWS}?cursor=c2`]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    view();
    await items();
    press("Cargar más");
    await waitFor(() => expect(urls(api)).toContain(`${URL_}?cursor=c2`));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Cargar más" })).toHaveAttribute(
        "aria-disabled",
        "false",
      ),
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("un 403 no enseña la tarjeta de error mientras la pantalla se cierra", async () => {
    mockApi({ [ROWS]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    const onDenied = view();
    await waitFor(() => expect(onDenied).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Reintentar" })).toBeNull();
    expect(screen.getByText("Cargando filas…")).toBeVisible();
  });
});
