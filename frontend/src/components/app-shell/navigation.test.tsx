import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CatalogNav } from "@/components/catalog/catalog-nav";
import { InventoryNav } from "@/components/inventory/inventory-nav";
import { PricingNav } from "@/components/pricing/pricing-nav";
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

  it("«Inventario» pide `inventory.view`, va tras «Catálogo» y cubre sus pestañas", async () => {
    route.pathname = "/o/acme/inventario/almacenes";
    const all = ["audit.view", "inventory.view", "products.view"].map((code) => ({
      code,
      scopes: [],
    }));
    expect((await links(all)).slice(1)).toEqual([
      ["/o/acme/catalogo/marcas", null],
      ["/o/acme/inventario/existencias", "true"],
      ["/o/acme/auditoria", null],
    ]);
    const inventory = NAVIGATION.find((item) => item.key === "inventory")!;
    expect(holds(inventory, "/o/acme", "/o/acme/inventarios")).toBe(false);
    renderApp(
      <TenantProvider value={ana}>
        <InventoryNav />
      </TenantProvider>,
    );
    const nav = screen.getByRole("navigation", { name: "Secciones del inventario" });
    const link = within(nav).getByRole("link", { name: "Almacenes", current: "page" });
    expect(link).toHaveAttribute("href", "/o/acme/inventario/almacenes");
  });

  it("«Clientes» pide `contacts.view` y va entre «Equipos» y «Catálogo»", async () => {
    route.pathname = "/o/acme/clientes";
    const all = ["contacts.create", "products.view", "teams.view"].map((code) => ({
      code,
      scopes: [],
    }));
    expect((await links(all)).map(([href]) => href)).not.toContain("/o/acme/clientes");
    cleanup();
    expect((await links([...all, { code: "contacts.view", scopes: [] }])).slice(1)).toEqual([
      ["/o/acme/equipos", null],
      ["/o/acme/clientes", "page"],
      ["/o/acme/catalogo/marcas", null],
    ]);
    expect(document.title).toBe("Clientes · Acme SAC · Good Doggy CRM");
    cleanup();
    route.pathname = "/o/acme/clientes/6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9c10"; // la ficha
    const card = await links([...all, { code: "contacts.view", scopes: [] }]);
    expect(card.find(([href]) => href === "/o/acme/clientes")).toEqual([
      "/o/acme/clientes",
      "true",
    ]);
    expect(document.title).toBe("Clientes · Acme SAC · Good Doggy CRM");
  });

  it("«Precios» sale con cualquiera de los permisos de lectura o `prices.manage`, tras «Inventario»", async () => {
    route.pathname = "/o/acme/precios/listas";
    const without = ["prices.bulk_update", "product_cost.view", "inventory.view"];
    expect((await links(without.map((code) => ({ code, scopes: [] })))).flat()).not.toContain(
      "/o/acme/precios/listas",
    );
    for (const code of ["view", "view_wholesale", "view_distributor", "view_internal", "manage"]) {
      cleanup();
      const all = ["inventory.view", `prices.${code}`, "audit.view"].map((one) => ({
        code: one,
        scopes: [],
      }));
      expect((await links(all)).slice(1)).toEqual([
        ["/o/acme/inventario/existencias", null],
        ["/o/acme/precios/listas", "page"],
        ["/o/acme/auditoria", null],
      ]);
    }
    const prices = NAVIGATION.find((item) => item.key === "prices")!;
    expect(holds(prices, "/o/acme", "/o/acme/precios/listas/x")).toBe(true);
    cleanup();
    renderApp(
      <TenantProvider value={ana}>
        <PricingNav />
      </TenantProvider>,
    );
    const nav = screen.getByRole("navigation", { name: "Secciones de precios" });
    const link = within(nav).getByRole("link", { name: "Listas", current: "page" });
    expect(link).toHaveAttribute("href", "/o/acme/precios/listas");
  });

  it("la raíz de una sección (`/precios`, los precios de una lista) también es de su entrada", async () => {
    route.pathname = "/o/acme/precios";
    const all = ["inventory.view", "prices.view"].map((code) => ({ code, scopes: [] }));
    expect((await links(all)).slice(1)).toEqual([
      ["/o/acme/inventario/existencias", null],
      ["/o/acme/precios/listas", "true"],
    ]);
    expect(document.title).toBe("Precios · Acme SAC · Good Doggy CRM");
    const [prices, inventory, catalog] = ["prices", "inventory", "catalog"].map((key) =>
      NAVIGATION.find((item) => item.key === key)!,
    );
    expect(holds(prices!, "/o/acme", "/o/acme/precioss")).toBe(false);
    expect(holds(prices!, "/o/otra", "/o/acme/precios")).toBe(false);
    expect(holds(inventory!, "/o/acme", "/o/acme/precios")).toBe(false);
    expect(holds(catalog!, "/o/acme", "/o/acme/catalogo")).toBe(true);
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
      ["Productos", "/o/acme/catalogo/productos", null],
    ]);
  });
});
