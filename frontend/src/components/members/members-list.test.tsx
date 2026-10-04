import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Member, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

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
const screenOf = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <MembersList />
    </TenantProvider>,
  );
const rows = () => within(screen.getByRole("list", { name: "Miembros" })).getAllByRole("listitem");

afterEach(() => vi.unstubAllGlobals());

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
    expect(first).toHaveTextContent("Activo");
    expect(first).toHaveTextContent(/Alta: 4 oct\.? 2026/);
    const roles = within(within(first).getByRole("list", { name: "Roles" })).getAllByRole(
      "listitem",
    );
    expect(roles.map((role) => role.textContent)).toEqual(["Owner", "Ventas"]); // nombres
    expect(second).toHaveTextContent("luis@acme.pe"); // sin nombre: el correo hace de nombre
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
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("3 miembros"));
    expect(api).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "Cargar más" })).not.toBeInTheDocument();
    const loaded = screen.getByText("luis@acme.pe", { selector: ".font-medium" }).closest("li");
    await waitFor(() => expect(loaded).toHaveFocus()); // el botón se fue: el foco, a la fila nueva
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
    expect(screen.getByText("ana@acme.pe")).toBeVisible(); // lo ya cargado sigue en pantalla
    reply = { status: 200, body: { results: [member("luis")], next: null } };
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("2 miembros"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
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
