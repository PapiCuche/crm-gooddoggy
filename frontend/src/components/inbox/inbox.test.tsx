import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";

const LIST = "GET /api/v1/o/acme/conversations/";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "conversations.view", scopes: [] }],
};
const page = (results: unknown[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
const conversation = (id: string, changes: Partial<Conversation> = {}) =>
  ({
    ...{ id, number: `CONV-0000${id}`, status: "OPEN", attention_mode: "HUMAN" },
    ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
    ...{ contact: { id: `c${id}`, display_name: "Rosa Díaz" } },
    ...{ channel: { id: "ch1", platform: "SANDBOX", name: "Sandbox" } },
    ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: "2026-11-04T15:30:00Z" },
    ...{ last_message_preview: "Hola, ¿tienen collares?", last_message_direction: "INBOUND" },
    ...{ version: 1, ...changes },
  }) as Conversation;
const rosa = conversation("1");
const show = (codes = ["conversations.view"]) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions: codes.map((code) => ({ code, scopes: [] })) }}>
      <Inbox />
    </TenantProvider>,
  );
const open = async (name: string | RegExp) => {
  fireEvent.click(await screen.findByRole("button", { name }));
  return screen.findByRole("heading", { level: 2 });
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Inbox, lista (F6-03a)", () => {
  it("carga, muestra cada conversación con su vista previa y marca la elegida", async () => {
    const luna = conversation("2", {
      ...{ contact: { id: "c2", display_name: "Luis Ramos" }, assigned_user_id: "u1" },
      ...{ assignee_kind: "USER", last_message_preview: "Sí, hay", status: "PENDING" },
      ...{ last_message_direction: "OUTBOUND" },
    });
    mockApi({ [LIST]: page([rosa, luna]) });
    show();
    expect(screen.getByRole("status")).toHaveTextContent("Cargando conversaciones…");
    const row = await screen.findByRole("button", { name: /Rosa Díaz/ });
    expect(row).toHaveTextContent("Hola, ¿tienen collares?");
    expect(row).toHaveTextContent("CONV-00001SandboxAbiertaSin asignar");
    expect(screen.getByRole("button", { name: /Luis Ramos/ })).toHaveTextContent(
      "Tú: Sí, hayCONV-00002SandboxPendienteAsignada a ti",
    );
    expect(row).not.toHaveAttribute("aria-current");
    const heading = await open(/Rosa Díaz/);
    expect(row).toHaveAttribute("aria-current", "true");
    expect(heading).toHaveTextContent("Rosa Díaz");
    expect(heading).toHaveFocus(); // en 390 px la lista se oculta: el foco va al panel
    // F6-03b trae los mensajes: hasta entonces el panel lo dice, no ofrece un botón roto.
    expect(screen.getByText(/La lectura de los mensajes llega/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Volver a las conversaciones" }));
    await waitFor(() => expect(row).toHaveFocus());
    expect(screen.queryByRole("heading", { level: 2 })).toBeNull();
  });

  it("vacío, fallo con reintento y 403 sin reintento", async () => {
    let reply: { status: number; body?: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    mockApi({ [LIST]: () => reply });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    reply = page([]);
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByText("Todavía no hay conversaciones.")).toBeVisible();
    cleanup();
    mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver las conversaciones de esta organización.",
    );
    expect(screen.queryByRole("button", { name: "Reintentar" })).toBeNull();
  });

  it("la bandeja y el estado son filtros de la API y se combinan", async () => {
    const api = mockApi({
      [LIST]: page([rosa]),
      [`${LIST}?box=mine`]: page([]),
      [`${LIST}?box=mine&status=OPEN`]: page([conversation("3")]),
    });
    show();
    await screen.findByRole("button", { name: /Rosa Díaz/ });
    expect(screen.getByRole("button", { name: "Todas" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Mías" }));
    expect(await screen.findByText("Ninguna conversación coincide con los filtros.")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Estado"), { target: { value: "OPEN" } });
    expect(await screen.findByText("CONV-00003")).toBeVisible();
    expect(screen.getByRole("button", { name: "Mías" })).toHaveAttribute("aria-pressed", "true");
    expect(api.mock.calls.map(([url]) => String(url))).toContain(
      "/api/v1/o/acme/conversations/?box=mine&status=OPEN",
    );
  });

  it("«Cargar más» pide la página siguiente y, si falla, no borra lo ya mostrado", async () => {
    let next: { status: number; body?: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    mockApi({
      [LIST]: page([rosa], "c1"),
      [`${LIST}?cursor=c1`]: () => next,
    });
    show();
    await screen.findByRole("button", { name: /Rosa Díaz/ });
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    expect(screen.getByRole("button", { name: /Rosa Díaz/ })).toBeVisible(); // sigue ahí
    next = page([conversation("4", { contact: { id: "c4", display_name: "Marta Paz" } })]);
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByRole("button", { name: /Marta Paz/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /Rosa Díaz/ })).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "Cargar más" })).toBeNull();
  });

  it("sin permiso sobre el contacto explica en vez de dejar un hueco", async () => {
    const hidden = conversation("5", {
      ...{ contact: { id: "c5", display_name: null }, last_message_preview: null },
    });
    mockApi({ [LIST]: page([hidden]) });
    show();
    const row = await screen.findByRole("button", { name: /Cliente no disponible/ });
    expect(row).toHaveTextContent("Sin permiso para ver el contenido");
    expect(await open(/Cliente no disponible/)).toHaveTextContent("Cliente no disponible");
    cleanup();
    // aunque el servidor mandara la vista previa sin el nombre, la pantalla no la pinta
    const leaky = conversation("7", { contact: { id: "c7", display_name: null } });
    mockApi({ [LIST]: page([leaky]) });
    show();
    const hiddenRow = await screen.findByRole("button", { name: /Cliente no disponible/ });
    expect(hiddenRow).not.toHaveTextContent("Hola, ¿tienen collares?");
  });

  it("el texto hostil se pinta como texto, aislado y sin ejecutarse", async () => {
    const hostile = "\u202Egnp.exe <img src=x onerror=alert(1)> <b>negrita</b>\u200B\nlínea 2";
    const evil = conversation("6", {
      ...{ contact: { id: "c6", display_name: "<script>x</script>\u202E" } },
      ...{ last_message_preview: hostile },
    });
    mockApi({ [LIST]: page([evil]) });
    const { container } = show();
    const row = await screen.findByRole("button", { name: /script/ });
    expect(row).toHaveTextContent(hostile.replace("\n", " "));
    const previews = Array.from(row.querySelectorAll("bdi"));
    expect(previews.map((bdi) => bdi.textContent)).toEqual([
      "<script>x</script>\u202E", // el nombre del cliente también va aislado
      hostile, // tal cual, sin interpretar
      "Sandbox", // y el nombre del canal
    ]);
    for (const bdi of previews) {
      expect(bdi).toHaveAttribute("dir", "auto");
      expect(bdi).toHaveClass("whitespace-pre-wrap", "wrap-anywhere");
    }
    expect(container.querySelector("img, script, b")).toBeNull();
  });
});
