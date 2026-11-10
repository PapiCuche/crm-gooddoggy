import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, Grant, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { CustomerPanel } from "./customer-panel";
import { Inbox } from "./inbox";

// Espera a la API simulada (lista, detalle, ficha y oportunidades): en una máquina cargada el
// segundo por omisión no alcanza. Solo en este archivo y sin reintentar nada.
configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

const BASE = "/api/v1/o/acme";
const CONTACT = `GET ${BASE}/contacts/c1/`;
const DEALS = `GET ${BASE}/opportunities/?contact_id=c1&limit=200`;
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [],
};
const grants = (...codes: string[]): Grant[] => codes.map((code) => ({ code, scopes: [] }));
const ALL = [
  ...grants("conversations.view", "contacts.view", "opportunities.view", "opportunities.create"),
  { code: "messages.send", scopes: ["ORGANIZATION" as const] }, // el cuadro de respuesta pide un alcance
];
const row = (contact: { id: string; display_name: string | null }) =>
  ({
    ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN", version: 1 },
    ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null, contact },
    ...{ channel: { id: "ch1", platform: "SANDBOX", name: "Sandbox" }, assignment_version: 1 },
    ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: null },
    ...{ last_message_preview: null, last_message_direction: null },
  }) as unknown as Conversation;
const person = (changes: Record<string, unknown> = {}) => ({
  ...{ id: "c1", display_name: "Rosa Díaz", first_name: "Rosa", last_name: "Díaz" },
  ...{ primary_phone_e164: "+51999111222", primary_email: "rosa@acme.pe", tags: [] },
  ...{ document_type: null, document_number: null, owner_user_id: null, branch_id: null },
  ...{ source: "MANUAL", created_by_user_id: null, created_at: "x", updated_at: "x", version: 1 },
  ...changes,
});
const deal = {
  ...{ id: "d1", number: "OPP-000001", title: "Collar para Rocky", status: "OPEN" },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" }, pipeline_id: "p1" },
  ...{ stage: { id: "s1", name: "Nuevo", type: "OPEN" }, amount: "150.00", currency: "PEN" },
  ...{ assigned_user_id: "u1", next_action: null, lost_reason: null, version: 1 },
  ...{ stage_entered_at: "2026-10-30T10:00:00Z" },
};
const pipeline = {
  ...{ id: "p1", name: "Ventas", code: "p1", currency: "PEN", is_default: true, is_active: true },
  ...{ sort_order: 0, version: 1, kind: "RETAIL" },
  stages: [{ id: "s1", name: "Nuevo", is_active: true, stage_type: "OPEN", sort_order: 0 }],
};
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
function server(
  options: {
    contact?: { id: string; display_name: string | null };
    extra?: Record<string, never> | object;
  } = {},
) {
  const contact = options.contact ?? { id: "c1", display_name: "Rosa Díaz" };
  return mockApi({
    [`GET ${BASE}/conversations/`]: page([row(contact)]),
    [`GET ${BASE}/conversations/1/`]: { status: 200, body: row(contact) },
    [`GET ${BASE}/conversations/1/messages/`]: page([]),
    [CONTACT]: { status: 200, body: person() },
    [DEALS]: page([deal]),
    [`GET ${BASE}/pipelines/?limit=200`]: page([pipeline]),
    ...options.extra,
  });
}
const show = (permissions: Grant[] = ALL) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions }}>
      <Inbox />
    </TenantProvider>,
  );
