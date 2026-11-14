import { fireEvent, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Contact, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { ContactCard } from "./contact-card";

const ID = "6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9c10";
const CARD = `GET /api/v1/o/acme/contacts/${ID}/`;
const LIST = `GET /api/v1/o/acme/opportunities/?contact_id=${ID}&limit=200`;
const PIPELINES = "GET /api/v1/o/acme/pipelines/?limit=200";
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: grants(...codes),
});
const rosa = {
  ...{ id: ID, display_name: "Rosa Díaz", first_name: "Rosa", last_name: "Díaz" },
  ...{ primary_phone_e164: null, primary_email: null, document_type: null },
  ...{ document_number: null, owner_user_id: null, branch_id: null, source: "WALK_IN" },
  ...{ created_by_user_id: "u1", version: 1, tags: [] },
  ...{ created_at: "2026-10-23T10:00:00Z", updated_at: "2026-10-24T20:30:00Z" },
} as Contact;
const deal = {
  ...{ id: "1", number: "OPP-000001", title: "Collar para Rocky", status: "OPEN", amount: "10.00" },
  ...{ stage: { id: "s1", name: "Nuevo", type: "OPEN" }, currency: "PEN", assigned_user_id: null },
  ...{ next_action: null, stage_entered_at: "2026-10-30T10:00:00Z", lost_reason: null },
};
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const open = (...codes: string[]) =>
  renderApp(
    <TenantProvider value={tenant(...codes)}>
      <ContactCard contactId={ID} />
    </TenantProvider>,
  );
const tabs = () => within(screen.getByRole("tablist")).getAllByRole("tab");

afterEach(() => vi.unstubAllGlobals());

describe("ficha del cliente: pestaña «Oportunidades» (F5-06)", () => {
  it("con `opportunities.view` aparece tras «Notas» y no pide nada hasta abrirla", async () => {
    const api = mockApi({
      [CARD]: { status: 200, body: rosa },
      [PIPELINES]: page([]),
      [LIST]: page([deal]),
    });
    open("contacts.view", "opportunities.view");
    await screen.findByRole("heading", { level: 1, name: "Rosa Díaz" });
    expect(tabs().map((tab) => tab.textContent)).toEqual(["Resumen", "Notas", "Oportunidades"]);
    expect(api.mock.calls.map(([url]) => String(url))).toEqual([CARD.slice(4)]);
    fireEvent.click(screen.getByRole("tab", { name: "Oportunidades" }));
    expect(await screen.findByText("Collar para Rocky")).toBeVisible();
    const panel = screen.getByRole("tabpanel", { name: "Oportunidades" });
    expect(panel).toContainElement(
      screen.getByRole("heading", { level: 2, name: "Oportunidades" }),
    );
    expect(screen.getAllByRole("tabpanel", { hidden: true })[0]).toHaveAttribute("hidden");
    // Sigue montada al cambiar de pestaña: volver a ella no repite la petición.
    fireEvent.click(screen.getByRole("tab", { name: "Resumen" }));
    fireEvent.click(screen.getByRole("tab", { name: "Oportunidades" }));
    expect(api.mock.calls.filter(([url]) => String(url) === LIST.slice(4))).toHaveLength(1);
    // Las flechas llegan a ella desde «Notas».
    fireEvent.click(screen.getByRole("tab", { name: "Notas" }));
    fireEvent.keyDown(screen.getByRole("tablist"), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Oportunidades" })).toHaveFocus();
  });

  it("sin `opportunities.view` no hay pestaña ni petición de oportunidades", async () => {
    const api = mockApi({ [CARD]: { status: 200, body: rosa } });
    open("contacts.view");
    await screen.findByRole("heading", { level: 1, name: "Rosa Díaz" });
    expect(tabs().map((tab) => tab.textContent)).toEqual(["Resumen", "Notas"]);
    expect(screen.queryByRole("tab", { name: "Oportunidades" })).toBeNull();
    expect(api).toHaveBeenCalledTimes(1);
  });

  it("si pierde `opportunities.view` con la pestaña abierta vuelve a «Resumen»", async () => {
    mockApi({ [CARD]: { status: 200, body: rosa }, [PIPELINES]: page([]), [LIST]: page([deal]) });
    function Harness() {
      const [codes, setCodes] = useState(["contacts.view", "opportunities.view"]);
      return (
        <TenantProvider value={tenant(...codes)}>
          <button onClick={() => setCodes(["contacts.view"])}>Quitar permiso</button>
          <ContactCard contactId={ID} />
        </TenantProvider>
      );
    }
    renderApp(<Harness />);
    await screen.findByRole("heading", { level: 1, name: "Rosa Díaz" });
    fireEvent.click(screen.getByRole("tab", { name: "Oportunidades" }));
    expect(await screen.findByText("Collar para Rocky")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Quitar permiso" }));
    expect(screen.queryByRole("tab", { name: "Oportunidades" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Resumen" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByText("Collar para Rocky")).toBeNull();
  });
});
