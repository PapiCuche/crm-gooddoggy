import {
  act,
  cleanup,
  configure,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, Grant, QuickReplyUse, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";
import { useQuickReplies } from "./quick-replies";

const BASE = "/api/v1/o/acme/conversations";
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
  ...{ channel: { id: "ch1", platform: "SANDBOX", name: "Sandbox" } },
  ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: null, version: 1 },
  ...{ last_message_preview: null, last_message_direction: null },
} as unknown as Conversation;
const reply = (id: string, shortcut: string, title: string, category: [string, number]) =>
  ({
    ...{ id, shortcut, title, body: `Hola {{contact.first_name}}, ${title.toLowerCase()}.` },
    category: { id: category[0], name: category[0], sort_order: category[1] },
  }) as QuickReplyUse;
// La API las da por creación; el selector las agrupa por el orden de su categoría.
const REPLIES = [
  reply("r1", "delivery", "Envío a domicilio", ["Delivery", 3]),
  reply("r2", "precio", "Lista de precios", ["Precios", 1]),
  reply("r3", "delivery-lima", "Envío en Lima", ["Delivery", 3]),
];

const juan = { ...rosa, id: "2", contact: { id: "c2", display_name: "Juan Pérez" } };

type Reply = { status: number; body?: unknown };
// El servidor de dos conversaciones; con `hold`, cada `render` espera a `release()`.
function server(
  render: () => Reply = () => ({ status: 200, body: rendered }),
  { hold = false, replies = REPLIES } = {},
) {
  const routes: Record<string, Reply | (() => Reply)> = { [`GET ${BASE}/`]: page([rosa, juan]) };
  for (const one of [rosa, juan]) {
    routes[`GET ${BASE}/${one.id}/`] = { status: 200, body: one };
    routes[`GET ${BASE}/${one.id}/messages/`] = page([]);
    routes[`POST ${BASE}/${one.id}/messages/`] = { status: 202, body: { id: "s1", body: "ok" } };
    for (const id of ["r1", "r3"])
      routes[`POST ${BASE}/${one.id}/quick-replies/${id}/render/`] = render;
  }
  const api = mockApi(routes);
  const inner = api.getMockImplementation()!;
  const held: (() => void)[] = [];
  api.mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://x");
    if (hold && url.pathname.endsWith("/render/")) await new Promise<void>((go) => held.push(go));
    if (!/\/conversations\/\d\/quick-replies\/$/.test(url.pathname)) return inner(input, init);
    const q = url.searchParams.get("q") ?? "";
    const found = replies.filter((one) => `${one.shortcut} ${one.title}`.includes(q));
    return new Response(JSON.stringify({ results: found, next: null }), { status: 200 });
  });
  const calls = (match: (url: URL, method: string) => boolean) =>
    api.mock.calls.filter(([url, init]) =>
      match(new URL(String(url), "http://x"), init?.method ?? "GET"),
    );
  return {
    held,
    release: () => act(async () => held.shift()?.()),
    searches: () =>
      calls((url) => url.pathname.endsWith("/quick-replies/")).map(([url]) =>
        new URL(String(url), "http://x").searchParams.get("q"),
      ),
    renders: () => calls((url) => url.pathname.endsWith("/render/")).length,
    sends: () =>
      calls((url, method) => url.pathname.endsWith("/messages/") && method === "POST").map(
        ([, init]) => JSON.parse(String(init!.body)) as { body: string },
      ),
  };
}
const rendered = {
  ...{ id: "r3", shortcut: "delivery-lima", text: "Hola , el envío en Lima llega hoy." },
  missing: ["contact.first_name"],
};
const show = (permissions = grants("conversations.view", "messages.send", "messages.note")) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions }}>
      <Inbox />
    </TenantProvider>,
  );
const openChat = async (name = /Rosa Díaz/) => {
  fireEvent.click(await screen.findByRole("button", { name }));
  await waitFor(() => expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(name));
};
const box = () => screen.findByRole("textbox", { name: "Responder al cliente" });
const type = async (text: string) => fireEvent.change(await box(), { target: { value: text } });
const key = async (name: string, extra = {}) =>
  fireEvent.keyDown(await box(), { key: name, ...extra });
