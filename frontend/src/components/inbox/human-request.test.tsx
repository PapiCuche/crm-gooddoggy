import {
  act,
  cleanup,
  configure,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Grant, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";

const BASE = "/api/v1/o/acme/conversations";
const URL = `${BASE}/1/human-request/`;
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: [],
};
const grants = (...codes: string[]): Grant[] =>
  codes.map((code) => ({ code, scopes: ["ORGANIZATION" as const] }));
const VIEW = grants("conversations.view", "users.view");
const rosa = {
  ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
  ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" }, assignment_version: 1 },
  ...{ channel: { id: "ch1", platform: "SANDBOX", name: "Sandbox" }, human_request: null },
  ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: null, version: 1 },
  ...{ last_message_preview: null, last_message_direction: null, standby_since: null },
};
const asked = (reason: string, by = "u1") => ({
  ...{ reason, requested_by_user_id: by, requested_at: "2026-11-22T15:30:00Z" },
});
const luis = {
  ...{ id: "m-u2", status: "ACTIVE", joined_at: "2026-01-01T00:00:00Z", roles: [] },
  ...{ default_branch: null, user: { id: "u2", email: "u2@acme.pe", first_name: "Luis" } },
};

type Reply = { status: number; body?: unknown };
// La conversación vive en el "servidor": el detalle y la lista leen su estado y la ruta lo cambia.
function server(initial: Record<string, unknown> = {}, refuse?: Reply) {
  const state: Record<string, unknown> = { ...rosa, ...initial };
  const change = (open: boolean) => (): Reply => {
    if (refuse) return refuse;
    if (open && !state.human_request) state.human_request = asked("AGENT");
    if (!open) state.human_request = null;
    return { status: 200, body: { id: "1", human_request: state.human_request } };
  };
  const api = mockApi({
    [`GET ${BASE}/`]: () => ({ status: 200, body: { results: [{ ...state }], next: null } }),
    [`GET ${BASE}/1/`]: () => ({ status: 200, body: { ...state } }),
    [`GET ${BASE}/1/messages/`]: { status: 200, body: { results: [], next: null } },
    [`GET /api/v1/o/acme/members/?limit=200`]: { status: 200, body: { results: [luis] } },
    [`POST ${URL}`]: change(true),
    [`DELETE ${URL}`]: change(false),
  });
  const calls = (method: string) =>
    api.mock.calls.filter(([url, init]) => init?.method === method && String(url) === URL);
  // Ningún camino de aquí escribe al cliente: ni mensajes, ni medios, ni notas.
  const sent = () =>
    api.mock.calls.filter(
      ([url, init]) => init?.method === "POST" && /\/1\/(messages|media|notes)\//.test(String(url)),
    );
  return { api, calls, sent, state };
}
const show = (permissions: Grant[] = VIEW) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions }}>
      <Inbox />
    </TenantProvider>,
  );
const openChat = async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz/ }));
  return screen.findByRole("heading", { level: 2 });
};
const header = () => screen.getByRole("heading", { level: 2 }).closest("header")!;
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 30)));

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("atención humana (F8-09b)", () => {
  it("se pide y se cierra desde la conversación, con su estado, sin enviar nada", async () => {
    const s = server();
    show();
    await openChat();
    const button = await screen.findByRole("button", { name: "Solicitar atención humana" });
    act(() => {
      button.click();
      button.click(); // dos clics antes de pintar la espera piden una sola vez
    });
    await screen.findByText(/Se pidió que una persona atienda/);
    const line = within(header()).getByText("Atención humana solicitada:").parentElement!;
    expect(line).toHaveTextContent("La pidió quien atiende. por ti · 22 nov");
    expect(line.querySelector("time")).toHaveAttribute("dateTime", "2026-11-22T15:30:00Z");
    // La lista también lo dice, con el campo que ya trae.
    await waitFor(() => expect(screen.getByText("Pide atención humana")).toBeVisible());
    fireEvent.click(screen.getByRole("button", { name: "Cerrar la solicitud de atención humana" }));
    await screen.findByText("Solicitud de atención humana cerrada.");
    // Otra vía la vuelve a abrir (el copiloto): el aviso de cierre ya no vale.
    s.state.human_request = asked("UPSET");
    fireEvent.click(screen.getByRole("button", { name: "Actualizar mensajes" }));
    await within(header()).findByText("Atención humana solicitada:");
    expect(screen.queryByText("Solicitud de atención humana cerrada.")).toBeNull();
    s.state.human_request = null;
    fireEvent.click(screen.getByRole("button", { name: "Actualizar mensajes" }));
    await waitFor(() =>
      expect(within(header()).queryByText("Atención humana solicitada:")).toBeNull(),
    );
    await waitFor(() => expect(screen.queryByText("Pide atención humana")).toBeNull());
    expect(screen.getByRole("button", { name: "Solicitar atención humana" })).toBeVisible();
    expect(s.calls("POST")).toHaveLength(1);
    expect(s.calls("DELETE")).toHaveLength(1);
    expect(s.sent()).toHaveLength(0);
  });

  it.each([
    ["el cliente pide una persona", asked("ASKED_FOR_PERSON", "u2"), "persona. por Luis"],
    ["el cliente está molesto", asked("UPSET", "u9"), "El cliente parece molesto. 22 nov"],
    ["el copiloto la recomendó", asked("MODEL"), "La recomendó el copiloto. por ti"],
    ["un motivo desconocido", asked("OTRO", "u9"), "La pidió quien atiende. 22 nov"],
  ])("muestra la abierta cuando %s, con quién y cuándo", async (_name, open, text) => {
    server({ human_request: open });
    show();
    await openChat();
    const line = await within(header()).findByText("Atención humana solicitada:");
    await waitFor(() => expect(line.parentElement).toHaveTextContent(text));
    expect(screen.getByText("Pide atención humana")).toBeVisible();
  });

  it.each([
    ["cerrada", { status: 409, body: { code: "CONVERSATION_CLOSED" } }, "está cerrada"],
    ["sin permiso", { status: 403, body: { code: "PERMISSION_DENIED" } }, "No puedes cambiar"],
    ["fuera de alcance", { status: 404, body: { code: "NOT_FOUND" } }, "No puedes cambiar"],
  ])("explica una conversación %s y vuelve a leerla", async (_name, refuse, text) => {
    const s = server({}, refuse);
    show();
    await openChat();
    await settle();
    const reads = () => s.api.mock.calls.filter(([url]) => String(url) === `${BASE}/1/`).length;
    const before = reads();
    fireEvent.click(await screen.findByRole("button", { name: "Solicitar atención humana" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    await waitFor(() => expect(reads()).toBeGreaterThan(before));
    expect(s.sent()).toHaveLength(0);
  });

  it("en una conversación cerrada no se pide, pero una abierta se puede cerrar", async () => {
    server({ status: "CLOSED" });
    show();
    await openChat();
    await settle();
    expect(screen.queryByRole("button", { name: "Solicitar atención humana" })).toBeNull();
    cleanup();
    server({ status: "CLOSED", human_request: asked("AGENT") });
    show();
    await openChat();
    expect(
      await screen.findByRole("button", { name: "Cerrar la solicitud de atención humana" }),
    ).toBeVisible();
  });
});
