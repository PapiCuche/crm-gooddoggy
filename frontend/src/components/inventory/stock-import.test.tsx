import { onlineManager } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext, StockImport as Job, StockImportRow } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { MAX_BYTES, MAX_ROWS, StockImport } from "./stock-import";

const URL_ = "/api/v1/o/acme/inventory/imports/";
const CREATE = `POST ${URL_}`;
const APPLY = `POST ${URL_}i1/apply/`;
const TEMPLATE = `GET ${URL_}template/`;
const REPORT = `GET ${URL_}i1/errors/`;
const ROWS = `GET ${URL_}i1/rows/`;
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const job: Job = {
  id: "i1",
  status: "VALIDATED",
  file_name: "stock.csv",
  total_rows: 4,
  valid_rows: 3,
  error_rows: 1,
  new_count: 1,
  update_count: 1,
  unchanged_count: 1,
  duplicate_of: "i0",
  encoding: "windows-1252",
  ignored_columns: ["nota"],
  created_at: "2026-10-23T10:00:00Z",
  applied_at: null,
  result: null,
};
const applied: Job = {
  ...job,
  status: "APPLIED",
  result: { applied: 1, unchanged: 1, stale: 1, failed: 0 },
};
const ok = (body: unknown, status = 200) => ({ status, body });
const row = (n: number, action: StockImportRow["action"], errors: string[] = []) => ({
  id: `r${n}`,
  row_number: n,
  action,
  sku: "IP16",
  almacen: "ALM1",
  cantidad: "5",
  quantity: 5,
  on_hand_seen: 3,
  errors,
});
const page = (...results: unknown[]) => ok({ results, next: null });
const rowsCalls = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.filter(([to]) => to === `${URL_}i1/rows/`);
const csv = (text: string, name = "stock.csv") => new File([text], name, { type: "text/csv" });
const lines = (n: number) => `sku,almacen,cantidad\n${"IP16,ALM1,5\n".repeat(n)}`;

const view = (...codes: string[]) =>
  renderApp(
    <TenantProvider value={tenant(...(codes.length ? codes : ["inventory.adjust"]))}>
      <StockImport />
    </TenantProvider>,
  );
function press(name: string) {
  const button = screen.getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
}
function choose(file: File) {
  fireEvent.change(screen.getByLabelText("Archivo CSV o Excel"), { target: { files: [file] } });
  press("Previsualizar");
}
const tile = (label: string) =>
  screen.getAllByText(label).find((node) => node.tagName === "DT")?.nextElementSibling;
const announced = () => screen.getAllByRole("status").at(-1);
const posts = (api: ReturnType<typeof mockApi>, url: string) =>
  api.mock.calls.filter(([to, init]) => to === url && init?.method === "POST");
