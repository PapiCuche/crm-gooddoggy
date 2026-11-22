import { act, cleanup, configure, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, Grant, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { utf16Ranges } from "./copilot";
import { Inbox } from "./inbox";

const BASE = "/api/v1/o/acme/conversations";
const SUGGEST = `POST ${BASE}/1/copilot/suggest/`;
const INSERT = `POST ${BASE}/1/copilot/suggestions/g1/insert/`;
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
const AGENT = grants("conversations.view", "messages.send", "messages.note", "ai.use");
const rosa = {
  ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
  ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" } },
  ...{ channel: { id: "ch1", platform: "SANDBOX", name: "Sandbox" } },
  ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: null, version: 1 },
  ...{ last_message_preview: null, last_message_direction: null },
} as unknown as Conversation;
// Un emoji (dos unidades UTF-16, un punto de código) antes de la marca: la API cuenta 13, no 14.
const TEXT = "Hola 🐶 Rosa: [verificar] soles.";
const at = Array.from(TEXT).indexOf("[");
const suggestion = {
  ...{ id: "g1", text: TEXT, evidence_expires_at: null },
  ...{ findings: [{ type: "FIGURE", position: { start: at, end: at + 11 } }] },
  ...{ handoff: { requested: false, reason: null } },
};
// Lo que devuelve la ruta de insertar (el texto guardado y auditado), distinto del que se mostró:
// el cuadro recibe este.
const SAVED = "Texto guardado 🐶: [verificar] soles.";
const SETTLE = 500; // el de `composer.tsx`
const wait = (ms: number) => act(() => new Promise((resolve) => setTimeout(resolve, ms)));

type Reply = { status: number; body?: unknown; headers?: Record<string, string> };
function server(
  options: {
    status?: string;
    suggest?: () => Reply;
    insert?: () => Reply;
    send?: () => Reply;
    hold?: "suggest" | "messages";
  } = {},
) {
  const conversation = { ...rosa, status: options.status ?? "OPEN" };
  let release = () => {};
  const held = options.hold ? new Promise<void>((resolve) => (release = resolve)) : null;
  let holding = options.hold; // solo el primer POST a esa ruta espera
  const api = mockApi({
    [`GET ${BASE}/`]: { status: 200, body: { results: [conversation], next: null } },
    [`GET ${BASE}/1/`]: { status: 200, body: conversation },
    [`GET ${BASE}/1/messages/`]: { status: 200, body: { results: [], next: null } },
    [SUGGEST]: options.suggest ?? { status: 201, body: suggestion },
    [INSERT]: options.insert ?? { status: 200, body: { text: SAVED } },
    [`POST ${BASE}/1/messages/`]: options.send ?? { status: 202, body: {} },
  });
  const inner = api.getMockImplementation()!;
  api.mockImplementation(async (input, init) => {
    if (holding && init?.method === "POST" && String(input).endsWith(`/${holding}/`)) {
      holding = undefined;
      await held;
    }
    return inner(input, init);
  });
  const posts = (end: string) =>
    api.mock.calls.filter(([url, init]) => init?.method === "POST" && String(url).endsWith(end));
  // Lo que la conversación recibió: mensajes, medios o notas. Ningún camino del panel llega aquí.
  const sent = () =>
    api.mock.calls.filter(
      ([url, init]) => init?.method === "POST" && /\/1\/(messages|media|notes)\//.test(String(url)),
    );
  const bodies = () => sent().map(([, init]) => JSON.parse(String(init!.body)).body as string);
  return { api, go: () => release(), posts, sent, bodies };
}
const show = (permissions: Grant[] = AGENT) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions }}>
      <Inbox />
    </TenantProvider>,
  );
const openChat = async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz/ }));
  await screen.findByRole("heading", { level: 2 });
};
const box = () => screen.findByRole("textbox", { name: "Responder al cliente" });
const suggestButton = () => screen.findByRole("button", { name: "Sugerir respuesta" });
const draft = async () => {
  fireEvent.click(await suggestButton());
  return screen.findByText("Sugerencia del copiloto");
};
// Lo que el panel anuncia (su región `aria-live`), no los avisos del resto del Inbox.
const announced = () => document.querySelector("[role=status][aria-live=polite]");
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 30)));

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("utf16Ranges", () => {
  it("convierte puntos de código en índices UTF-16, también tras un carácter astral", () => {
    const [range] = utf16Ranges(TEXT, suggestion.findings);
    expect(TEXT.slice(...range!)).toBe("[verificar]");
    // Sin convertir, el tramo quedaría corrido un lugar.
    expect(TEXT.slice(at, at + 11)).not.toBe("[verificar]");
  });

  it("descarta lo que no cabe, lo vacío, lo negativo y lo que se solapa, y ordena", () => {
    const pos = (start: number, end: number) => ({ type: "LINK", position: { start, end } });
    expect(utf16Ranges("ab😀cd", [pos(3, 5), pos(0, 1), pos(4, 5), pos(2, 2), pos(-1, 1)])).toEqual(
      [
        [0, 1],
        [4, 6],
      ],
    );
    expect(utf16Ranges("abc", [pos(1, 9), pos(1.5, 2)])).toEqual([]);
  });
});