const settle = (ms = 30) => act(() => new Promise((resolve) => setTimeout(resolve, ms)));
const options = () =>
  within(screen.getByRole("listbox", { name: "Respuestas rápidas" })).getAllByRole("option");
const selected = () =>
  within(screen.getByRole("listbox", { name: "Respuestas rápidas" })).getByRole("option", {
    selected: true,
  });

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("respuestas rápidas en el cuadro de respuesta (F6-08b)", () => {
  it("`/` abre la lista agrupada por categoría y `/deli` busca una sola vez, al dejar de teclear", async () => {
    const s = server();
    show();
    await openChat();
    await type("/");
    const list = await screen.findByRole("listbox", { name: "Respuestas rápidas" });
    // Con la lista abierta, la ayuda de teclas dice lo que hace Enter ahora.
    expect(
      screen.getByText("Enter elige la respuesta rápida; Esc cierra la lista."),
    ).toBeInTheDocument();
    const groups = await within(list).findAllByRole("group");
    const names = groups.map((group) => {
      const label = document.getElementById(group.getAttribute("aria-labelledby")!);
      return [label?.textContent, within(group).getAllByRole("option").length];
    });
    expect(names).toEqual([
      ["Precios", 1],
      ["Delivery", 2],
    ]);
    // El cuadro sigue siendo el textbox y señala la lista y la opción activa.
    const field = await box();
    expect(field).toHaveAttribute("aria-autocomplete", "list");
    expect(field).toHaveAttribute("aria-controls", list.id);
    expect(field).toHaveAttribute("aria-activedescendant", selected().id);
    for (const text of ["/d", "/de", "/del", "/deli"]) await type(text);
    await waitFor(() => expect(within(list).getAllByRole("option")).toHaveLength(2));
    expect(within(list).queryByText("Lista de precios")).toBeNull();
    expect(s.searches()).not.toEqual(expect.arrayContaining(["d", "de", "del"]));
    expect(s.searches()).toContain("deli");
    // Con un espacio ya es un mensaje: se cierra.
    await type("/deli hoy");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("las flechas mueven la opción, Enter la inserta resuelta sin enviar y avisa lo que faltó", async () => {
    const s = server();
    show();
    await openChat();
    await type("/deli");
    await waitFor(() => expect(options()).toHaveLength(2));
    expect(selected()).toHaveTextContent("/delivery");
    await key("ArrowDown");
    expect(selected()).toHaveTextContent("/delivery-lima");
    await key("ArrowDown"); // vuelve al inicio
    expect(selected()).toHaveTextContent("/delivery");
    await key("ArrowUp");
    expect(selected()).toHaveTextContent("/delivery-lima");
    expect(await box()).toHaveAttribute("aria-activedescendant", selected().id);
    await key("Enter");
    await waitFor(() => expect(box()).resolves.toHaveValue(rendered.text));
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(await box()).toHaveFocus();
    expect(screen.getByText(/Quedó sin completar: el nombre del cliente/)).toBeVisible();
    await type(`${rendered.text} Saludos.`); // al editar, el aviso se va
    expect(screen.queryByText(/Quedó sin completar/)).toBeNull();
    await settle();
    expect(s.renders()).toBe(1);
    expect(s.sends()).toEqual([]); // elegir nunca envía
  });

  it("al hacer clic se inserta; se edita y se envía con el botón de siempre", async () => {
    const s = server();
    show();
    await openChat();
    await type("/delivery-l");
    fireEvent.click(await screen.findByRole("option", { name: /Envío en Lima/ }));
    await waitFor(() => expect(box()).resolves.toHaveValue(rendered.text));
    expect(s.sends()).toEqual([]);
    await type("Hola Rosa, el envío en Lima llega hoy.");
    fireEvent.click(screen.getByRole("button", { name: "Enviar" }));
    await waitFor(() =>
      expect(s.sends()).toEqual([
        expect.objectContaining({ body: "Hola Rosa, el envío en Lima llega hoy." }),
      ]),
    );
  });

  it("Escape cierra sin tocar el texto; Enter con la lista abierta nunca envía", async () => {
    const s = server(() => ({ status: 404, body: { code: "NOT_FOUND" } }));
    show();
    await openChat();
    await type("/delivery");
    await waitFor(() => expect(options()).toHaveLength(2));
    await key("Enter");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Esa respuesta ya no está disponible",
    );
    expect(await box()).toHaveValue("/delivery");
    await key("Escape");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(await box()).toHaveValue("/delivery");
    await settle();
    expect(s.sends()).toEqual([]);
  });

  it("Enter antes de que llegue la búsqueda espera y elige de lo escrito, no de la lista anterior", async () => {
    const s = server();
    show();
    await openChat();
    await type("/");
    await waitFor(() => expect(options()).toHaveLength(3));
    await type("/delivery-l");
    await key("Enter"); // aún se ve la lista de «/», con «/precio» primero
    await settle();
    expect(s.renders()).toBe(0);
    // Llega la búsqueda de «delivery-l»: se elige su única opción (si fuera «/precio», `render` daría 404).
    await waitFor(() => expect(box()).resolves.toHaveValue(rendered.text));
    expect(s.renders()).toBe(1);
    expect(s.sends()).toEqual([]);
    // Si se sigue escribiendo tras Enter, se olvida.
    await type("/");
    await waitFor(() => expect(options()).toHaveLength(3));
    await type("/pre");
    await key("Enter");
    await type("/prec");
    await waitFor(() => expect(options()).toHaveLength(1));
    await settle();
    expect(s.renders()).toBe(1);
  });

  it("un `render` que llega tarde no pisa lo que se escribió después", async () => {
    const s = server(undefined, { hold: true });
    show();
    await openChat();
    await type("/delivery-l");
    await waitFor(() => expect(options()).toHaveLength(1));
    await key("Enter");
    await waitFor(() => expect(s.held).toHaveLength(1));
    await type("hola, te escribo yo");
    await s.release();
    await settle();
    expect(await box()).toHaveValue("hola, te escribo yo");
    expect(screen.queryByText(/Quedó sin completar/)).toBeNull();
    await key("Enter"); // envía lo escrito, no el texto de la respuesta
    await waitFor(() =>
      expect(s.sends()).toEqual([expect.objectContaining({ body: "hola, te escribo yo" })]),
    );
  });

  it("cambiar de conversación con un `render` pendiente no lleva el texto a la otra", async () => {
    const s = server(undefined, { hold: true });
    show();
    await openChat();
    await type("/delivery-l");
    await waitFor(() => expect(options()).toHaveLength(1));
    await key("Enter");
    await waitFor(() => expect(s.held).toHaveLength(1));
    await openChat(/Juan Pérez/);
    await s.release();
    await settle(50);
    expect(await box()).toHaveValue(""); // el cuadro de Juan
    expect(screen.queryByDisplayValue(rendered.text)).toBeNull();
    expect(s.sends()).toEqual([]);
  });

  it.each([
    ["sostenido (repeat)", { repeat: true }],
    ["repetido al instante", {}],
  ])("el Enter que eligió, %s, no envía el texto insertado", async (_name, extra) => {
    const s = server();
    show();
    await openChat();
    await type("/delivery-l");
    await waitFor(() => expect(options()).toHaveLength(1));
    await key("Enter");
    await waitFor(() => expect(box()).resolves.toHaveValue(rendered.text));
    await key("Enter", extra);
    await settle(50);
    expect(s.sends()).toEqual([]);
    expect(await box()).toHaveValue(rendered.text);
    await settle(500); // pasado el margen, Enter vuelve a enviar
    await key("Enter");
    await waitFor(() => expect(s.sends()).toHaveLength(1));
  });

  it("el marcado se ve como texto; Tab y salir del cuadro cierran sin descartar; Escape descarta", async () => {
    const html = reply("r1", "delivery", "Envío", ["Delivery", 1]);
    server(undefined, { replies: [{ ...html, body: "<b id=boom>x</b> {{contact.name}}" }] });
    show();
    await openChat();
    await type("/deli");
    await waitFor(() => expect(options()).toHaveLength(1));
    expect(document.getElementById("boom")).toBeNull();
    expect(options()[0]).toHaveTextContent("<b id=boom>x</b>");
    // Tab no es del selector: deja mover el foco y el `blur` cierra; volver (Shift+Tab) la reabre.
    expect(fireEvent.keyDown(await box(), { key: "Tab" })).toBe(true);
    fireEvent.blur(await box());
    expect(screen.queryByRole("listbox")).toBeNull();
    fireEvent.focus(await box());
    await waitFor(() => expect(options()).toHaveLength(1));
    // Escape sí la descarta: cerrada hasta quitar la `/`.
    await key("Escape");
    expect(screen.queryByRole("listbox")).toBeNull();
    await type("/");
    await type("/d");
    expect(screen.queryByRole("listbox")).toBeNull();
    await type("");
    await type("/");
    await waitFor(() => expect(options()).toHaveLength(1));
  });

  it("una nota interna no abre el selector", async () => {
    const s = server();
    show(grants("conversations.view", "messages.note"));
    await openChat();
    fireEvent.change(await screen.findByRole("textbox", { name: "Nota interna" }), {
      target: { value: "/delivery" },
    });
    await settle();
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(s.searches()).toEqual([]);
  });

  it("el Enter que eligió, sostenido más allá del margen (repeat), no envía", async () => {
    const s = server();
    show();
    await openChat();
    await type("/delivery-l");
    await waitFor(() => expect(options()).toHaveLength(1));
    await key("Enter");
    await waitFor(() => expect(box()).resolves.toHaveValue(rendered.text));
    await settle(600); // pasado el margen de 500 ms, solo `repeat` dice que sigue sostenido
    await key("Enter", { repeat: true });
    await settle();
    expect(s.sends()).toEqual([]);
  });

  it("el clic en una opción no quita el foco al cuadro y la inserta", async () => {
    const s = server();
    show();
    await openChat();
    await type("/delivery-l");
    const option = await screen.findByRole("option", { name: /Envío en Lima/ });
    expect(fireEvent.mouseDown(option)).toBe(false); // `preventDefault`: el cuadro no pierde el foco
    fireEvent.click(option);
    await waitFor(() => expect(box()).resolves.toHaveValue(rendered.text));
    expect(s.sends()).toEqual([]);
  });

  it.each([
    ["isComposing", { isComposing: true }],
    ["keyCode 229 (Safari)", { keyCode: 229 }],
  ])(
    "un Enter del IME (%s) recién insertado es del IME: no se bloquea ni envía",
    async (_n, ime) => {
      const s = server();
      show();
      await openChat();
      await type("/delivery-l");
      await waitFor(() => expect(options()).toHaveLength(1));
      await key("Enter");
      await waitFor(() => expect(box()).resolves.toHaveValue(rendered.text));
      expect(fireEvent.keyDown(await box(), { key: "Enter", ...ime })).toBe(true);
      await settle();
      expect(s.sends()).toEqual([]);
    },
  );

  it("el hook descarta un `render` que vuelve con otra conversación, aunque el texto sea igual", async () => {
    const s = server(undefined, { hold: true });
    const insert = vi.fn();
    let hook!: ReturnType<typeof useQuickReplies>;
    let switchTo!: (id: string) => void;
    function Probe() {
      const [conversationId, setId] = useState("1");
      switchTo = setId;
      hook = useQuickReplies({ enabled: true, conversationId, text: "/delivery-l", insert });
      return hook.picker;
    }
    renderApp(
      <TenantProvider value={tenant}>
        <Probe />
      </TenantProvider>,
    );
    await waitFor(() => expect(options()).toHaveLength(1));
    const enter = { key: "Enter", nativeEvent: {}, preventDefault() {}, stopPropagation() {} };
    act(() => void hook.onKey(enter as Parameters<typeof hook.onKey>[0]));
    await waitFor(() => expect(s.held).toHaveLength(1));
    act(() => switchTo("2"));
    await s.release();
    await settle();
    expect(insert).not.toHaveBeenCalled();
  });
});