async function preview(routes: Parameters<typeof mockApi>[0] = {}) {
  const api = mockApi({ [CREATE]: ok(job, 201), [ROWS]: page(row(2, "CREATE")), ...routes });
  const screen_ = view();
  choose(csv("sku,almacen,cantidad\nIP16,ALM1,5\n"));
  await screen.findByRole("heading", { name: "Revisa la importación" });
  return { api, ...screen_ };
}

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
  URL.createObjectURL = vi.fn(() => "blob:x");
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("StockImport", () => {
  it("sin `inventory.adjust` no pide nada y lo dice", () => {
    const api = mockApi({});
    view("inventory.view");
    expect(screen.getByRole("alert")).toHaveTextContent("No tienes permiso para importar stock");
    expect(screen.queryByLabelText("Archivo CSV o Excel")).toBeNull();
    expect(api).not.toHaveBeenCalled();
  });

  it("descarga la plantilla de la API; un error se dice al lado del botón", async () => {
    const headers: Record<string, string> = { "Content-Type": "text/csv; charset=utf-8" };
    let reply = { status: 200, body: "sku,almacen,cantidad" as unknown, headers };
    mockApi({ [TEMPLATE]: () => reply });
    view();
    press("Descargar plantilla");
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalledWith(expect.any(Blob)));
    reply = { status: 500, body: { code: "INTERNAL_ERROR" }, headers: {} };
    press("Descargar plantilla");
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    reply = { status: 403, body: { code: "PERMISSION_DENIED" }, headers: {} };
    press("Descargar plantilla");
    expect(await screen.findByText("No tienes permiso para hacer esto.")).toBeVisible();
  });

  it("revisa el archivo en el navegador antes de enviarlo: nada sale si no vale", async () => {
    const api = mockApi({ [CREATE]: ok(job, 201) });
    view();
    press("Previsualizar");
    expect(screen.getByRole("alert")).toHaveTextContent("Elige un archivo CSV o Excel.");
    const cases: [File, string][] = [
      [csv("a", "stock.xls"), "Solo se importan CSV y Excel .xlsx"],
      [new File([new Uint8Array(MAX_BYTES + 1)], "big.csv"), "pasa de 1 MB"],
      [csv(" \r\n\n"), "El archivo está vacío."],
      [csv(lines(1001)), "más de 1000 filas"],
    ];
    for (const [file, message] of cases) {
      choose(file);
      expect(await screen.findByRole("alert")).toHaveTextContent(message);
      expect(screen.getByLabelText("Archivo CSV o Excel")).toHaveAttribute("aria-invalid", "true");
    }
    expect(api).not.toHaveBeenCalled();
    choose(csv(lines(1000), "á.csv")); // 1000 filas sí; el contenido, en base64
    fireEvent.click(screen.getByRole("button", { name: /Previsualizar|Leyendo/ })); // no cuenta
    await screen.findByRole("heading", { name: "Revisa la importación" });
    expect(posts(api, URL_)).toHaveLength(1);
    const body = JSON.parse(String(posts(api, URL_)[0]?.[1]?.body));
    expect(body.file_name).toBe("á.csv");
    expect(atob(body.content)).toBe(lines(1000));
  });

  it("un Excel (por su firma) va tal cual: sus filas las cuenta la API", async () => {
    const api = mockApi({ [CREATE]: ok({ ...job, encoding: "xlsx", ignored_columns: [] }, 201) });
    view();
    const zip = new Uint8Array([
      0x50,
      0x4b,
      0x03,
      0x04,
      ...new Array(MAX_ROWS + 5).fill([97, 10]).flat(),
    ]);
    choose(csv(lines(1001), "stock.xlsx")); // un CSV con otro nombre: se cuenta como CSV
    expect(await screen.findByRole("alert")).toHaveTextContent("más de 1000 filas");
    choose(new File([zip], "conteo.xlsx"));
    await screen.findByRole("heading", { name: "Revisa la importación" });
    expect(posts(api, URL_)).toHaveLength(1);
    const sent = atob(JSON.parse(String(posts(api, URL_)[0]?.[1]?.body)).content);
    expect(Uint8Array.from(sent, (char) => char.charCodeAt(0))).toEqual(zip);
    expect(screen.getByText("Se leyó la primera hoja del Excel.")).toBeVisible();
  });

  it("las líneas solo con separadores o comillas cuentan en blanco, como en la API", async () => {
    const api = mockApi({ [CREATE]: ok(job, 201) });
    view();
    const padded = `${lines(1000)},,\n;;\n"",\n \t\r\n`;
    choose(csv(padded));
    await screen.findByRole("heading", { name: "Revisa la importación" });
    expect(atob(JSON.parse(String(posts(api, URL_)[0]?.[1]?.body)).content)).toBe(padded);
  });

  it("lo que muestra el campo es lo que se envía, también si cambia a mitad de una petición", async () => {
    let release: (reply: Response) => void = () => {};
    const sent: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((_to: RequestInfo | URL, init?: RequestInit) => {
        sent.push(atob(JSON.parse(String(init?.body)).content));
        return sent.length === 1
          ? new Promise<Response>((resolve) => {
              release = resolve;
            })
          : Promise.resolve(new Response(JSON.stringify(job), { status: 201 }));
      }),
    );
    view();
    choose(csv("sku,almacen,cantidad\nA,ALM1,1\n"));
    await waitFor(() => expect(sent).toHaveLength(1));
    fireEvent.change(screen.getByLabelText("Archivo CSV o Excel"), {
      target: { files: [csv("sku,almacen,cantidad\nB,ALM1,2\n")] },
    });
    release(new Response(JSON.stringify({ code: "INTERNAL_ERROR" }), { status: 500 }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    press("Previsualizar");
    await screen.findByRole("heading", { name: "Revisa la importación" });
    expect(sent).toEqual(["sku,almacen,cantidad\nA,ALM1,1\n", "sku,almacen,cantidad\nB,ALM1,2\n"]);
  });

  it("previsualiza: recuentos y avisos; sin cambios no ofrece aplicar; el archivo no queda en caché", async () => {
    const { client } = await preview();
    expect(screen.getByRole("heading", { name: "Revisa la importación" })).toHaveFocus();
    expect(screen.getByText("stock.csv · 4 filas")).toBeVisible();
    for (const [label, value] of [
      ["Celdas nuevas", "1"],
      ["Cambian", "1"],
      ["Con error", "1"],
    ]) {
      expect(tile(label!)).toHaveTextContent(value!);
    }
    expect(screen.getByText(/Este mismo archivo ya se subió antes/)).toBeVisible();
    expect(screen.getByText(/CSV de Excel en Windows\. Columnas ignoradas: nota\./)).toBeVisible();
    expect(screen.getByRole("button", { name: "Descargar filas con error" })).toBeVisible();
    expect(announced()).toHaveTextContent("Revisión lista: 4 filas, 1 con error.");
    await waitFor(() => expect(client.getMutationCache().getAll()).toHaveLength(0));
    press("Subir otro archivo");
    const same = { ...job, new_count: 0, update_count: 0, error_rows: 0, duplicate_of: null };
    mockApi({ [CREATE]: ok(same, 201) });
    choose(csv("sku,almacen,cantidad\nIP16,ALM1,3\n"));
    expect(await screen.findByText(/Ninguna fila cambia el stock/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Aplicar|Descargar filas/ })).toBeNull();
    expect(screen.queryByText(/ya se subió antes/)).toBeNull();
  });

  it("errores al subir: el motivo de la API, la red y sin sesión", async () => {
    let reply: { status: number; body: unknown } = {
      status: 400,
      body: {
        code: "VALIDATION_ERROR",
        fields: { content: [{ code: "invalid", message: "Falta la columna «almacen»." }] },
      },
    };
    mockApi({ [CREATE]: () => reply });
    view();
    choose(csv("sku\nA\n"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No pudimos leer el archivo: Falta la columna «almacen».",
    );
    onlineManager.setOnline(false); // sin red falla enseguida: no queda en cola
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("offline"))),
    );
    press("Previsualizar");
    expect(await screen.findByText(/No hay conexión con el servidor/)).toBeVisible();
    onlineManager.setOnline(true);
    mockApi({ [CREATE]: () => reply });
    reply = { status: 401, body: { code: "NOT_AUTHENTICATED" } };
    press("Previsualizar");
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull()); // `Providers` lleva al login
    expect(screen.getByRole("button", { name: "Leyendo el archivo…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("confirma con un diálogo explícito, aplica una vez por pulsación y enseña el resultado", async () => {
    let done = false;
    const stale = row(3, "STALE", ["El saldo cambió desde la previsualización: era 3, ahora 4."]);
    const { api } = await preview({
      [APPLY]: () => ((done = true), ok(applied)),
      [REPORT]: { status: 200, body: "x", headers: { "Content-Type": "text/csv" } },
      [ROWS]: () =>
        done ? page(row(2, "UPDATE"), stale) : page(row(2, "UPDATE"), row(3, "UPDATE")),
    });
    expect(await screen.findAllByText("Cambia el saldo")).toHaveLength(2);
    press("Aplicar 2 filas");
    const ask = screen.getByRole("group", { name: "Aplicar 2 filas" });
    expect(ask).toHaveAccessibleDescription(
      /Las 2 celdas quedarán .* La fila con error no se aplica\. .*STALE/,
    );
    expect(screen.getByRole("button", { name: "Cancelar" })).toHaveFocus();
    press("Cancelar");
    expect(screen.getByRole("button", { name: "Aplicar 2 filas" })).toHaveFocus();
    press("Aplicar 2 filas");
    fireEvent.click(screen.getByRole("button", { name: "Sí, aplicar" }));
    fireEvent.click(screen.getByRole("button", { name: /Sí, aplicar|Aplicando/ }));
    expect(await screen.findByRole("heading", { name: "Importación aplicada" })).toHaveFocus();
    expect(posts(api, `${URL_}i1/apply/`)).toHaveLength(1);
    expect(tile("Aplicadas")).toHaveTextContent("1");
    expect(tile("STALE")).toHaveTextContent("1");
    expect(screen.getByText(/Las filas STALE no se tocaron/)).toBeVisible();
    expect(announced()).toHaveTextContent("Importación aplicada: 1 fila aplicada.");
    // Aplicar cambia las filas: la lista se vuelve a pedir y enseña la STALE y la aplicada.
    const list = await screen.findByRole("list", { name: "Filas del archivo" });
    expect(await within(list).findByText("STALE")).toBeVisible();
    expect(within(list).getByText("Aplicada · Cambia el saldo")).toBeVisible();
    expect(within(list).getByText(/El saldo cambió desde la previsualización/)).toBeVisible();
    expect(rowsCalls(api)).toHaveLength(2);
    press("Descargar filas con error");
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalled());
    press("Subir otro archivo");
    expect(screen.getByLabelText("Archivo CSV o Excel")).toHaveFocus();
  });

  it("una respuesta perdida al aplicar se reintenta sin miedo: misma importación, mismo resultado", async () => {
    let lost = true;
    const { api } = await preview();
    const fetch = api.getMockImplementation()!;
    api.mockImplementation((url, init) =>
      String(url).endsWith("/apply/") && lost
        ? ((lost = false), Promise.reject(new TypeError("reset")))
        : String(url).endsWith("/apply/")
          ? Promise.resolve(new Response(JSON.stringify(applied), { status: 200 }))
          : fetch(url, init),
    );
    press("Aplicar 2 filas");
    press("Sí, aplicar");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/No hay conexión.*la importación se aplica una sola vez/);
    press("Sí, aplicar");
    expect(await screen.findByRole("heading", { name: "Importación aplicada" })).toBeVisible();
    expect(posts(api, `${URL_}i1/apply/`)).toHaveLength(2);
  });

  it("aplicar: un 404 vuelve al archivo con su aviso y un 403 cierra la pantalla", async () => {
    let status = 404;
    await preview({
      [APPLY]: () => ({
        status,
        body: { code: status === 404 ? "NOT_FOUND" : "PERMISSION_DENIED" },
      }),
    });
    press("Aplicar 2 filas");
    press("Sí, aplicar");
    expect(
      await screen.findByText("Esa importación ya no existe. Sube el archivo otra vez."),
    ).toBeVisible();
    expect(screen.getByLabelText("Archivo CSV o Excel")).toHaveFocus();
    status = 403;
    choose(csv("sku,almacen,cantidad\nIP16,ALM1,5\n"));
    await screen.findByRole("button", { name: "Aplicar 2 filas" });
    press("Aplicar 2 filas");
    press("Sí, aplicar");
    expect(await screen.findByText(/No tienes permiso para importar stock/)).toBeVisible();
  });

  it("las filas: un 403 al pedirlas cierra la pantalla, como el resto", async () => {
    await preview({ [ROWS]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    expect(await screen.findByText(/No tienes permiso para importar stock/)).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Revisa la importación" })).toBeNull();
  });

  it("Enter mantenido no repite el envío", () => {
    mockApi({});
    view();
    const form = screen.getByLabelText("Archivo CSV o Excel");
    expect(fireEvent.keyDown(form, { key: "Enter", repeat: true })).toBe(false);
    expect(fireEvent.keyDown(form, { key: "Enter" })).toBe(true);
  });
});
