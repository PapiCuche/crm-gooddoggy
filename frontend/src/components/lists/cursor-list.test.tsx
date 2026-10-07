import { fireEvent, screen, waitFor } from "@testing-library/react";
import { createRef, useState } from "react";
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

  it("`empty` solo sustituye a una lista sin filas: ni al cargar, ni con un error o una negativa", async () => {
    let reply: { status: number; body: unknown } = page([]);
    mockApi({ [LIST]: () => reply });
    const withEmpty = (
      <CursorList<Row>
        section="members"
        organization="Acme SAC"
        listKey={["lista"]}
        fetchPage={fetchPage}
        notice={<p>aviso de la pantalla</p>}
        empty={<p>sin filas</p>}
      >
        {(row) => <span>fila {row.id}</span>}
      </CursorList>
    );
    const view = renderApp(withEmpty);
    expect(screen.queryByText("sin filas")).not.toBeInTheDocument(); // todavía no se sabe
    expect(await screen.findByText("sin filas")).toBeVisible();
    expect(screen.getByText("aviso de la pantalla")).toBeVisible(); // el aviso sigue encima
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("0 miembros en la lista");
    view.unmount();
    for (const [status, code] of [
      [500, "INTERNAL_ERROR"],
      [403, "PERMISSION_DENIED"],
    ] as const) {
      reply = { status, body: { code } };
      const failed = renderApp(withEmpty);
      await screen.findByRole("alert");
      expect(screen.queryByText("sin filas")).not.toBeInTheDocument();
      failed.unmount();
    }
    reply = page([]);
    renderApp(ui); // sin `empty`, la lista vacía de siempre
    expect(await screen.findByRole("list", { name: "Miembros" })).toBeEmptyDOMElement();
  });

  it("`controls` sigue montado, con su foco, al cargar, con filas, con un error, con una negativa y al cambiar de lista", async () => {
    let reply: { status: number; body: unknown } = page(["a"]);
    const api = mockApi({ [LIST]: () => reply });
    function Lists() {
      const [key, setKey] = useState(0); // otra clave: otra lista, que se vuelve a pedir
      return (
        <>
          <button type="button" onClick={() => setKey(key + 1)}>
            otra lista
          </button>
          <CursorList<Row>
            section="members"
            organization="Acme SAC"
            listKey={["lista", key]}
            fetchPage={fetchPage}
            controls={<input aria-label="filtro" />}
            notice={<p>aviso de la pantalla</p>}
          >
            {(row) => <span>fila {row.id}</span>}
          </CursorList>
        </>
      );
    }
    renderApp(<Lists />);
    const another = () => fireEvent.click(screen.getByRole("button", { name: "otra lista" }));
    const control = screen.getByRole("textbox", { name: "filtro" }); // ya al cargar
    expect(screen.queryByText("aviso de la pantalla")).not.toBeInTheDocument(); // el aviso, no
    control.focus();
    await screen.findByText("fila a");
    // Entre el título y lo demás: antes del aviso y de la lista.
    const order = control.compareDocumentPosition(screen.getByText("aviso de la pantalla"));
    expect(order & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(
      screen.getByRole("heading", { level: 1 }).compareDocumentPosition(control) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    another(); // se vuelve a pedir, y falla
    expect(screen.getByRole("status")).toHaveTextContent("Cargando miembros…");
    expect(control).toBeInTheDocument(); // el mismo nodo: no se desmontó
    expect(await screen.findByRole("button", { name: "Reintentar" })).toBeVisible();
    expect(control).toHaveFocus();
    reply = { status: 403, body: { code: "PERMISSION_DENIED" } };
    another();
    expect(await screen.findByText(/No tienes permiso/)).toBeVisible();
    expect(control).toBeInTheDocument(); // tampoco con una negativa: el foco no se queda sin sitio
    expect(control).toHaveFocus();
    reply = page(["a"]);
    another(); // la lista vuelve
    await screen.findByText("fila a");
    expect(control).toHaveFocus();
    expect(api.mock.calls.length).toBeGreaterThanOrEqual(3);
  });
});
