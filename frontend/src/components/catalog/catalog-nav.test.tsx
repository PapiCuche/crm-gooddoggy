import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext } from "@/lib/api/model";
import { renderApp } from "@/test-utils";

import { CatalogNav } from "./catalog-nav";

vi.mock("next/navigation", () => ({ usePathname: () => "/o/acme/catalogo/categorias/" }));
const ana: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "products.view", scopes: [] }],
};

describe("CatalogNav", () => {
  it("enlaza las páginas del catálogo y marca la abierta, también con la barra final", () => {
    renderApp(
      <TenantProvider value={ana}>
        <CatalogNav />
      </TenantProvider>,
    );
    const nav = screen.getByRole("navigation", { name: "Secciones del catálogo" });
    const links = within(nav)
      .getAllByRole("link")
      .map((link) => [
        link.textContent,
        link.getAttribute("href"),
        link.getAttribute("aria-current"),
      ]);
    expect(links).toEqual([
      ["Marcas", "/o/acme/catalogo/marcas", null],
      ["Categorías", "/o/acme/catalogo/categorias", "page"],
    ]);
  });
});
