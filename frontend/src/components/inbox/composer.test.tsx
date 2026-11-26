import {
  act,
  cleanup,
  configure,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { onlineManager, QueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, Grant, Message, SelfContext } from "@/lib/api/model";
import { hangPosts, mockApi, renderApp } from "@/test-utils";

import { HOLD, keepPending, messagesKey, pausedId } from "./composer";
import { Inbox } from "./inbox";

// Cada vez que se dibuja una fila de la lista (el punto de su red solo está en la fila): UX-INBOX-04b.
const rowRenders = vi.hoisted(() => ({ count: 0 }));
vi.mock("@/components/channels/channel-identity", async (actual) => {
  const real = await actual<typeof import("@/components/channels/channel-identity")>();
  return {
    ...real,
    ChannelDot: (props: Parameters<typeof real.ChannelDot>[0]) => {
      rowRenders.count += 1;
      return real.ChannelDot(props);
    },
  };
});

const BASE = "/api/v1/o/acme/conversations";
const LIST = `GET ${BASE}/`;
const DETAIL = `GET ${BASE}/1/`;
const MESSAGES = `GET ${BASE}/1/messages/`;
const SEND = `POST ${BASE}/1/messages/`;
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
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const rosa = {
  ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
  ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" } },
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
  ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: null, version: 1 },
  ...{ last_message_preview: null, last_message_direction: null },
} as unknown as Conversation;
const juan = { ...rosa, id: "2", contact: { id: "c2", display_name: "Juan Pérez" } };
const saved = (id: string, body: string, changes: Partial<Message> = {}) =>
  ({
    ...{ id, direction: "OUTBOUND", sender_type: "USER", sender_user_id: "u1" },
    ...{ message_type: "TEXT", body, client_message_id: `cm-${id}`, delivery_status: "QUEUED" },
    ...{ created_at: "2026-11-04T16:00:00Z", sent_at: null, delivered_at: null },
    ...{ read_at: null, failed_at: null, ...changes },
  }) as Message;

type Reply = { status: number; body?: unknown };
// El servidor: lo que responde al envío y lo que lista; el envío puede quedar en espera.
function server(options: { conversation?: Partial<Conversation>; send?: () => Reply } = {}) {
  const conversation = { ...rosa, ...options.conversation };
  const stored: Message[] = [];
  let release = () => {};
  let hold: Promise<void> | null = null;
  const api = mockApi({
    [LIST]: page([conversation, juan]),
    [DETAIL]: { status: 200, body: conversation },
    [`GET ${BASE}/2/`]: { status: 200, body: juan },
    [`GET ${BASE}/2/messages/`]: page([]),
    [MESSAGES]: () => page([...stored].reverse()),
    [SEND]: () => {
      const reply = options.send?.() ?? { status: 202, body: saved("s1", "ok") };
      if (reply.status < 300) stored.push(reply.body as Message);
      return reply;
    },
  });
  const inner = api.getMockImplementation()!;
  api.mockImplementation(async (input, init) => {
    if (init?.method === "POST") await hold;
    return inner(input, init);
  });
  const posts = () =>
    api.mock.calls
      .filter(([url, init]) => String(url).endsWith("/1/messages/") && init?.method === "POST")
      .map(
        ([, init]) => JSON.parse(String(init!.body)) as { client_message_id: string; body: string },
      );
  return {
    api,
    stored,
    wait: () => void (hold = new Promise<void>((resolve) => (release = resolve))),
    go: () => release(),
    ids: () => posts().map((post) => post.client_message_id) as [string, string, string],
    posts,
  };
}
const show = (permissions: Grant[] = grants("conversations.view", "messages.send")) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions }}>
      <Inbox />
    </TenantProvider>,
  );
const openChat = async (name = /Rosa Díaz/) => {
  fireEvent.click(await screen.findByRole("button", { name }));
  await waitFor(() => expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(name));
};
const box = (name = "Responder al cliente") => screen.findByRole("textbox", { name });
const type = async (text: string, name?: string) =>
  fireEvent.change(await box(name), { target: { value: text } });
// Lo que un envío indebido haría llega tras unas esperas asíncronas: se deja pasar antes de afirmar.
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 30)));
const sendButton = (name = "Enviar") => screen.getByRole("button", { name });

