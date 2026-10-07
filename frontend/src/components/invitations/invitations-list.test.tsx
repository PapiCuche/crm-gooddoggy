import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Invitation, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { InvitationsList } from "./invitations-list";

const LIST = "GET /api/v1/o/acme/invitations/";
const ROLES = "GET /api/v1/o/acme/roles/?limit=200";
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
// Identificadores con prefijo propio: ninguno debe llegar al DOM, y así se distinguen del texto.
const invitation = (name: string, extra: Partial<Invitation> = {}): Invitation =>
  ({
    id: `fila-${name}`,
    email: `${name}@cliente.pe`,
    role_ids: ["rol-ventas"],
    status: "PENDING",
    expires_at: "2026-10-14T15:30:00Z",
    invited_by: "cuenta-ana",
    created_at: "2026-10-07T15:30:00Z",
    ...extra,
  }) as Invitation;
const role = (id: string, name: string) => ({ id, name, code: id, description: "" });
const page = (...rows: Invitation[]) => ({ status: 200, body: { results: rows, next: null } });
const directory = { results: [role("rol-ventas", "Ventas"), role("rol-caja", "Caja")], next: null };
const screenOf = (...codes: string[]) =>
  renderApp(
    <TenantProvider value={tenant(...codes)}>
      <InvitationsList />
    </TenantProvider>,
  );
const card = (name: string) => screen.getByText(`${name}@cliente.pe`).closest("li") as HTMLElement;
const lines = (name: string) =>
  [...card(name).querySelectorAll("span, li, p.text-muted")].map((node) => node.textContent);
const urls = (api: ReturnType<typeof mockApi>) => api.mock.calls.map(([url]) => String(url));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks(); // el espía de `console.error`, también si el test falló
});

