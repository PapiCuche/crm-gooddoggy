import { fireEvent, screen, waitFor } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiFetch } from "@/lib/http";
import { mockApi, renderApp } from "@/test-utils";

import { CursorList, type CursorListHandle } from "./cursor-list";

type Row = { id: string };
const LIST = "GET /lista";
const handle = createRef<CursorListHandle>();
const fetchPage = (cursor: string | undefined, signal: AbortSignal) =>
  apiFetch<{ results: Row[]; next: string | null }>(cursor ? `/lista?cursor=${cursor}` : "/lista", {
    signal,
  });
// Una lista mínima sobre la sección de miembros: lo que cualquier pantalla recibe de `CursorList`.
const ui = (
  <>
    <button type="button">otra parte</button>
    <CursorList<Row>
      ref={handle}
      section="members"
      organization="Acme SAC"
      listKey={["lista"]}
      fetchPage={fetchPage}
      rowClassName="fila-propia"
    >
      {(row) => <span>fila {row.id}</span>}
    </CursorList>
  </>
);
const page = (ids: string[], next: string | null = null) => ({
  status: 200,
  body: { results: ids.map((id) => ({ id })), next },
});
const rows = () => screen.getAllByText(/^fila /).map((cell) => cell.closest("li") as HTMLElement);

afterEach(() => vi.unstubAllGlobals());

describe("CursorList", () => {
  it("pone el marco de la pantalla y las filas, sin foco programático en una sola página", async () => {
    mockApi({ [LIST]: page(["a", "b"]) });
    renderApp(ui);
    expect(screen.getByText("Good Doggy / Miembros")).toBeVisible();
    expect(screen.getByText(/pertenecen a Acme SAC/)).toBeVisible();
    await screen.findByRole("list", { name: "Miembros" });
    for (const row of rows()) {
      expect(row).toHaveClass("fila-propia", "bg-surface"); // las clases de la pantalla y las suyas
      expect(row).not.toHaveAttribute("tabindex"); // solo la primera fila de una página nueva
    }
    expect(screen.getByRole("status")).toHaveClass("sr-only"); // el recuento, para quien no ve
  });

  it("una negativa al cargar más no le quita el foco a quien ya está en otra parte", async () => {
    mockApi({
      [LIST]: page(["a"], "abc"),
      [`${LIST}?cursor=abc`]: { status: 403, body: { code: "PERMISSION_DENIED" } },
    });
    renderApp(ui);
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    const elsewhere = screen.getByRole("button", { name: "otra parte" });
    elsewhere.focus();
    expect(await screen.findByRole("alert")).toHaveTextContent("No tienes permiso");
    expect(elsewhere).toHaveFocus();
  });

  it("el error de la primera carga se dice por su código, y reintentar queda ocupado", async () => {
    const api = mockApi({ [LIST]: page(["a"]) });
    api.mockRejectedValue(new TypeError("Failed to fetch"));
    renderApp(ui);
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay conexión"); // por código
    const retry = screen.getByRole("button", { name: "Reintentar" });
    expect(retry).toHaveAttribute("aria-disabled", "false");
    let release = () => {};
    api.mockImplementation(
      () =>
        new Promise<Response>(
          (resolve) => (release = () => resolve(new Response(JSON.stringify(page(["a"]).body)))),
        ),
    );
    fireEvent.click(retry);
    await waitFor(() => expect(retry).toHaveTextContent("Cargando miembros…"));
    expect(retry).toHaveAttribute("aria-disabled", "true");
    release();
    await screen.findByRole("list", { name: "Miembros" });
  });

  it("la pantalla vuelve a pedir la lista y lleva el foco al título solo mientras está montada", async () => {
    const api = mockApi({ [LIST]: page(["a"]) });
    const view = renderApp(ui);
    await screen.findByRole("list", { name: "Miembros" });
    handle.current?.focusHeading();
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();
    handle.current?.refetch();
    await waitFor(() => expect(api).toHaveBeenCalledTimes(2));
    view.unmount();
    expect(handle.current).toBeNull(); // una respuesta que llegue después ya no pide nada
  });
});
