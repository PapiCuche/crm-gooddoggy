import { onlineManager, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Member, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp, renderIntl } from "@/test-utils";

import { MembersList } from "./members-list";

const LIST = "GET /api/v1/o/acme/members/";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "users.view", scopes: [] }],
};
const member = (id: string, extra: Partial<Member> = {}): Member => ({
  id,
  status: "ACTIVE",
  joined_at: "2026-10-04T15:49:34Z",
  user: { id: `u-${id}`, email: `${id}@acme.pe`, first_name: "", last_name: "" },
  roles: [],
  ...extra,
});
const ana = member("ana", {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  roles: [
    { code: "owner", name: "Owner" },
    { code: "sales", name: "Ventas" },
  ],
});
const ui = (
  <TenantProvider value={tenant}>
    <MembersList />
  </TenantProvider>
);
const screenOf = () => renderApp(ui);
const twoPages = {
  [LIST]: { status: 200, body: { results: [ana], next: "abc" } },
  [`${LIST}?cursor=abc`]: { status: 200, body: { results: [member("luis")], next: null } },
};
const rows = () => within(screen.getByRole("list", { name: "Miembros" })).getAllByRole("listitem");

afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("MembersList", () => {
  it("muestra lo que devuelve la API: nombre, correo, roles, estado y alta", async () => {
    const luis = member("luis", { status: "SUSPENDED", joined_at: "2026-01-02T03:04:05Z" });
    mockApi({ [LIST]: { status: 200, body: { results: [ana, luis], next: null } } });
    screenOf();
    expect(screen.getByRole("status")).toHaveTextContent("Cargando miembros");
    expect(await screen.findByRole("list", { name: "Miembros" })).toBeVisible();
    const [first, second] = rows().filter(
      (row) => row.parentElement?.getAttribute("aria-label") !== "Roles",
    ) as [HTMLElement, HTMLElement];
    expect(first).toHaveTextContent("Ana López");
    expect(first).toHaveTextContent("ana@acme.pe");
    for (const text of ["Ana López", "ana@acme.pe"])
      expect(screen.getByText(text)).toHaveClass("wrap-anywhere"); // largo: se parte, no se recorta
    expect(first).toHaveTextContent("Activo");
    expect(first).toHaveTextContent(/Alta: 4 oct\.? 2026/);
    const roles = within(within(first).getByRole("list", { name: "Roles" })).getAllByRole(
      "listitem",
    );
    expect(roles.map((role) => role.textContent)).toEqual(["Owner", "Ventas"]); // nombres
    expect(within(second).getAllByText("luis@acme.pe")).toHaveLength(1); // sin nombre: el correo, una vez
    expect(second).toHaveTextContent("Suspendido");
    expect(second).toHaveTextContent("Sin rol");
    expect(second).toHaveTextContent(/Alta: 1 ene\.? 2026/); // la fecha, en la zona de la aplicación
    expect(screen.getByRole("status")).toHaveTextContent("2 miembros en la lista");
    expect(screen.queryByRole("button", { name: "Cargar más" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Miembros");
    expect(screen.getByText(/pertenecen a Acme SAC/)).toBeVisible();
  });

  it("carga la página siguiente con el cursor y deja el foco en lo que llegó", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [ana], next: "abc+/=" } },
      [`${LIST}?cursor=abc%2B%2F%3D`]: {
        status: 200,
        body: { results: [member("luis"), member("marta")], next: null },
      },
    });
    screenOf();
    const more = await screen.findByRole("button", { name: "Cargar más" });
    fireEvent.click(more);
    fireEvent.click(more); // ocupado: la segunda pulsación no cuenta
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("3 miembros"));
    expect(api).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "Cargar más" })).not.toBeInTheDocument();
    const loaded = screen.getByText("luis@acme.pe", { selector: ".font-medium" }).closest("li");
    await waitFor(() => expect(loaded).toHaveFocus()); // el botón se fue: el foco, a la fila nueva
  });

  it("al reabrir la pantalla con páginas en caché el foco no se mueve", async () => {
    const api = mockApi(twoPages);
    const view = screenOf();
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("2 miembros"));
    view.unmount();
    renderIntl(<QueryClientProvider client={view.client}>{ui}</QueryClientProvider>);
    expect(screen.getByRole("status")).toHaveTextContent("2 miembros"); // lo que había en caché
    await waitFor(() => expect(api).toHaveBeenCalledTimes(4)); // y se vuelve a pedir
    expect(document.body).toHaveFocus();
  });

  it("sin red el botón queda ocupado, y la página no le quita el foco a quien ya se fue", async () => {
    const api = mockApi(twoPages);
    screenOf();
    const more = await screen.findByRole("button", { name: "Cargar más" });
    act(() => onlineManager.setOnline(false));
    fireEvent.click(more);
    expect(more).toHaveTextContent("Cargando más…");
    expect(more).toHaveAttribute("aria-disabled", "true");
    const title = screen.getByRole("heading", { level: 1 });
    title.focus();
    expect(api).toHaveBeenCalledTimes(1); // la petición espera a la red
    act(() => onlineManager.setOnline(true));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("2 miembros"));
    expect(api).toHaveBeenCalledTimes(2);
    expect(title).toHaveFocus();
  });

  it("si la página siguiente falla lo dice y el mismo botón reintenta", async () => {
    let reply: { status: number; body: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    mockApi({
      [LIST]: { status: 200, body: { results: [ana], next: "abc" } },
      [`${LIST}?cursor=abc`]: () => reply,
    });
    screenOf();
    const more = await screen.findByRole("button", { name: "Cargar más" });
    fireEvent.click(more);
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal");
    expect(more.nextElementSibling).toBe(screen.getByRole("alert")); // al lado: no mueve el botón
    expect(screen.getByText("ana@acme.pe")).toBeVisible(); // lo ya cargado sigue en pantalla
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // al reintentar se quita y,
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal"); // si falla, vuelve
    reply = { status: 200, body: { results: [member("luis")], next: "abc" } };
    more.focus();
    fireEvent.click(more);
    await waitFor(() => expect(screen.getByText("luis@acme.pe").closest("li")).toHaveFocus());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    more.focus();
    fireEvent.click(more);
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal");
    expect(more).toHaveFocus(); // falló: el foco sigue en el botón, no vuelve a la fila anterior
  });

  it("sin el permiso lo explica y no ofrece reintentar: decide la API", async () => {
    mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    screenOf();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver los miembros de esta organización.",
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  it("una negativa cierra la lista, también si ya estaba abierta, y un fallo pasajero no la reabre", async () => {
    const page = { status: 200, body: { results: [ana], next: null } };
    const failure = { status: 500, body: { code: "INTERNAL_ERROR" } };
    const denial = { status: 403, body: { code: "PERMISSION_DENIED" } };
    let reply: { status: number; body: unknown } = failure;
    mockApi({ [LIST]: () => reply });
    const view = screenOf();
    const refresh = async (next: typeof reply) => {
      reply = next; // como al volver a la pestaña; React se entera en una tarea posterior
      await act(async () => {
        await view.client.refetchQueries();
        await new Promise((resolve) => setTimeout(resolve));
      });
    };
    const retry = await screen.findByRole("button", { name: "Reintentar" });
    reply = denial;
    fireEvent.click(retry); // la API niega al reintentar: el foco no se queda en ninguna parte
    expect(await screen.findByRole("alert")).toHaveTextContent("No tienes permiso para ver");
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();
    await refresh(page);
    expect(screen.getByText("ana@acme.pe")).toBeVisible(); // la API vuelve a responder bien
    await refresh(denial);
    expect(screen.getByRole("alert")).toHaveTextContent("No tienes permiso para ver");
    expect(screen.queryByText("ana@acme.pe")).not.toBeInTheDocument(); // con la lista abierta
    await refresh(failure);
    expect(screen.queryByText("ana@acme.pe")).not.toBeInTheDocument(); // sigue cerrada
    view.unmount(); // al salir no queda nada en memoria: la próxima entrada pregunta a la API
    await waitFor(() => expect(view.client.getQueryCache().getAll()).toHaveLength(0));
  });

  it("una negativa al cargar más también cierra la lista, con el foco en el título", async () => {
    mockApi({
      [LIST]: { status: 200, body: { results: [ana], next: "abc" } },
      [`${LIST}?cursor=abc`]: { status: 403, body: { code: "PERMISSION_DENIED" } },
    });
    screenOf();
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No tienes permiso para ver");
    expect(screen.queryByText("ana@acme.pe")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
  });

  it("sin sesión no enseña un error: el login lo decide el proveedor", async () => {
    mockApi({ [LIST]: { status: 401, body: { code: "NOT_AUTHENTICATED" } } });
    const { client } = screenOf();
    await waitFor(() => expect(client.getQueryCache().getAll()[0]?.state.status).toBe("error"));
    await act(() => new Promise((resolve) => setTimeout(resolve))); // React ya lo sabe
    expect(screen.getByRole("status")).toHaveTextContent("Cargando miembros");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("un fallo al cargar se dice, y reintentar lleva a la lista con el foco en el título", async () => {
    let reply: { status: number; body: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    mockApi({ [LIST]: () => reply });
    screenOf();
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal");
    const retry = screen.getByRole("button", { name: "Reintentar" });
    retry.focus();
    fireEvent.click(retry);
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal"); // falla otra vez
    expect(screen.getByRole("button", { name: "Reintentar" })).toBe(retry); // el mismo botón
    expect(retry).toHaveFocus();
    reply = { status: 200, body: { results: [ana], next: null } };
    fireEvent.click(retry);
    expect(await screen.findByRole("list", { name: "Miembros" })).toBeVisible();
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
  });
});