describe("InvitationsList", () => {
  it("muestra lo que devuelve la API: correo, roles por su nombre, estado y fechas", async () => {
    const api = mockApi({
      [LIST]: page(
        invitation("nueva", { role_ids: ["rol-caja", "rol-ventas"] }),
        invitation("tarde", { status: "EXPIRED", expires_at: "2026-10-01T03:00:00Z" }),
        invitation("fuera", { status: "REVOKED", role_ids: ["rol-borrado", "rol-ventas"] }),
        invitation("dentro", { status: "ACCEPTED", role_ids: ["rol-caja", "rol-caja"] }),
      ),
      [ROLES]: { status: 200, body: directory },
    });
    screenOf("roles.view");
    expect(screen.getByRole("status")).toHaveTextContent("Cargando invitaciones");
    expect(await screen.findByRole("list", { name: "Invitaciones" })).toBeVisible();
    await waitFor(() =>
      expect(lines("nueva")).toEqual([
        "nueva@cliente.pe",
        "Invitada el 7 oct. 2026",
        "Caja", // en el orden de la invitación
        "Ventas",
        "Pendiente",
        "Caduca el 14 oct. 2026",
      ]),
    );
    // La fecha, en la zona de la aplicación: las 03:00 UTC del día 1 son el 30 de septiembre.
    expect(lines("tarde").slice(-2)).toEqual(["Caducada", "Caducidad: 30 set. 2026"]);
    expect(lines("fuera")).toEqual([
      "fuera@cliente.pe",
      "Invitada el 7 oct. 2026",
      "Un rol que ya no existe",
      "Ventas",
      "Revocada", // sin fecha de caducidad: ya no importa
    ]);
    expect(lines("dentro").slice(2)).toEqual(["Caja", "Caja", "Aceptada"]); // repetido: dos veces
    expect(screen.getByText("Pendiente")).not.toHaveClass("text-muted", "text-success");
    expect(screen.getByText("Aceptada")).toHaveClass("text-success");
    for (const status of ["Revocada", "Caducada", "Un rol que ya no existe"])
      expect(screen.getByText(status)).toHaveClass("text-muted");
    expect(screen.getByText("nueva@cliente.pe")).toHaveClass("wrap-anywhere");
    expect(screen.getAllByRole("list", { name: "Roles" })).toHaveLength(4);
    const dom = new XMLSerializer().serializeToString(document.body);
    for (const hidden of ["fila-", "rol-", "cuenta-"]) expect(dom).not.toContain(hidden);
    expect(screen.getByText("Good Doggy / Invitaciones")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("4 invitaciones en la lista");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Invitaciones");
    expect(screen.getByText(/Las personas invitadas a Acme SAC/)).toBeVisible();
    expect(urls(api)).toEqual(["/api/v1/o/acme/invitations/", "/api/v1/o/acme/roles/?limit=200"]);
    expect(screen.queryByRole("button")).toBeNull(); // solo lectura
  });

  it("sin `roles.view` no pide el directorio de roles y dice cuántos son", async () => {
    const api = mockApi({
      [LIST]: page(invitation("una"), invitation("dos", { role_ids: ["rol-a", "rol-b"] })),
    });
    screenOf("users.invite", "users.manage");
    await screen.findByRole("list", { name: "Invitaciones" });
    expect(lines("una")).toContain("1 rol");
    expect(lines("dos")).toContain("2 roles");
    expect(screen.queryByRole("list", { name: "Roles" })).toBeNull();
    expect(urls(api)).toEqual(["/api/v1/o/acme/invitations/"]);
    expect(new XMLSerializer().serializeToString(document.body)).not.toContain("rol-");
  });

  it("si el directorio de roles falla, la lista sigue y dice cuántos son", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const api = mockApi({ [LIST]: page(invitation("una")), [ROLES]: { status: 500 } });
    screenOf("roles.view");
    await screen.findByRole("list", { name: "Invitaciones" });
    await waitFor(() => expect(urls(api).filter((url) => url.includes("/roles/"))).toHaveLength(2));
    expect(lines("una")).toContain("1 rol"); // un fallo de lectura se reintenta una vez, y ya
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("lee el directorio de roles entero, página a página", async () => {
    const api = mockApi({
      [LIST]: page(invitation("una", { role_ids: ["rol-caja"] })),
      [ROLES]: { status: 200, body: { results: [role("rol-ventas", "Ventas")], next: "p2" } },
      [`${ROLES}&cursor=p2`]: {
        status: 200,
        body: { results: [role("rol-caja", "Caja")], next: null },
      },
    });
    const view = screenOf("roles.view");
    await waitFor(() => expect(lines("una")).toContain("Caja"));
    expect(urls(api).slice(1)).toEqual([
      "/api/v1/o/acme/roles/?limit=200",
      "/api/v1/o/acme/roles/?limit=200&cursor=p2",
    ]);
    view.unmount(); // al salir, el directorio tampoco queda en memoria
    await waitFor(() => expect(view.client.getQueryCache().getAll()).toEqual([]));
  });

  it("pide la página siguiente con el cursor, y un estado desconocido se enseña como llega", async () => {
    const odd = invitation("rara", { status: "ON_HOLD" as Invitation["status"] });
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [invitation("una")], next: "c2" } },
      [`${LIST}?cursor=c2`]: page(odd),
    });
    screenOf();
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    await waitFor(() =>
      expect(lines("rara")).toEqual([
        "rara@cliente.pe",
        "Invitada el 7 oct. 2026",
        "1 rol",
        "ON_HOLD",
      ]),
    );
    expect(screen.getByText("ON_HOLD")).toHaveClass("text-muted");
    expect(urls(api)).toEqual([
      "/api/v1/o/acme/invitations/",
      "/api/v1/o/acme/invitations/?cursor=c2",
    ]);
  });

  it("sin invitaciones lo dice, y un 403 se explica", async () => {
    mockApi({ [LIST]: page() });
    const view = screenOf();
    expect(
      await screen.findByText("Esta organización todavía no ha invitado a nadie."),
    ).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Sin invitaciones en la lista");
    view.unmount();
    mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    screenOf("roles.view");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver las invitaciones de esta organización.",
    );
  });
});