// Estos tests esperan a la API simulada (la lista, el detalle y los mensajes) y al montaje del chat:
// en una máquina cargada (CI, el gate) el segundo por omisión no alcanza. Se sube lo que esperan los
// `findBy…` y `waitFor` de este archivo y el tiempo de cada test, sin reintentar nada: la espera
// termina en cuanto aparece lo esperado.
configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("cuadro de redacción (F6-04c)", () => {
  const OWN = [
    { code: "messages.send", scopes: ["OWN" as const] },
    ...grants("conversations.view"),
  ];
  const TEAM = [
    { code: "messages.send", scopes: ["TEAM" as const] },
    ...grants("conversations.view"),
  ];
  it.each([
    ["sin `messages.send` no hay cuadro", {}, grants("conversations.view"), false],
    // Sin alcance concedido (lista vacía) falla cerrado, como la API, aunque la conversación sea mía.
    [
      "con una lista de alcances vacía falla cerrado",
      { assigned_user_id: "u1" },
      [{ code: "messages.send", scopes: [] }, ...grants("conversations.view")],
      false,
    ],
    [
      "con alcance OWN no se ofrece en una conversación ajena",
      { assigned_user_id: "u2" },
      OWN,
      false,
    ],
    ["con alcance OWN se ofrece en la propia", { assigned_user_id: "u1" }, OWN, true],
    // Con equipo o sucursal no se sabe desde aquí: se ofrece y la API decide.
    ["con alcance de equipo se ofrece", { assigned_user_id: "u2" }, TEAM, true],
  ])("%s", async (_name, conversation, permissions, offered) => {
    server({ conversation });
    show(permissions);
    await openChat();
    if (offered) {
      expect(await box()).toBeVisible();
    } else {
      expect(await screen.findByText(/No tienes permiso para responder/)).toBeVisible();
      expect(screen.queryByRole("textbox")).toBeNull();
    }
  });

  it("envía con un id nuevo, muestra «En cola» al instante, limpia, enfoca y refresca", async () => {
    const hostile = "<b>hola</b> ‮abc";
    const s = server({ send: () => ({ status: 202, body: saved("s1", hostile) }) });
    show();
    await openChat();
    s.wait();
    await type(hostile);
    fireEvent.click(sendButton());
    // Optimista: ya está en la lista, en cola, y el cuadro no se pierde mientras tanto.
    const log = await screen.findByRole("list", { name: "Mensajes" });
    expect(await within(log).findByText("En cola")).toBeVisible();
    expect(log.querySelector("bdi")?.textContent).toBe(hostile);
    expect(log.querySelector("b")).toBeNull(); // texto, nunca HTML
    expect(sendButton("Enviando…")).toHaveAttribute("aria-disabled", "true");
    expect(await box()).toHaveAttribute("readonly");
    s.go();
    await waitFor(() => expect(box()).resolves.toHaveValue(""));
    expect(await box()).toHaveFocus();
    expect(s.posts()).toEqual([
      { client_message_id: expect.stringMatching(/^[0-9a-f-]{36}$/), body: hostile },
    ]);
    expect(within(log).getAllByRole("listitem")).toHaveLength(1); // sin duplicar tras refrescar
    // El siguiente mensaje lleva otro id.
    await type(hostile); // el mismo texto otra vez es otro mensaje: otro id
    fireEvent.click(sendButton());
    await waitFor(() => expect(s.posts()).toHaveLength(2));
    const [first, second] = s.ids();
    expect(second).not.toBe(first);
  });

  it("si falla conserva el texto, quita el mensaje en cola y «Reintentar» reutiliza el mismo id", async () => {
    let reply: Reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    const s = server({ send: () => reply });
    show();
    await openChat();
    await type("Hola Rosa");
    fireEvent.click(sendButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    expect(await box()).toHaveValue("Hola Rosa");
    expect(screen.queryByText("En cola")).toBeNull();
    reply = { status: 202, body: saved("s1", "Hola Rosa") };
    fireEvent.click(sendButton("Reintentar"));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    await waitFor(() => expect(box()).resolves.toHaveValue(""));
    const [a, b] = s.ids();
    expect(b).toBe(a); // el servidor no lo duplica
    expect(screen.getAllByText("Hola Rosa")).toHaveLength(1);
  });

  it("tras un fallo, sumar texto conserva el id; cambiar lo que falló empieza otro", async () => {
    let reply: Reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    const s = server({ send: () => reply });
    show();
    await openChat();
    await type("uno");
    fireEvent.click(sendButton());
    await screen.findByRole("alert");
    // Lo primero pudo haber llegado: con lo sumado detrás, el mismo id no deja que salga dos veces.
    await type("uno y dos");
    reply = { status: 409, body: { code: "CLIENT_MESSAGE_ID_TAKEN" } };
    fireEvent.click(sendButton());
    expect(await screen.findByText(/Puede que la primera parte ya se haya enviado/)).toBeVisible();
    await type("dos");
    fireEvent.click(sendButton());
    await waitFor(() => expect(s.posts()).toHaveLength(3));
    const [one, two, three] = s.ids();
    expect(two).toBe(one);
    expect(three).not.toBe(one);
  });

  it("explica 409 y 404 sin perder el texto; un id ocupado se renueva", async () => {
    let reply: Reply = { status: 409, body: { code: "CHANNEL_UNAVAILABLE" } };
    const s = server({ send: () => reply });
    show();
    await openChat();
    await type("Hola");
    fireEvent.click(sendButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El canal de esta conversación no está conectado",
    );
    expect(await box()).toHaveValue("Hola");
    reply = { status: 409, body: { code: "CLIENT_MESSAGE_ID_TAKEN" } };
    fireEvent.click(sendButton("Reintentar"));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Ese id de mensaje ya se usó"),
    );
    reply = { status: 404, body: { code: "NOT_FOUND" } };
    fireEvent.click(sendButton("Reintentar"));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "No puedes responder en esta conversación",
      ),
    );
    expect(await box()).toHaveValue("Hola");
    const [first, again, renewed] = s.ids();
    expect(again).toBe(first);
    expect(renewed).not.toBe(again); // tras CLIENT_MESSAGE_ID_TAKEN
  });

  it("un 403 se explica como el 404 y un segundo envío mientras el primero espera no sale", async () => {
    const s = server({ send: () => ({ status: 403, body: { code: "PERMISSION_DENIED" } }) });
    show();
    await openChat();
    s.wait();
    await type("Hola");
    fireEvent.click(sendButton());
    fireEvent.click(await screen.findByRole("button", { name: "Enviando…" })); // doble clic
    const said = screen.getAllByRole("status").map((one) => one.textContent);
    expect(said).toContain("Enviando…"); // se anuncia, no solo cambia el botón
    fireEvent.keyDown(await box(), { key: "Enter" });
    s.go();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No puedes responder en esta conversación",
    );
    expect(await box()).toHaveValue("Hola");
    expect(s.posts()).toHaveLength(1);
  });

  it.each([
    "CONVERSATION_CLOSED",
    "CHANNEL_UNAVAILABLE",
    "CONTACT_BLOCKED",
    "WINDOW_CLOSED",
    "CONSENT_REVOKED",
  ])("un 409 %s vuelve a pedir la conversación", async (code) => {
    const s = server({ send: () => ({ status: 409, body: { code } }) });
    show();
    await openChat();
    const reads = () =>
      s.api.mock.calls.filter(
        ([url, init]) => String(url).endsWith("/conversations/1/") && init?.method !== "POST",
      ).length;
    await type("Hola");
    const before = reads();
    fireEvent.click(sendButton());
    await screen.findByRole("alert");
    await waitFor(() => expect(reads()).toBeGreaterThan(before));
  });

  it("tras enviar se vuelve a pedir la lista de mensajes (el estado de entrega avanza)", async () => {
    const s = server();
    show();
    await openChat();
    const reads = () =>
      s.api.mock.calls.filter(
        ([url, init]) => String(url).endsWith("/1/messages/") && init?.method !== "POST",
      ).length;
    const before = reads();
    await type("Hola");
    fireEvent.click(sendButton());
    await waitFor(() => expect(reads()).toBeGreaterThan(before));
  });

  it("Enter durante una composición (IME, también keyCode 229 de Safari) no envía", async () => {
    const s = server();
    show();
    await openChat();
    await type("你好");
    fireEvent.keyDown(await box(), { key: "Enter", isComposing: true });
    fireEvent.keyDown(await box(), { key: "Enter", keyCode: 229 });
    await settle();
    expect(s.posts()).toEqual([]);
    fireEvent.keyDown(await box(), { key: "Enter" });
    await waitFor(() => expect(s.posts()).toHaveLength(1));
  });

  it("una conversación cerrada no admite respuestas y lo dice con el texto del servidor", async () => {
    const s = server({ conversation: { status: "CLOSED" } });
    show();
    await openChat();
    expect(await box()).toBeDisabled();
    expect(await screen.findByText(/La conversación está cerrada. Podrás responder/)).toBeVisible();
    const closed = screen.getByText(/La conversación está cerrada. Podrás responder/);
    expect(await box()).toHaveAttribute("aria-describedby", expect.stringContaining(closed.id));
    expect(sendButton()).toBeDisabled();
    await type("Hola");
    fireEvent.keyDown(await box(), { key: "Enter" });
    await settle();
    expect(s.posts()).toEqual([]);
  });

  it("Enter envía, Mayús+Enter no; vacío o solo espacios no se envía", async () => {
    const s = server();
    show();
    await openChat();
    expect(sendButton()).toBeDisabled();
    await type("   \n ");
    expect(sendButton()).toBeDisabled();
    fireEvent.keyDown(await box(), { key: "Enter" });
    await settle();
    expect(s.posts()).toEqual([]);
    await type("Hola");
    fireEvent.keyDown(await box(), { key: "Enter", shiftKey: true });
    await settle();
    expect(s.posts()).toEqual([]);
    fireEvent.keyDown(await box(), { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(s.posts()).toHaveLength(1));
  });

  it("el contador avisa y bloquea al pasar de 4096 caracteres (puntos de código)", async () => {
    const s = server();
    show();
    await openChat();
    await type("😀".repeat(4096));
    expect(screen.getByText("4096 de 4096 caracteres")).toBeVisible();
    expect(sendButton()).toBeEnabled();
    await type("😀".repeat(4097));
    expect(screen.getByText("Te pasaste por 1 carácter.")).toBeVisible();
    expect(sendButton()).toBeDisabled();
    fireEvent.keyDown(await box(), { key: "Enter" });
    await settle();
    expect(s.posts()).toEqual([]);
    expect(await box()).toHaveAttribute("aria-describedby");
  });
});

