import { act, cleanup, configure, fireEvent, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, Grant, Message, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

const BASE = "/api/v1/o/acme/conversations";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: [{ code: "conversations.view", scopes: [] }],
};
const conversation = (preview: string) =>
  ({
    ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
    ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
    ...{ contact: { id: "c1", display_name: "Rosa Díaz" }, version: 1 },
    ...{
      channel: {
        id: "ch1",
        platform: "SANDBOX",
        name: "Sandbox",
        text_max: 4096,
        text_unit: "CHARACTERS",
        accepts_media: true,
      },
    },
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
  send() {}
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

  it("un mensaje nuevo llega a la lista y al chat sin recargar, y avisa si se pidió", async () => {
    vi.stubEnv("NEXT_PUBLIC_INBOX_WS", "on");
    const notified = vi.fn();
    vi.stubGlobal("Notification", Object.assign(notified, { permission: "granted" }));
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    localStorage.setItem("inbox.notify", "on");
    const { state } = server();
    show();
    await openChat();
    act(() => socket().onopen?.({}));
    await say({ type: "conversation.updated", conversation_id: "1" });
    expect(notified).not.toHaveBeenCalled(); // solo un mensaje entrante avisa
    state.preview = "Segundo";
    state.texts.push(message("m2", "Segundo"));
    await say({ type: "message.received", conversation_id: "1", message_id: "m2" });
    expect(await screen.findAllByText("Segundo")).not.toHaveLength(0);
    await waitFor(() => expect(screen.getAllByText("Segundo").length).toBeGreaterThanOrEqual(2));
    expect(notified).toHaveBeenCalledExactlyOnceWith("Nuevo mensaje", {
      ...{ tag: "inbox.notify", renotify: true },
    });
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("un aviso que el navegador no deja crear no frena la relectura", async () => {
    vi.stubEnv("NEXT_PUBLIC_INBOX_WS", "on");
    const illegal = vi.fn(() => {
      throw new TypeError("Illegal constructor"); // Chrome en Android
    });
    vi.stubGlobal("Notification", Object.assign(illegal, { permission: "granted" }));
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    localStorage.setItem("inbox.notify", "on");
    const { state } = server();
    show();
    await openChat();
    state.preview = "Tercero";
    state.texts.push(message("m3", "Tercero"));
    await say({ type: "message.received", conversation_id: "1", message_id: "m3" });
    await waitFor(() => expect(screen.getAllByText("Tercero").length).toBeGreaterThanOrEqual(2));
    expect(illegal).toHaveBeenCalledTimes(1);
    localStorage.clear();
    vi.restoreAllMocks();
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

describe("ráfagas y estado de la conexión (UX-INBOX-06)", () => {
  const states = () => screen.queryAllByRole("status").map((one) => one.textContent);
  const NONE = /Reconectando|Sin conexión|no disponible/;
  const wait = (ms: number) => act(() => new Promise((resolve) => setTimeout(resolve, ms)));
  const fake = () =>
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"], shouldAdvanceTime: true });
  const reconnect = async () => {
    act(() => socket().onclose?.({ code: 1006 }));
    await act(() => vi.advanceTimersByTimeAsync(1000));
    act(() => socket().onopen?.({}));
  };
  beforeEach(() => vi.stubEnv("NEXT_PUBLIC_INBOX_WS", "on"));
  afterEach(() => vi.useRealTimers());

  it("tres eventos seguidos: una sola lectura de cada clave, al cerrar la ventana", async () => {
    const { reads, state } = server();
    show();
    await openChat();
    act(() => socket().onopen?.({}));
    const before = [reads(""), reads("1/"), reads("1/messages/")];
    const messages = reads("1/messages/");
    state.texts.push(message("m2", "Dos"), message("m3", "Tres"));
    await say({ type: "message.received", conversation_id: "1", message_id: "m2" });
    await say({ type: "message.updated", conversation_id: "1", message_id: "m2" });
    await say({ type: "message.received", conversation_id: "1", message_id: "m3" });
    expect(reads("1/messages/")).toBe(messages); // aún dentro de la ventana
    expect(await screen.findByText("Tres")).toBeInTheDocument();
    await wait(400);
    expect([reads(""), reads("1/"), reads("1/messages/")]).toEqual(before.map((n) => n + 1));
  });

  it("los estados llegan a la pantalla: reconectando, sin red y no disponible; abierta, nada", async () => {
    fake();
    const online = vi.spyOn(navigator, "onLine", "get");
    server();
    show();
    await openChat();
    expect(states().join()).not.toMatch(NONE); // conectando: nada
    act(() => socket().onopen?.({}));
    act(() => socket().onclose?.({ code: 1006 }));
    expect(states()).toContain("Reconectando…"); // en una región `status`, con su icono
    expect(screen.getByText("Reconectando…").querySelector("svg")).not.toBeNull();
    online.mockReturnValue(false);
    act(() => void window.dispatchEvent(new Event("offline")));
    expect(states()).toContain("Sin conexión"); // sin red, eso es lo que se dice
    online.mockReturnValue(true);
    act(() => void window.dispatchEvent(new Event("online")));
    expect(states()).toContain("Reconectando…"); // la red volvió, el socket aún no
    await act(() => vi.advanceTimersByTimeAsync(1000));
    act(() => socket().onopen?.({}));
    expect(states().join()).not.toMatch(NONE);
    act(() => socket().onclose?.({ code: 4403 }));
    expect(states()).toContain("Tiempo real no disponible");
    expect(screen.getByRole("button", { name: "Actualizar mensajes" })).toBeInTheDocument();
    online.mockRestore();
  });

  it("apagado no muestra ningún estado, ni su región", async () => {
    vi.stubEnv("NEXT_PUBLIC_INBOX_WS", "off");
    server();
    show();
    await openChat();
    const heading = screen.getByRole("heading", { level: 1 });
    expect(heading.parentElement!.querySelector("[role=status]")).toBeNull();
  });

  it("una ráfaga pendiente al salir no invalida nada", async () => {
    server();
    const { client } = show();
    await openChat();
    await say({ type: "message.received", conversation_id: "1", message_id: "m2" });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    cleanup();
    await wait(400);
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("otra organización: lo pendiente de la anterior no se relee y su conexión no da estados", async () => {
    const { reads } = server();
    let pick: (slug: string) => void = () => {};
    function Switcher() {
      const [slug, setSlug] = useState("acme");
      pick = setSlug;
      const value = { ...tenant, organization: { id: slug, slug, name: slug } };
      return (
        <TenantProvider key={slug} value={value}>
          <Inbox />
        </TenantProvider>
      );
    }
    renderApp(<Switcher />);
    await openChat();
    const acme = socket();
    act(() => acme.onopen?.({}));
    const before = [reads(""), reads("1/messages/")];
    await say({ type: "message.received", conversation_id: "1", message_id: "m2" });
    act(() => pick("beta"));
    expect(acme.closed).toBe(true);
    expect(socket().url).toBe(`ws://${window.location.host}/ws/o/beta/inbox/`);
    act(() => acme.onclose?.({ code: 1006 })); // el viejo ya no avisa de nada
    await wait(400);
    expect([reads(""), reads("1/messages/")]).toEqual(before);
    expect(states().join()).not.toMatch(NONE);
  });

  it("una caída no pierde la ráfaga pendiente; al volver se relee una vez más", async () => {
    fake();
    const { reads } = server();
    show();
    await openChat();
    act(() => socket().onopen?.({}));
    const [list, detail, messages] = [reads(""), reads("1/"), reads("1/messages/")];
    await say({ type: "message.received", conversation_id: "1", message_id: "m2" });
    await reconnect();
    await act(() => vi.advanceTimersByTimeAsync(1000));
    // la ventana (300 ms) cierra antes del primer reintento (≥ 500 ms): la ráfaga y la reconexión
    await waitFor(() => expect(reads("1/messages/")).toBe(messages + 2));
    expect([reads(""), reads("1/")]).toEqual([list + 2, detail + 2]);
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(reads("1/messages/")).toBe(messages + 2); // y nada más: no se sondea
  });

  it("un envío propio en curso no parpadea ni se duplica cuando llega su evento", async () => {
    const { api, state } = server();
    let release = () => {};
    const answered = new Promise<void>((resolve) => (release = resolve));
    const inner = api.getMockImplementation()!;
    api.mockImplementation(async (input, init) => {
      if (init?.method !== "POST") return inner(input, init);
      const sent = {
        ...message("m9", "Hola Rosa"),
        ...{ direction: "OUTBOUND", sender_type: "USER", sender_user_id: "u1" },
        ...{ client_message_id: JSON.parse(String(init.body)).client_message_id },
      } as Message;
      state.texts.push(sent); // el servidor ya lo guardó; la respuesta tarda
      await answered;
      return new Response(JSON.stringify(sent), { status: 202 });
    });
    document.cookie = "csrftoken=token-de-prueba"; // un envío lleva la cabecera CSRF
    const send: Grant = { code: "messages.send", scopes: ["ORGANIZATION"] };
    renderApp(
      <TenantProvider value={{ ...tenant, permissions: [...tenant.permissions, send] }}>
        <Inbox />
      </TenantProvider>,
    );
    await openChat();
    act(() => socket().onopen?.({}));
    const count = () =>
      [...document.querySelectorAll("ol li")].filter((li) => li.textContent?.includes("Hola R"))
        .length;
    const seen: number[] = [];
    const watcher = new MutationObserver(() => seen.push(count()));
    watcher.observe(document.body, { subtree: true, childList: true, characterData: true });
    const box = screen.getByRole("textbox", { name: "Responder al cliente" });
    fireEvent.change(box, { target: { value: "Hola Rosa" } });
    fireEvent.click(screen.getByRole("button", { name: "Enviar" }));
    await waitFor(() => expect(count()).toBe(1)); // la burbuja del envío en curso
    const reads = () => api.mock.calls.filter(([url]) => url === `${BASE}/1/messages/`).length;
    const before = reads();
    await say({ type: "message.received", conversation_id: "1", message_id: "m9" });
    await wait(400);
    expect(reads()).toBe(before); // mientras sale, sus mensajes no se releen
    await act(async () => release());
    await waitFor(() => expect(reads()).toBe(before + 1)); // al terminar, el cuadro relee una vez
    await wait(400);
    watcher.disconnect();
    expect(count()).toBe(1);
    expect(seen.slice(seen.indexOf(1))).not.toContain(0); // nunca desaparece
    expect(Math.max(...seen)).toBe(1); // nunca dos
  });

  it("con páginas anteriores cargadas, un evento o una reconexión relee cada página cargada", async () => {
    fake();
    const page = (id: string, next: string | null) => () => ({
      ...{ status: 200, body: { results: [message(id, `Texto ${id}`)], next } },
    });
    const api = mockApi({
      [`GET ${BASE}/`]: { status: 200, body: { results: [conversation("Hola")], next: null } },
      [`GET ${BASE}/1/`]: { status: 200, body: conversation("Hola") },
      [`GET ${BASE}/1/messages/`]: page("m3", "p2"),
      [`GET ${BASE}/1/messages/?cursor=p2`]: page("m2", "p3"),
      [`GET ${BASE}/1/messages/?cursor=p3`]: page("m1", null),
    });
    const reads = () =>
      api.mock.calls.filter(([url]) => String(url).startsWith(`${BASE}/1/messages/`)).length;
    show();
    (await screen.findByRole("button", { name: /Rosa Díaz/ })).click();
    for (const id of ["m2", "m1"]) {
      (await screen.findByRole("button", { name: "Cargar mensajes anteriores" })).click();
      await screen.findByText(`Texto ${id}`);
    }
    act(() => socket().onopen?.({}));
    let before = reads();
    await say({ type: "message.received", conversation_id: "1", message_id: "m4" });
    await act(() => vi.advanceTimersByTimeAsync(400));
    await waitFor(() => expect(reads()).toBe(before + 3)); // las tres páginas cargadas
    before = reads();
    await reconnect();
    await waitFor(() => expect(reads()).toBe(before + 3));
  });
});
