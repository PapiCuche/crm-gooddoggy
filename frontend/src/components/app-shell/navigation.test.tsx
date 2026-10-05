import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { TenantGate } from "./tenant-gate";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/o/acme/miembros",
  notFound: vi.fn(),
}));
const ana: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [],
};
const links = async (permissions: SelfContext["permissions"]) => {
  mockApi({ "GET /api/v1/o/acme/me/": { status: 200, body: { ...ana, permissions } } });
  renderApp(<TenantGate orgSlug="acme">pantalla</TenantGate>);
  const nav = await screen.findByRole("navigation", { name: "Navegación principal" });
  return within(nav)
    .getAllByRole("link")
    .map((link) => [link.getAttribute("href"), link.getAttribute("aria-current")]);
};

afterEach(() => vi.unstubAllGlobals());

describe("navegación del shell", () => {
  it("no lista una entrada cuyo permiso el usuario no tiene", async () => {
    expect(await links([{ code: "audit.view", scopes: [] }])).toEqual([["/o/acme", null]]);
  });

  it("la lista con el permiso, y marca solo la entrada de la ruta actual", async () => {
    expect(await links([{ code: "users.view", scopes: [] }])).toEqual([
      ["/o/acme", null],
      ["/o/acme/miembros", "page"],
    ]);
    expect(screen.getByRole("banner")).toHaveTextContent("Workspace › Miembros");
    expect(document.title).toBe("Miembros · Acme SAC · Good Doggy CRM"); // sección y organización
  });

  it("la entrada «Roles» pide `roles.view`: ver miembros no basta", async () => {
    const both = [
      { code: "users.view", scopes: [] },
      { code: "roles.view", scopes: [] },
    ];
    expect(await links(both)).toEqual([
      ["/o/acme", null],
      ["/o/acme/miembros", "page"],
      ["/o/acme/roles", null],
    ]);
    expect(screen.getByRole("link", { name: "Roles" })).toBeVisible();
  });
});