// F7b-16: el tope y los adjuntos del canal de la conversación (`channel`), antes de enviar.
describe("cuadro de redacción: Instagram y Messenger", () => {
  const channel = (platform: string, text_max: number, text_unit: string) => ({
    channel: { ...rosa.channel, platform, text_max, text_unit, accepts_media: false },
  });
  const instagram = channel("INSTAGRAM", 1000, "BYTES") as Partial<Conversation>;
  const messenger = channel("MESSENGER", 2000, "CHARACTERS") as Partial<Conversation>;
  const live = () =>
    Array.from(document.querySelectorAll("[aria-live=polite]")).map((one) => one.textContent);

  it("Instagram cuenta bytes UTF-8: tildes, emojis y acentos combinados ocupan más", async () => {
    const s = server({ conversation: instagram });
    show();
    await openChat();
    for (const [text, bytes] of [
      ["a".repeat(1000), 1000],
      ["ñ".repeat(500), 1000],
      ["👍🏽".repeat(125), 1000],
      ["e\u0301".repeat(333) + "a", 1000],
    ] as const) {
      await type(text);
      expect(screen.getByText(new RegExp(`^${bytes} de 1000 bytes`))).toBeInTheDocument();
      expect(live()).toEqual(["Te acercas al tope de bytes del canal."]);
      expect(sendButton()).toBeEnabled();
    }
    await type("ñ".repeat(500) + "a");
    expect(screen.getByText(/^Te pasaste por 1 byte: este canal acepta hasta 1000/)).toBeVisible();
    expect(live()).toEqual(["Pasaste el tope de bytes del canal: no se puede enviar."]);
    expect(sendButton()).toBeDisabled();
    fireEvent.keyDown(await box(), { key: "Enter" });
    await settle();
    expect(s.posts()).toEqual([]);
    expect(await box()).toHaveValue("ñ".repeat(500) + "a"); // el texto no se pierde
  });

  it("Messenger cuenta caracteres y ninguno de los dos ofrece adjuntar", async () => {
    server({ conversation: messenger });
    show();
    await openChat();
    await type("ñ".repeat(2000));
    expect(screen.getByText("2000 de 2000 caracteres")).toBeInTheDocument();
    expect(sendButton()).toBeEnabled();
    await type("ñ".repeat(2001));
    expect(screen.getByText("Te pasaste por 1 carácter.")).toBeVisible();
    expect(sendButton()).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Adjuntar imagen o PDF" })).toBeNull();
  });

  it("una nota interna no sale por el canal: sigue el tope de la API", async () => {
    server({ conversation: instagram });
    show(grants("conversations.view", "messages.note"));
    await openChat();
    const note = await screen.findByRole("textbox", { name: "Nota interna" });
    fireEvent.change(note, { target: { value: "ñ".repeat(1500) } });
    expect(screen.getByText("1500 de 4096 caracteres")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Guardar nota" })).toBeEnabled();
  });

  it("con una API anterior, sin los campos del canal, todo sigue como antes", async () => {
    const { id, platform, name } = rosa.channel; // durante un despliegue o un rollback
    server({ conversation: { channel: { id, platform, name } as Conversation["channel"] } });
    show();
    await openChat();
    await type("ñ".repeat(4096));
    expect(screen.getByText("4096 de 4096 caracteres")).toBeInTheDocument();
    expect(sendButton()).toBeEnabled();
    expect(screen.getByRole("button", { name: "Adjuntar imagen o PDF" })).toBeVisible();
  });

  it("si la API lo rechaza igual, lo explica y conserva el texto", async () => {
    server({
      conversation: instagram,
      send: () => ({ status: 400, body: { code: "TEXT_TOO_LONG_FOR_CHANNEL" } }),
    });
    show();
    await openChat();
    expect(screen.queryByRole("button", { name: "Adjuntar imagen o PDF" })).toBeNull();
    await type("Hola");
    fireEvent.click(sendButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El texto es demasiado largo para este canal",
    );
    expect(await box()).toHaveValue("Hola");
  });
});

describe("teclado del teléfono (UX-INBOX-03)", () => {
  let touch = false;
  const start = async (coarse: boolean) => {
    touch = coarse; // se pregunta en cada tecla: cambiarlo luego cambia lo que hace Enter
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("coarse") && touch,
    }));
    const s = server();
    show();
    await openChat();
    return s;
  };
  const enter = async () => fireEvent.keyDown(await box(), { key: "Enter" }); // `false`: impedido
  const keys = async () => document.getElementById(`${(await box()).id}-keys`);

  it("se mira al pulsar: pasar a táctil sin montar de nuevo hace que Enter agregue la línea", async () => {
    const s = await start(false);
    expect(await box()).toHaveAttribute("enterkeyhint", "send");
    expect(await keys()).toHaveTextContent("Enter envía; Mayús+Enter agrega una línea.");
    await type("Hola");
    touch = true; // sin montar de nuevo (p. ej. se soltó el teclado del iPad)
    expect(await enter()).toBe(true); // el navegador pone la línea
    await settle();
    expect(s.posts()).toEqual([]);
  });

  it("en táctil lo dicen la ayuda y la tecla; dos toques en «Enviar» mandan una sola petición", async () => {
    const s = await start(true);
    expect(await box()).toHaveAttribute("enterkeyhint", "enter");
    expect(await keys()).toHaveTextContent("Enter agrega una línea; envía con el botón.");
    s.wait();
    await type("Hola");
    const button = sendButton();
    fireEvent.click(button);
    fireEvent.click(button); // en el mismo instante, el mismo botón
    await settle();
    expect(s.posts()).toHaveLength(1);
    // «Volver» y reabrir en curso: no sale otro; si termina con el chat cerrado, se vacía (UX-INBOX-04).
    const back = async () => {
      fireEvent.click(screen.getByRole("button", { name: "Volver a las conversaciones" }));
      await waitFor(() => expect(screen.queryByRole("heading", { level: 2 })).toBeNull());
    };
    await back();
    await openChat();
    expect(await box()).toHaveValue("Hola");
    fireEvent.click(sendButton("Enviando…"));
    await back();
    s.go();
    await waitFor(() => expect(s.stored).toHaveLength(1));
    await settle(); // sus callbacks corren con el cuadro desmontado
    await openChat();
    expect(await box()).toHaveValue("");
    expect(s.posts()).toHaveLength(1);
    await type("Otra vez");
    fireEvent.click(sendButton());
    await waitFor(() => expect(s.posts()).toHaveLength(2));
  });
});

