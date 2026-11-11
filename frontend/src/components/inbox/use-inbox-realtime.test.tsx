import { cleanup, configure, screen, waitFor, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, Message, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

const BASE = "/api/v1/o/acme/conversations";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "conversations.view", scopes: [] }],
};
const conversation = (preview: string) =>
  ({
    ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
    ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
    ...{ contact: { id: "c1", display_name: "Rosa Díaz" }, version: 1 },
    ...{ channel: { id: "ch1", platform: "SANDBOX", name: "Sandbox" } },
    ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: "2026-11-04T15:30:00Z" },
    ...{ last_message_preview: preview, last_message_direction: "INBOUND" },
  }) as Conversation;
const message = (id: string, body: string) =>
  ({
    ...{ id, direction: "INBOUND", sender_type: "CONTACT", sender_user_id: null },
    ...{ message_type: "TEXT", body, client_message_id: null, delivery_status: null },
    ...{ created_at: "2026-11-04T15:30:00Z", sent_at: null, delivered_at: null },
    ...{ read_at: null, failed_at: null },
  }) as Message;

// El doble del WebSocket global.
class Fake {
  static all: Fake[] = [];
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  closed = false;
  constructor(readonly url: string) {
    Fake.all.push(this);
  }
  close() {
    this.closed = true;
  }
}
const socket = () => Fake.all[Fake.all.length - 1]!;
const say = (body: object) => act(() => socket().onmessage?.({ data: JSON.stringify(body) }));

// El servidor de la prueba: lo que responde cambia con `state`, y se cuentan las lecturas.
function server() {
  const state = { preview: "Hola", texts: [message("m1", "Primero")] };
  const api = mockApi({
    [`GET ${BASE}/`]: () => ({
      status: 200,
      body: { results: [conversation(state.preview)], next: null },
    }),
    [`GET ${BASE}/1/`]: () => ({ status: 200, body: conversation(state.preview) }),
    [`GET ${BASE}/1/messages/`]: () => ({
      status: 200,
      body: { results: [...state.texts].reverse(), next: null },
    }),
  });
  const reads = (suffix: string) =>
    api.mock.calls.filter(
      ([url]) => new URL(String(url), "http://x").pathname === `${BASE}/${suffix}`,
    ).length;
  return { state, api, reads };
}
const show = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <Inbox />
    </TenantProvider>,
  );
const openChat = async () => {
  (await screen.findByRole("button", { name: /Rosa Díaz/ })).click();
  await screen.findByText("Primero");
};

beforeEach(() => {
  Fake.all = [];
  vi.stubGlobal("WebSocket", Fake);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("tiempo real del Inbox (F6-07c)", () => {
  it("apagado por omisión: no abre ninguna conexión", async () => {
    server();
    show();
    await screen.findByRole("button", { name: /Rosa Díaz/ });
    expect(Fake.all).toHaveLength(0);
    cleanup();
    vi.stubEnv("NEXT_PUBLIC_INBOX_WS", "off");
    show();
    await screen.findByRole("button", { name: /Rosa Díaz/ });
    expect(Fake.all).toHaveLength(0);
  });

  it("encendido: abre /ws/o/{slug}/inbox/ del mismo origen y la cierra al salir", async () => {
    vi.stubEnv("NEXT_PUBLIC_INBOX_WS", "on");
    server();
    show();
    await screen.findByRole("button", { name: /Rosa Díaz/ });
    expect(Fake.all).toHaveLength(1);
    expect(socket().url).toBe(`ws://${window.location.host}/ws/o/acme/inbox/`);
    cleanup();
    expect(socket().closed).toBe(true);
  });

  it("un mensaje nuevo llega a la lista y al chat sin recargar", async () => {
    vi.stubEnv("NEXT_PUBLIC_INBOX_WS", "on");
    const { state } = server();
    show();
    await openChat();
    act(() => socket().onopen?.({}));
    state.preview = "Segundo";
    state.texts.push(message("m2", "Segundo"));
    await say({ type: "message.received", conversation_id: "1", message_id: "m2" });
    expect(await screen.findAllByText("Segundo")).not.toHaveLength(0);
    await waitFor(() => expect(screen.getAllByText("Segundo").length).toBeGreaterThanOrEqual(2));
  });

  it("conversation.updated relee lista y detalle, no los mensajes", async () => {
    vi.stubEnv("NEXT_PUBLIC_INBOX_WS", "on");
    const { reads, state } = server();
    show();
    await openChat();
    const before = { list: reads(""), detail: reads("1/"), messages: reads("1/messages/") };
    state.preview = "Cambió";
    await say({ type: "conversation.updated", conversation_id: "1" });
    await waitFor(() => expect(reads("")).toBe(before.list + 1));
    await waitFor(() => expect(reads("1/")).toBe(before.detail + 1));
    expect(reads("1/messages/")).toBe(before.messages);
  });

  it("ignora tipos desconocidos y texto roto: no hay ninguna lectura de más", async () => {
    vi.stubEnv("NEXT_PUBLIC_INBOX_WS", "on");
    const { reads } = server();
    show();
    await openChat();
    const total = reads("") + reads("1/") + reads("1/messages/");
    await say({ type: "typing", conversation_id: "1" });
    act(() => socket().onmessage?.({ data: "{roto" }));
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(reads("") + reads("1/") + reads("1/messages/")).toBe(total);
  });

  it("de otra conversación solo cambia la lista: no relee los mensajes del chat abierto", async () => {
    vi.stubEnv("NEXT_PUBLIC_INBOX_WS", "on");
    const { reads } = server();
    show();
    await openChat();
    const before = { list: reads(""), messages: reads("1/messages/") };
    await say({ type: "message.received", conversation_id: "9", message_id: "m7" });
    await waitFor(() => expect(reads("")).toBe(before.list + 1));
    expect(reads("1/messages/")).toBe(before.messages);
  });

  it("al reconectar relee lista, detalle y mensajes una vez; la primera conexión no", async () => {
    vi.stubEnv("NEXT_PUBLIC_INBOX_WS", "on");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"], shouldAdvanceTime: true });
    try {
      const { reads, state } = server();
      show();
      await openChat();
      const first = { list: reads(""), detail: reads("1/"), messages: reads("1/messages/") };
      act(() => socket().onopen?.({}));
      await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
      expect(reads("")).toBe(first.list); // la primera conexión no relee
      state.preview = "Mientras tanto";
      state.texts.push(message("m3", "Mientras tanto"));
      act(() => socket().onclose?.({ code: 1006 }));
      await act(() => vi.advanceTimersByTimeAsync(1000));
      expect(Fake.all).toHaveLength(2);
      act(() => socket().onopen?.({}));
      expect((await screen.findAllByText("Mientras tanto")).length).toBeGreaterThan(0);
      await waitFor(() => expect(reads("")).toBe(first.list + 1));
      expect(reads("1/")).toBe(first.detail + 1);
      expect(reads("1/messages/")).toBe(first.messages + 1);
      // sin más tráfico no hay más lecturas: no se sondea con la conexión abierta
      await act(() => vi.advanceTimersByTimeAsync(120_000));
      expect(reads("")).toBe(first.list + 1);
    } finally {
      vi.useRealTimers();
    }
  });
});