const open = async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz|Cliente no disponible/ }));
  return screen.findByRole("heading", { level: 2 });
};
const toggle = () => screen.findByRole("button", { name: "Cliente y oportunidades" });
const calls = (api: ReturnType<typeof mockApi>) => api.mock.calls.map(([url]) => String(url));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("panel del cliente de la conversación (F6-06)", () => {
  it("enseña el resumen del cliente, su ficha y sus oportunidades con crear, sin salir del Inbox", async () => {
    server();
    show();
    await open();
    fireEvent.click(await toggle());
    const panel = await screen.findByRole("region", { name: "Cliente de la conversación" });
    expect(within(panel).getByRole("heading", { name: "Rosa Díaz" })).toBeVisible();
    expect(await within(panel).findByText("+51999111222")).toBeVisible();
    expect(within(panel).getByText("rosa@acme.pe")).toBeVisible();
    expect(within(panel).getByRole("link", { name: "Ver ficha del cliente" })).toHaveAttribute(
      "href",
      "/o/acme/clientes/c1",
    );
    // sus oportunidades (la pestaña de la ficha) con enlace al detalle y crear una nueva
    const link = await screen.findByRole("link", { name: "Collar para Rocky" });
    expect(link).toHaveAttribute("href", "/o/acme/oportunidades/d1");
    expect(await screen.findByRole("button", { name: /Nueva oportunidad/ })).toBeVisible();
  });

  it("el chat sigue montado: el borrador se conserva y el foco vuelve al botón que abrió el panel", async () => {
    server();
    show();
    await open();
    const draft = await screen.findByRole("textbox", { name: "Responder al cliente" });
    fireEvent.change(draft, { target: { value: "Hola Rosa, ya te escribo" } });
    fireEvent.click(await toggle());
    await screen.findByRole("region", { name: "Cliente de la conversación" });
    expect(screen.queryByRole("textbox", { name: "Responder al cliente" })).toBeNull(); // oculto
    fireEvent.click(screen.getByRole("button", { name: "Volver a la conversación" }));
    expect(await screen.findByRole("textbox", { name: "Responder al cliente" })).toHaveValue(
      "Hola Rosa, ya te escribo",
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Cliente y oportunidades" })).toHaveFocus(),
    );
  });

  it("sin nombre del contacto (D-F6-10) no hay panel y no se pide nada suyo", async () => {
    const api = server({ contact: { id: "c1", display_name: null } });
    show();
    await open();
    await screen.findByText(/No tienes permiso para ver a este cliente/);
    expect(screen.queryByRole("button", { name: "Cliente y oportunidades" })).toBeNull();
    expect(
      calls(api).filter((url) => url.includes("/contacts/") || url.includes("/opportunities/")),
    ).toEqual([]);
  });

  it("abrir el panel lleva el foco a su título; elegir otra conversación vuelve al chat", async () => {
    server();
    show();
    await open();
    fireEvent.click(await toggle());
    const panel = await screen.findByRole("region", { name: "Cliente de la conversación" });
    await waitFor(() =>
      expect(within(panel).getByRole("heading", { name: "Rosa Díaz" })).toHaveFocus(),
    );
    fireEvent.click(screen.getByRole("button", { name: /Rosa Díaz/ })); // la fila de la lista
    expect(await screen.findByRole("textbox", { name: "Responder al cliente" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "Cliente de la conversación" })).toBeNull();
  });

  it("si la fila ya no trae el nombre del contacto, el panel no pide nada suyo", async () => {
    const api = server();
    renderApp(
      <TenantProvider value={{ ...tenant, permissions: ALL }}>
        <CustomerPanel
          contact={{ id: "c1", display_name: null }}
          headingRef={{ current: null }}
          onBack={() => {}}
        />
      </TenantProvider>,
    );
    expect(
      await screen.findByText(/No tienes permiso para ver los datos de este cliente/),
    ).toBeVisible();
    expect(
      calls(api).filter((url) => url.includes("/contacts/") || url.includes("/opportunities/")),
    ).toEqual([]);
  });

  it("sin `contacts.view` no se pide la ficha, se explica, y las oportunidades siguen", async () => {
    const api = server();
    show(grants("conversations.view", "opportunities.view"));
    await open();
    fireEvent.click(await toggle());
    expect(
      await screen.findByText(/No tienes permiso para ver los datos de este cliente/),
    ).toBeVisible();
    expect(calls(api).some((url) => url.includes("/contacts/c1/"))).toBe(false);
    expect(await screen.findByRole("link", { name: "Collar para Rocky" })).toBeVisible();
  });

  it.each([403, 404])(
    "un %i de la ficha se explica sin enseñar nada del cliente",
    async (status) => {
      server({ extra: { [CONTACT]: { status, body: { code: "PERMISSION_DENIED" } } } });
      show();
      await open();
      fireEvent.click(await toggle());
      expect(
        await screen.findByText(/No tienes permiso para ver los datos de este cliente/),
      ).toBeVisible();
      expect(screen.queryByText("+51999111222")).toBeNull();
    },
  );

  it("sin `opportunities.view` no se piden las oportunidades y se dice", async () => {
    const api = server();
    show(grants("conversations.view", "contacts.view"));
    await open();
    fireEvent.click(await toggle());
    expect(await screen.findByText("No tienes permiso para ver las oportunidades.")).toBeVisible();
    expect(calls(api).some((url) => url.includes("/opportunities/"))).toBe(false);
  });

  it("lo que escribió el cliente (nombre, correo, etiquetas) va como texto aislado", async () => {
    const hostile = "<img src=x onerror=alert(1)>‮abc";
    server({
      contact: { id: "c1", display_name: hostile },
      extra: {
        [CONTACT]: {
          status: 200,
          body: person({
            primary_email: hostile,
            tags: [{ id: "t1", name: hostile, color: "#000" }],
          }),
        },
      },
    });
    const { container } = show();
    fireEvent.click(await screen.findByRole("button", { name: /img/ }));
    fireEvent.click(await toggle());
    const panel = await screen.findByRole("region", { name: "Cliente de la conversación" });
    await within(panel).findByText("Correo");
    const isolated = [...panel.querySelectorAll("bdi")].filter((one) =>
      one.textContent?.includes(hostile),
    );
    expect(isolated.length).toBeGreaterThanOrEqual(3); // nombre, correo y etiqueta
    expect(container.querySelector("img")).toBeNull();
  });
});