describe("borradores por organización, conversación y modo (UX-INBOX-04)", () => {
  const toNote = () => fireEvent.click(screen.getByRole("button", { name: "Nota interna" }));
  const away = async () => (await openChat(/Juan Pérez/), await openChat()); // ir a Juan y volver

  it("cambiar de conversación y volver conserva respuesta y nota, aparte y solo en memoria", async () => {
    const storage = vi.spyOn(Storage.prototype, "setItem");
    vi.stubGlobal("indexedDB", { open: vi.fn() });
    server();
    show(grants("conversations.view", "messages.send", "messages.note"));
    await openChat();
    await type("Hola Rosa");
    toNote();
    await type("Pidió delivery", "Nota interna");
    await openChat(/Juan Pérez/);
    expect(await box()).toHaveValue(""); // nada de Rosa en el cuadro de Juan
    toNote();
    expect(await box("Nota interna")).toHaveValue("");
    await openChat();
    expect(await box()).toHaveValue("Hola Rosa"); // abre en «Responder», como siempre
    toNote();
    expect(await box("Nota interna")).toHaveValue("Pidió delivery");
    expect([storage.mock.calls, localStorage.length, sessionStorage.length]).toEqual([[], 0, 0]);
    expect(indexedDB.open).not.toHaveBeenCalled();
  });

  it("sin respuesta, a los 30 s se libera; el texto se queda y, al volver, lleva su id", async () => {
    const s = server();
    const net = hangPosts(s.api);
    show();
    await openChat();
    await type("Hola Rosa");
    fireEvent.click(sendButton());
    await waitFor(() => expect(net.waits).toEqual([30_000]));
    net.expire();
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay conexión con el servidor");
    expect(await box()).not.toHaveAttribute("readonly");
    expect(screen.queryByText(/Puede que el archivo/)).toBeNull(); // sin archivo, nada que dudar
    await away();
    expect(await box()).toHaveValue("Hola Rosa");
    net.answer();
    fireEvent.click(sendButton());
    await waitFor(() => expect(box()).resolves.toHaveValue(""));
    const [first, retry] = s.ids();
    expect(retry).toBe(first);
  });

  it("una nota sin respuesta también se libera a los 30 s (UX-INBOX-04c)", async () => {
    const net = hangPosts(server().api);
    show(grants("conversations.view", "messages.note"));
    await openChat();
    await type("Llamar mañana", "Nota interna");
    fireEvent.click(sendButton("Guardar nota"));
    await waitFor(() => expect(net.waits).toEqual([30_000]));
    net.expire();
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay conexión con el servidor");
    expect(await box("Nota interna")).not.toHaveAttribute("readonly");
  });

  // La marca de la fila (UX-INBOX-04b): texto e icono dentro del botón, por respuesta o por nota.
  const row = (name: RegExp) => screen.getByRole("button", { name });
  const marked = (name = /Rosa Díaz/) => within(row(name)).queryByText("Borrador") !== null;

  it("la fila dice «Borrador» con icono por una respuesta o por una nota; espacios solos no", async () => {
    server();
    show(grants("conversations.view", "messages.send", "messages.note"));
    await openChat();
    expect(marked()).toBe(false);
    await type("Hola Rosa");
    expect(row(/Rosa Díaz/)).toHaveAccessibleName(/Rosa Díaz.*Borrador/);
    const mark = within(row(/Rosa Díaz/)).getByText("Borrador");
    expect(mark.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(marked(/Juan Pérez/)).toBe(false);
    toNote();
    await type("Pidió delivery", "Nota interna");
    await type("", "Nota interna");
    expect(marked()).toBe(true); // vaciar la nota no borra la marca de la respuesta
    fireEvent.click(screen.getByRole("button", { name: "Responder al cliente" }));
    await type("   \n ");
    expect(marked()).toBe(false);
    toNote(); // M10 de #1123: solo la nota
    await type("Pidió delivery", "Nota interna");
    expect(marked()).toBe(true);
  });

  it("durante el envío lo que sale no marca; la nota sí; tras un fallo vuelve y tras enviar se va", async () => {
    let fail = true;
    const s = server({
      send: () =>
        fail ? { status: 500, body: { code: "X" } } : { status: 202, body: saved("s1", "ok") },
    });
    show(grants("conversations.view", "messages.send", "messages.note"));
    await openChat();
    await type("Hola Rosa");
    s.wait();
    fireEvent.click(sendButton());
    await waitFor(() => expect(box()).resolves.toHaveAttribute("readonly"));
    expect(marked()).toBe(false); // su único texto es el que está saliendo
    toNote();
    await type("Pidió delivery", "Nota interna");
    expect(marked()).toBe(true); // la nota no está saliendo
    await type("", "Nota interna");
    expect(marked()).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Responder al cliente" }));
    s.go();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(marked()).toBe(true); // falló: el texto sigue sin enviar
    await type(""); // queda el intento (su id), pero sin texto no hay borrador
    expect(marked()).toBe(false);
    await type("Hola Rosa");
    fail = false;
    fireEvent.click(await screen.findByRole("button", { name: /^(Reintentar|Enviar)$/ }));
    await waitFor(() => expect(box()).resolves.toHaveValue(""));
    expect(marked()).toBe(false);
  });

  it("una nota que se está guardando no marca; si falla, vuelve a marcar", async () => {
    const s = server(); // este doble no tiene la ruta de notas: responde 404
    show(grants("conversations.view", "messages.note"));
    await openChat();
    await type("Pidió delivery", "Nota interna");
    s.wait();
    fireEvent.click(screen.getByRole("button", { name: "Guardar nota" }));
    await waitFor(() => expect(box("Nota interna")).resolves.toHaveAttribute("readonly"));
    expect(marked()).toBe(false);
    s.go();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(marked()).toBe(true);
  });

  it("convive con «La respuesta no llegó» y «Pide atención humana»", async () => {
    const asked = {
      reason: "UPSET",
      requested_at: "2026-11-04T15:31:00Z",
      requested_by_user_id: "u1",
    };
    server({ conversation: { last_reply_failed: true, human_request: asked } as never });
    show();
    await openChat();
    await type("Hola Rosa");
    for (const text of ["La respuesta no llegó", "Pide atención humana", "Borrador"]) {
      expect(within(row(/Rosa Díaz/)).getByText(text)).toBeInTheDocument();
    }
  });

  it("escribir solo vuelve a dibujar la fila cuya marca cambia, y una sola vez", async () => {
    server();
    show();
    await openChat();
    await settle();
    rowRenders.count = 0;
    await type("H");
    expect(rowRenders.count).toBe(1); // la de Rosa, que pasa a «Borrador»; no la de Juan
    await type("Hola Rosa");
    await settle();
    expect(rowRenders.count).toBe(1); // sigue con borrador: nada cambia en la lista
  });

  // Mismos ids y dirección; otra persona de la misma organización entra tras cerrar sesión.
  it.each([
    ["otra organización", { organization: { ...tenant.organization, id: "o2" } }],
    ["otra persona de la misma organización", { user: { ...tenant.user, id: "u2" } }],
  ])("con %s los borradores se vacían y ninguno vuelve", async (_name, change) => {
    server();
    const permissions = grants("conversations.view", "messages.send");
    function Switch() {
      const [other, setOther] = useState(false);
      return (
        <TenantProvider value={{ ...tenant, permissions, ...(other && change) }}>
          <button onClick={() => setOther(!other)}>Cambiar</button>
          <Inbox />
        </TenantProvider>
      );
    }
    renderApp(<Switch />);
    await openChat();
    await type("Hola Rosa");
    for (const round of [1, 2]) {
      fireEvent.click(screen.getByRole("button", { name: "Cambiar" }));
      await openChat();
      expect(await box(), `vuelta ${round}`).toHaveValue("");
      expect(marked(), `vuelta ${round}`).toBe(false); // tampoco su marca en la lista
    }
  });
});

describe("sin conexión y reintentos (UX-INBOX-06c)", () => {
  afterEach(() => onlineManager.setOnline(true));
  const log = () => screen.findByRole("list", { name: "Mensajes" });

  it("sin conexión la burbuja lo dice y nada sale; el plazo empieza al salir, una sola vez", async () => {
    const s = server({ send: () => ({ status: 202, body: saved("s1", "Hola Rosa") }) });
    const timeout = vi.spyOn(AbortSignal, "timeout");
    show();
    await openChat();
    act(() => onlineManager.setOnline(false));
    await type("Hola Rosa");
    fireEvent.click(sendButton());
    const list = await log();
    expect(await within(list).findByText("Sin conexión: aún no se envió")).toBeVisible();
    expect(within(list).queryByText("En cola")).toBeNull(); // no se afirma lo que no pasó
    fireEvent.click(sendButton("En espera")); // el candado sigue: ni un segundo envío
    await settle();
    expect(s.posts()).toEqual([]);
    expect(timeout).not.toHaveBeenCalled(); // el plazo de 30 s no corre mientras espera
    act(() => onlineManager.setOnline(true));
    await waitFor(() => expect(s.posts()).toHaveLength(1));
    expect(timeout.mock.calls).toEqual([[30_000]]);
    const [, init] = s.api.mock.calls.find(([, init]) => init?.method === "POST")!;
    expect(init!.signal!.aborted).toBe(false); // recién creado: no sale ya vencido
    await waitFor(() => expect(box()).resolves.toHaveValue(""));
    expect(screen.queryByText(/Sin conexión/)).toBeNull();
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
  });

  it("una nota en pausa dice que se guardará, no que se enviará", async () => {
    server();
    show(grants("conversations.view", "messages.note"));
    await openChat();
    act(() => onlineManager.setOnline(false));
    await type("Llamar mañana", "Nota interna");
    fireEvent.click(sendButton("Guardar nota"));
    expect(await screen.findByText("Sin conexión: aún no se guardó")).toBeVisible();
    expect(screen.queryByText(/aún no se envió/)).toBeNull();
  });

  it("la respuesta se perdió tras guardarse: el reintento lleva su id y se ve una sola burbuja", async () => {
    const s = server();
    const inner = s.api.getMockImplementation()!;
    let lost = true;
    let [gate, open] = [Promise.resolve(), () => {}];
    s.api.mockImplementation(async (input, init) => {
      if (init?.method !== "POST") return inner(input, init);
      const { client_message_id } = JSON.parse(String(init.body));
      // Como `messages_client_uq`: el mismo id devuelve lo que ya estaba guardado.
      s.stored[0] ??= saved("s1", "Hola Rosa", { client_message_id });
      if (lost) {
        lost = false;
        throw new TypeError("Failed to fetch");
      }
      await gate; // el reintento tarda: se mira la pantalla mientras sale
      return new Response(JSON.stringify(s.stored[0]), { status: 202 });
    });
    show();
    await openChat();
    await type("Hola Rosa");
    fireEvent.click(sendButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay conexión con el servidor");
    const list = await log();
    await within(list).findByText("Hola Rosa"); // la relectura ya trae lo guardado
    const own = () => within(list).queryAllByText("Hola Rosa").length;
    const seen: number[] = [];
    const watcher = new MutationObserver(() => seen.push(own()));
    watcher.observe(list, { subtree: true, childList: true, characterData: true });
    gate = new Promise<void>((resolve) => (open = resolve));
    fireEvent.click(sendButton("Reintentar"));
    await waitFor(() => expect(sendButton("Enviando…")).toBeInTheDocument());
    await settle();
    expect(own()).toBe(1); // mientras sale el reintento, sin la burbuja duplicada
    open();
    await waitFor(() => expect(box()).resolves.toHaveValue(""));
    await settle();
    watcher.disconnect();
    const [first, retry] = s.ids();
    expect(retry).toBe(first);
    expect(own()).toBe(1);
    expect(seen.filter((n) => n !== 1)).toEqual([]); // nunca dos (ni ninguna)
    expect(s.stored).toHaveLength(1);
  });

  it("una nota ya guardada que llega en una relectura antes de su respuesta se ve una sola vez", async () => {
    const s = server();
    const inner = s.api.getMockImplementation()!;
    let open = () => {};
    const gate = new Promise<void>((resolve) => (open = resolve));
    s.api.mockImplementation(async (input, init) => {
      if (init?.method !== "POST") return inner(input, init);
      // Como la API: una nota vuelve sin `client_message_id` (solo lo lleva lo saliente propio).
      const changes = {
        direction: "INTERNAL",
        message_type: "NOTE",
        client_message_id: null,
      } as const;
      s.stored.push(saved("n1", "Llamar mañana", changes)); // guardada; la respuesta tarda
      await gate;
      return new Response(JSON.stringify(s.stored[0]), { status: 201 });
    });
    const { client } = show(grants("conversations.view", "messages.note"));
    await openChat();
    await type("Llamar mañana", "Nota interna");
    fireEvent.click(sendButton("Guardar nota"));
    await waitFor(() => expect(s.stored).toHaveLength(1));
    await act(() => client.invalidateQueries({ queryKey: messagesKey("acme", "1") })); // un evento
    const notes = async () => (await settle(), within(await log()).getAllByText("Llamar mañana"));
    expect(await notes()).toHaveLength(1);
    open();
    expect(await notes()).toHaveLength(1);
  });

  it("de un envío en pausa la pantalla solo conoce su id: ni el texto ni el archivo", () => {
    const file = new File(["x"], "privado.pdf", { type: "application/pdf" });
    const variables = { client_message_id: "cm-1", body: "secreto", file, draft: "1:reply" };
    const state = (isPaused: boolean, status = "pending") => ({
      state: { isPaused, status, variables },
    });
    expect(pausedId(state(true))).toBe("cm-1");
    expect(pausedId(state(false))).toBeNull(); // en vuelo, no en pausa
    expect(pausedId(state(true, "error"))).toBeNull();
  });

  it("solo conserva las burbujas en curso de su conversación, y no la que el servidor ya trae", () => {
    const client = new QueryClient();
    const pending = (id: string) =>
      saved(`pending-${id}`, "…", { client_message_id: id }) as Message;
    const pages = (...rows: Message[]) => ({
      pages: [{ results: rows, next: null }],
      pageParams: [""],
    });
    const old = saved("s0", "ya no está en la primera página", { client_message_id: "c" });
    client.setQueryData(messagesKey("acme", "1"), pages(pending("a"), pending("b"), old));
    client.setQueryData(messagesKey("acme", "2"), pages(pending("z"))); // otra conversación
    const fetched = { results: [saved("s1", "ok", { client_message_id: "b" })], next: null };
    const kept = keepPending(client, messagesKey("acme", "1"), fetched).results;
    expect(kept.map(({ id }) => id)).toEqual(["pending-a", "s1"]);
    const empty = { results: [], next: null };
    expect(keepPending(client, messagesKey("acme", "3"), empty)).toBe(empty);
  });
});

describe("envío retenido sin conexión: 5 minutos o «Cancelar» (UX-INBOX-06f)", () => {
  let clock = 0; // el reloj de pared (`Date.now`); los temporizadores siguen siendo los reales
  beforeEach(() => {
    clock = Date.parse("2026-11-26T15:00:00Z");
    vi.spyOn(Date, "now").mockImplementation(() => clock);
  });
  afterEach(() => onlineManager.setOnline(true));
  const online = (on: boolean) => act(() => onlineManager.setOnline(on));
  const cancel = () => screen.queryByRole("button", { name: "Cancelar" });
  const hold = async (text = "Hola Rosa") => {
    online(false);
    await type(text);
    fireEvent.click(sendButton());
    await waitFor(() => expect(cancel()).toBeVisible());
  };
  const pressedIds = (client: QueryClient) =>
    client
      .getMutationCache()
      .getAll()
      .map(({ state }) => (state.variables as { client_message_id: string }).client_message_id);
  const back = (text = "Hola Rosa") =>
    waitFor(async () => {
      expect(await box()).toHaveValue(text);
      expect(await box()).not.toHaveAttribute("readonly");
    });

  it("dentro de los 5 min sale una sola vez al reconectar, con su id; dice cuánto falta", async () => {
    const s = server();
    const { client } = show();
    await openChat();
    await hold();
    expect(
      screen.getByText(/aún no se envió\. Se enviará solo si vuelve la conexión en 5:00/),
    ).toBeVisible();
    fireEvent.click(sendButton("En espera")); // doble toque: el candado sigue
    fireEvent.click(sendButton("En espera"));
    clock += HOLD - 1;
    expect(await screen.findByText(/conexión en 0:01;/)).toBeVisible();
    expect(screen.getByText(/en menos de 1 minuto\./)).toHaveClass("sr-only");
    online(true);
    await waitFor(() => expect(box()).resolves.toHaveValue(""));
    await settle();
    expect(s.ids()).toEqual(pressedIds(client));
    expect(s.posts()).toHaveLength(1);
  });

  it("a los 5 min justos no sale: vuelve al cuadro, sin burbuja ni alerta; un tab dormido igual", async () => {
    const s = server();
    show();
    await openChat();
    await hold();
    clock += HOLD; // el equipo durmió: ningún temporizador corrió
    online(true);
    expect(
      await screen.findByText(
        "No se envió: pasaron 5 minutos sin conexión. El texto sigue en el cuadro.",
      ),
    ).toBeVisible();
    await back();
    await settle();
    expect(s.posts()).toEqual([]);
    expect(screen.queryByRole("alert")).toBeNull();
    const shown = screen
      .queryAllByText("Hola Rosa")
      .filter(({ tagName }) => tagName !== "TEXTAREA");
    expect(shown).toEqual([]); // sin la burbuja
    expect(sendButton()).toBeEnabled(); // «Enviar», no «Reintentar»
  });

  it("si el reloj retrocede no se fía: no sale", async () => {
    const s = server();
    show();
    await openChat();
    await hold();
    clock -= 1000;
    online(true);
    await back();
    await settle();
    expect(s.posts()).toEqual([]);
  });

  it("sin red, caduca sola a los 5 min; lo soltado nunca sale después, y el reintento lleva su id", async () => {
    const s = server();
    const { client } = show();
    await openChat();
    await hold();
    clock += HOLD;
    expect(await screen.findByText(/^No se envió: pasaron 5 minutos/)).toBeVisible();
    expect(cancel()).toBeNull();
    await back();
    for (const on of [true, false, true, false, true]) online(on);
    await settle();
    expect(s.posts()).toEqual([]); // la mutación soltada sigue en la caché y no pide nada
    fireEvent.click(sendButton());
    await waitFor(() => expect(s.posts()).toHaveLength(1));
    expect(new Set([...s.ids(), ...pressedIds(client)]).size).toBe(1);
  });

  it("«Cancelar» no envía nada; un toque nuevo sale una vez y el viejo no suelta su candado", async () => {
    const s = server();
    const { client } = show();
    await openChat();
    await hold();
    fireEvent.click(cancel()!);
    expect(
      await screen.findByText("Cancelado: no se envió nada. El texto sigue en el cuadro."),
    ).toBeVisible();
    await back();
    expect(await box()).toHaveFocus();
    fireEvent.click(sendButton()); // sigue sin red: otro toque, otra espera
    await waitFor(() => expect(cancel()).toBeVisible());
    s.wait();
    online(true);
    await waitFor(() => expect(s.posts()).toHaveLength(1));
    await settle();
    expect(await box()).toHaveAttribute("readonly"); // en vuelo: el toque cancelado no lo libera
    expect(cancel()).toBeNull(); // ya salió: no se promete cancelarlo
    s.go();
    await waitFor(() => expect(box()).resolves.toHaveValue(""));
    expect(s.posts()).toHaveLength(1);
    expect(new Set([...s.ids(), ...pressedIds(client)]).size).toBe(1); // el mismo id
  });

  it("en cuanto la petición sale, «Cancelar» se va y los 5 min no la tocan; en línea ni aparece", async () => {
    const s = server();
    const inner = s.api.getMockImplementation()!;
    const shownAtSend: boolean[] = []; // tras el render que dispara el envío, antes de su respuesta
    s.api.mockImplementation(async (input, init) => {
      if (init?.method === "POST") {
        await Promise.resolve();
        await Promise.resolve();
        shownAtSend.push(cancel() !== null);
      }
      return inner(input, init);
    });
    show();
    await openChat();
    await hold();
    s.wait();
    clock += HOLD - 1;
    online(true);
    await waitFor(() => expect(s.posts()).toHaveLength(1));
    expect(shownAtSend).toEqual([false]);
    clock += HOLD;
    await act(() => new Promise((resolve) => setTimeout(resolve, 1100)));
    expect(cancel()).toBeNull();
    expect(screen.queryByText(/^No se envió/)).toBeNull();
    expect(await box()).toHaveAttribute("readonly");
    s.go();
    await waitFor(() => expect(box()).resolves.toHaveValue(""));
    const flashes: boolean[] = [];
    const watcher = new MutationObserver(() => flashes.push(cancel() !== null));
    watcher.observe(document.body, { subtree: true, childList: true });
    await type("Otra");
    fireEvent.click(sendButton()); // con red: sale sin retención
    await waitFor(() => expect(box()).resolves.toHaveValue(""));
    watcher.disconnect();
    expect(flashes.filter(Boolean)).toEqual([]);
    expect(s.posts()).toHaveLength(2);
  });

  it("cambiar de conversación: solo vuelve el borrador de la suya, sin tocar los otros", async () => {
    const s = server();
    show();
    await openChat();
    await hold();
    await openChat(/Juan Pérez/);
    await type("Hola Juan");
    clock += HOLD;
    online(true); // caduca sin su cuadro a la vista
    await settle();
    expect(await box()).toHaveValue("Hola Juan");
    await openChat();
    await back();
    await openChat(/Juan Pérez/);
    expect(await box()).toHaveValue("Hola Juan");
    expect(s.posts()).toEqual([]);
  });

  it("con archivo: cancelar deja texto y archivo, sin «dudoso», y el siguiente toque usa su id", async () => {
    const s = server();
    const { client } = show();
    await openChat();
    online(false);
    await type("Mira");
    const file = new File(["x"], "foto.png", { type: "image/png" });
    fireEvent.change(document.querySelector<HTMLInputElement>('input[type="file"]')!, {
      target: { files: [file] },
    });
    fireEvent.click(sendButton());
    await waitFor(() => expect(cancel()).toBeVisible());
    fireEvent.click(cancel()!);
    expect(await screen.findByText(/El texto y el archivo siguen en el cuadro\.$/)).toBeVisible();
    expect(screen.getByText("foto.png")).toBeVisible();
    expect(screen.queryByText(/Puede que el archivo/)).toBeNull();
    online(true);
    fireEvent.click(sendButton()); // un solo toque: sin el aviso de dos toques
    const media = () => s.api.mock.calls.filter(([url]) => String(url).endsWith("/1/media/"));
    await waitFor(() => expect(media()).toHaveLength(1));
    const sent = (media()[0]![1]!.body as FormData).get("client_message_id");
    expect(new Set([sent, ...pressedIds(client)]).size).toBe(1);
  });
});