describe("panel del copiloto (F8-09a)", () => {
  it.each([
    ["sin `ai.use`", grants("conversations.view", "messages.send"), "OPEN"],
    ["sin poder responder", grants("conversations.view", "messages.note", "ai.use"), "OPEN"],
    ["en una conversación cerrada", AGENT, "CLOSED"],
  ])("no se ofrece %s", async (_name, permissions, status) => {
    const s = server({ status });
    show(permissions);
    await openChat();
    await settle();
    expect(screen.queryByRole("button", { name: "Sugerir respuesta" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Copiloto de IA" })).toBeNull();
    expect(s.posts("/suggest/")).toHaveLength(0);
  });

  it("muestra la sugerencia con la marca sin verificar en su sitio y sin enviar nada", async () => {
    const s = server();
    show();
    await openChat();
    await draft();
    const mark = document.querySelector("mark");
    expect(mark?.textContent).toBe("[verificar]");
    expect(mark).toHaveAttribute("title", "sin verificar");
    expect(screen.getByText(/no tiene respaldo en los datos/)).toBeVisible();
    expect(announced()).toHaveTextContent("Sugerencia lista");
    expect(await box()).toHaveValue(""); // nada llega al cuadro hasta insertar
    expect(s.sent()).toHaveLength(0);
    // Ningún botón del panel envía: solo pedir, insertar y descartar.
    const panel = screen.getByRole("region", { name: "Copiloto de IA" });
    const names = [...panel.querySelectorAll("button")].map((b) => b.textContent);
    expect(names).toEqual(["Insertar", "Descartar"]);
    expect(screen.getByRole("button", { name: "Sugerir otra" })).toBeVisible();
  });

  it("«Insertar» audita, pone el texto guardado en el cuadro, lo enfoca y no envía", async () => {
    const s = server();
    show();
    await openChat();
    await draft();
    fireEvent.click(screen.getByRole("button", { name: "Insertar" }));
    await waitFor(() => expect(box()).resolves.toHaveValue(SAVED)); // el del servidor, no el local
    expect(await box()).toHaveFocus();
    expect(s.posts("/insert/")).toHaveLength(1);
    expect(screen.queryByText("Sugerencia del copiloto")).toBeNull();
    expect(announced()).toHaveTextContent("Revísala y envíala tú");
    // Un Enter justo después (el de «Insertar», repetido) no envía lo que aún no se revisó.
    fireEvent.keyDown(await box(), { key: "Enter" });
    // Pasada la espera, nada salió solo: solo la acción del agente envía.
    await wait(SETTLE + 200);
    expect(s.sent()).toHaveLength(0);
    // Quien atiende lo edita y lo envía con Enter, que vuelve a enviar: ese es el único envío.
    fireEvent.change(await box(), { target: { value: "Hola 🐶 Rosa: 30 soles." } });
    fireEvent.keyDown(await box(), { key: "Enter" });
    await waitFor(() => expect(s.bodies()).toEqual(["Hola 🐶 Rosa: 30 soles."]));
  });

  it("dos clics seguidos en «Insertar» insertan y auditan una sola vez", async () => {
    const s = server();
    show();
    await openChat();
    await draft();
    const insert = screen.getByRole("button", { name: "Insertar" });
    fireEvent.click(insert);
    fireEvent.click(insert);
    await waitFor(() => expect(box()).resolves.toHaveValue(SAVED));
    await settle();
    expect(s.posts("/insert/")).toHaveLength(1);
    expect(await box()).toHaveValue(SAVED);
  });

  it("insertar mientras sale un envío no vuelve a enviar lo ya enviado", async () => {
    const s = server({ hold: "messages" });
    show();
    await openChat();
    fireEvent.change(await box(), { target: { value: "Hola" } });
    fireEvent.keyDown(await box(), { key: "Enter" }); // sale «Hola» y queda en espera
    await waitFor(() => expect(s.bodies()).toEqual(["Hola"]));
    await draft();
    fireEvent.click(screen.getByRole("button", { name: "Insertar" }));
    await waitFor(() => expect(box()).resolves.toHaveValue(`Hola\n\n${SAVED}`));
    s.go(); // el envío termina: se quita solo «Hola»
    await waitFor(() => expect(box()).resolves.toHaveValue(SAVED));
    await wait(SETTLE + 100);
    fireEvent.click(screen.getByRole("button", { name: "Enviar" }));
    await waitFor(() => expect(s.bodies()).toEqual(["Hola", SAVED]));
  });

  it("un envío con la marca [verificar] se explica con su propio texto", async () => {
    const s = server({ send: () => ({ status: 400, body: { code: "UNVERIFIED_CONTENT" } }) });
    show();
    await openChat();
    await draft();
    fireEvent.click(screen.getByRole("button", { name: "Insertar" }));
    await waitFor(() => expect(box()).resolves.toHaveValue(SAVED));
    await wait(SETTLE + 100);
    fireEvent.click(screen.getByRole("button", { name: "Enviar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El texto lleva la marca [verificar] del copiloto",
    );
    expect(await box()).toHaveValue(SAVED);
    expect(s.sent()).toHaveLength(1); // el intento del agente, rechazado
  });

  it("«Insertar» suma el texto a lo ya escrito y vuelve a «Responder» desde la nota", async () => {
    server();
    show();
    await openChat();
    fireEvent.change(await box(), { target: { value: "Buenas tardes." } });
    fireEvent.click(screen.getByRole("button", { name: "Nota interna" }));
    await draft();
    fireEvent.click(screen.getByRole("button", { name: "Insertar" }));
    await waitFor(() => expect(box()).resolves.toHaveValue(`Buenas tardes.\n\n${SAVED}`));
    expect(await box()).toBeVisible();
    expect(await box()).toHaveFocus();
  });

  it("«Descartar» quita la sugerencia sin tocar el cuadro ni llamar a nada", async () => {
    const s = server();
    show();
    await openChat();
    fireEvent.change(await box(), { target: { value: "Mi borrador" } });
    await draft();
    fireEvent.click(screen.getByRole("button", { name: "Descartar" }));
    expect(screen.queryByText("Sugerencia del copiloto")).toBeNull();
    expect(await box()).toHaveValue("Mi borrador");
    expect(await suggestButton()).toHaveFocus();
    await settle();
    expect(s.posts("/insert/")).toHaveLength(0);
    expect(s.sent()).toHaveLength(0);
    expect(announced()).toHaveTextContent("Sugerencia descartada");
  });

  it("mientras carga no pide dos veces y lo anuncia", async () => {
    const s = server({ hold: "suggest" });
    show();
    await openChat();
    const button = await suggestButton();
    // Dos clics antes de que React pinte el estado de carga (un doble clic real).
    act(() => {
      button.click();
      button.click();
    });
    const busy = await screen.findByRole("button", { name: "Preparando una sugerencia…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(busy);
    expect(announced()).toHaveTextContent("Preparando");
    s.go();
    await screen.findByText("Sugerencia del copiloto");
    expect(s.posts("/suggest/")).toHaveLength(1);
  });

  const error = (status: number, code: string, headers?: Record<string, string>) => () => ({
    status,
    headers,
    body: { code, detail: "texto-del-servidor-que-no-se-muestra" },
  });
  it.each([
    ["el tope por persona", error(429, "RATE_LIMITED", { "Retry-After": "120" }), "en 2 minutos"],
    ["el tope sin espera", error(429, "RATE_LIMITED"), "Pediste muchas sugerencias seguidas"],
    [
      "el tope de la empresa",
      error(429, "AI_ORG_RATE_LIMITED", { "Retry-After": "30" }),
      "Tu empresa pidió muchas sugerencias seguidas. Vuelve a probar en 1 minuto.",
    ],
    ["el presupuesto agotado", error(409, "AI_BUDGET_EXHAUSTED"), "Se agotó el presupuesto"],
    ["sin presupuesto", error(409, "AI_BUDGET_NOT_SET"), "aún no tiene presupuesto"],
    ["la IA apagada", error(409, "AI_DISABLED"), "La IA está apagada"],
    ["la IA sin configurar", error(409, "AI_NOT_CONFIGURED"), "aún no está configurada"],
    ["el plazo", error(504, "AI_DEADLINE"), "tardó demasiado"],
    ["el proveedor", error(502, "PROVIDER_ERROR"), "no pudo preparar una sugerencia"],
    ["sin permiso", error(403, "PERMISSION_DENIED"), "No puedes usar el copiloto"],
    ["fuera de alcance", error(404, "NOT_FOUND"), "Puede que ya no esté en tu alcance"],
  ])("explica %s con su propio texto", async (_name, suggest, message) => {
    const s = server({ suggest });
    show();
    await openChat();
    fireEvent.click(await suggestButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(document.body.textContent).not.toContain("texto-del-servidor");
    expect(screen.queryByText("Sugerencia del copiloto")).toBeNull();
    expect(await box()).toHaveValue("");
    expect(s.sent()).toHaveLength(0);
  });

  it.each([
    ["con la evidencia caducada", error(409, "AI_EVIDENCE_EXPIRED"), "caducaron"],
    ["ya purgada", error(404, "NOT_FOUND"), "ya no está disponible"],
  ])("al insertar %s lo explica, la quita y no toca el cuadro", async (_name, insert, message) => {
    const s = server({ insert });
    show();
    await openChat();
    await draft();
    fireEvent.click(screen.getByRole("button", { name: "Insertar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByText("Sugerencia del copiloto")).toBeNull();
    expect(await box()).toHaveValue("");
    expect(await suggestButton()).toHaveFocus(); // «Insertar» ya no está: el foco no se pierde
    expect(s.sent()).toHaveLength(0);
  });
});
