import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CatalogNav } from "@/components/catalog/catalog-nav";
import type { SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { holds, NAVIGATION } from "./navigation";
import { TenantProvider } from "./tenant-context";
import { TenantGate } from "./tenant-gate";

const route = vi.hoisted(() => ({ pathname: "/o/acme/miembros" }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => route.pathname,
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

afterEach(() => {
  vi.unstubAllGlobals();
  route.pathname = "/o/acme/miembros";
});

describe("navegación del shell", () => {
  it("no lista una entrada cuyo permiso el usuario no tiene", async () => {
    expect(await links([{ code: "users.invite", scopes: [] }])).toEqual([["/o/acme", null]]);
  });

  it("la lista con el permiso, y marca solo la entrada de la ruta actual", async () => {
    expect(await links([{ code: "users.view", scopes: [] }])).toEqual([
      ["/o/acme", null],
      ["/o/acme/miembros", "page"],
    ]);
    expect(screen.getByRole("banner")).toHaveTextContent("Workspace › Miembros");
    expect(document.title).toBe("Miembros · Acme SAC · Good Doggy CRM"); // sección y organización
  });

  it("la entrada «Invitaciones» pide `users.invite` y `users.manage` a la vez", async () => {
    const both = ["roles.view", "users.manage", "users.view", "users.invite"].map((code) => ({
      code,
      scopes: [],
    }));
    expect(await links(both)).toEqual([
      ["/o/acme", null],
      ["/o/acme/miembros", "page"],
      ["/o/acme/invitaciones", null], // después de «Miembros» y antes de «Roles»
      ["/o/acme/roles", null],
    ]);
    expect(screen.getByRole("link", { name: "Invitaciones" })).toBeVisible();
  });

  it("con uno solo de los dos no hay entrada «Invitaciones»", async () => {
    expect(await links([{ code: "users.manage", scopes: [] }])).toEqual([["/o/acme", null]]);
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

  it("la entrada «Sucursales» pide `organization.view`: administrarlas no basta", async () => {
    expect(await links([{ code: "branches.manage", scopes: [] }])).toEqual([["/o/acme", null]]);
  });

  it("con `organization.view` lista «Sucursales», después de «Roles»", async () => {
    const both = [
      { code: "organization.view", scopes: [] },
      { code: "roles.view", scopes: [] },
    ];
    expect(await links(both)).toEqual([
      ["/o/acme", null],
      ["/o/acme/roles", null],
      ["/o/acme/sucursales", null],
    ]);
    expect(screen.getByRole("link", { name: "Sucursales" })).toBeVisible();
  });

  it("la entrada «Equipos» pide `teams.view`: administrarlos o ver miembros no basta", async () => {
    const without = [
      { code: "teams.manage", scopes: [] },
      { code: "users.view", scopes: [] },
    ];
    expect(await links(without)).toEqual([
      ["/o/acme", null],
      ["/o/acme/miembros", "page"],
    ]);
  });

  it("con `teams.view` lista «Equipos», al final", async () => {
    const both = [
      { code: "teams.view", scopes: [] },
      { code: "organization.view", scopes: [] },
    ];
    expect(await links(both)).toEqual([
      ["/o/acme", null],
      ["/o/acme/sucursales", null],
      ["/o/acme/equipos", null],
    ]);
    expect(screen.getByRole("link", { name: "Equipos" })).toBeVisible();
  });

  it("la entrada «Auditoría» pide `audit.view` y va al final", async () => {
    expect(await links([{ code: "roles.manage", scopes: [] }])).toEqual([["/o/acme", null]]);
  });

  it("con `audit.view` lista «Auditoría», después de «Equipos»", async () => {
    const both = [
      { code: "audit.view", scopes: [] },
      { code: "teams.view", scopes: [] },
    ];
    expect(await links(both)).toEqual([
      ["/o/acme", null],
      ["/o/acme/equipos", null],
      ["/o/acme/auditoria", null],
    ]);
    expect(screen.getByRole("link", { name: "Auditoría" })).toBeVisible();
  });

  it("la entrada «Catálogo» pide `products.view`: administrar marcas no basta", async () => {
    expect(await links([{ code: "brands.manage", scopes: [] }])).toEqual([["/o/acme", null]]);
  });

  it("con `products.view` lista «Catálogo», entre «Equipos» y «Auditoría»", async () => {
    const all = ["audit.view", "products.view", "teams.view"].map((code) => ({ code, scopes: [] }));
    expect(await links(all)).toEqual([
      ["/o/acme", null],
      ["/o/acme/equipos", null],
      ["/o/acme/catalogo/marcas", null],
      ["/o/acme/auditoria", null],
    ]);
    expect(screen.getByRole("link", { name: "Catálogo" })).toBeVisible();
  });

  it("en otra página del catálogo, «Catálogo» es la sección actual, no la página", async () => {
    route.pathname = "/o/acme/catalogo/categorias";
    expect(await links([{ code: "products.view", scopes: [] }])).toEqual([
      ["/o/acme", null],
      ["/o/acme/catalogo/marcas", "true"],
    ]);
    expect(screen.getByRole("banner")).toHaveTextContent("Workspace › Catálogo");
    expect(document.title).toBe("Catálogo · Acme SAC · Good Doggy CRM");
  });

  it("una sección cubre las páginas bajo su ruta, no las rutas que solo empiezan igual", () => {
    const catalog = NAVIGATION.find((item) => item.key === "catalog")!;
    expect(holds(catalog, "/o/acme", "/o/acme/catalogo/marcas")).toBe(true);
    expect(holds(catalog, "/o/acme", "/o/acme/catalogo/categorias")).toBe(true);
    expect(holds(catalog, "/o/acme", "/o/acme/catalogos/x")).toBe(false);
    expect(holds(catalog, "/o/otra", "/o/acme/catalogo/categorias")).toBe(false);
  });

  it("CatalogNav enlaza las páginas del catálogo y marca la abierta, también con la barra final", () => {
    route.pathname = "/o/acme/catalogo/categorias/";
    renderApp(
      <TenantProvider value={ana}>
        <CatalogNav />
      </TenantProvider>,
    );
    const nav = screen.getByRole("navigation", { name: "Secciones del catálogo" });
    const found = within(nav)
      .getAllByRole("link")
      .map((link) => [
        link.textContent,
        link.getAttribute("href"),
        link.getAttribute("aria-current"),
      ]);
    expect(found).toEqual([
      ["Marcas", "/o/acme/catalogo/marcas", null],
      ["Categorías", "/o/acme/catalogo/categorias", "page"],
    ]);
  });
});
