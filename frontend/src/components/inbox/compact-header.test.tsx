import { act, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Grant, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";

// UX-INBOX-03b. jsdom no aplica Tailwind: estado, foco y clases; lo visible se midió en un navegador.
const BASE = "/api/v1/o/acme/conversations";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  ...{ membership_id: "m1", mfa: { required: false, missing: false }, roles: [] },
  permissions: "conversations.view messages.send ai.use conversations.assign users.view"
    .split(" ")
    .map((code): Grant => ({ code, scopes: ["ORGANIZATION"] })),
};
const asked = { reason: "AGENT", requested_by_user_id: "u1", requested_at: "2026-11-22T15:30:00Z" };
const rosa = {
  ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
  ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" }, assignment_version: 1 },
  ...{ ai_excluded_at: null, human_request: asked, standby_since: null },
  ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: null, version: 1 },
  ...{ last_message_preview: null, last_message_direction: null },
  channel: {
    ...{ id: "ch1", platform: "WHATSAPP", name: "Tienda Lima" },
    ...{ text_max: 4096, text_unit: "CHARACTERS", accepts_media: true },
  },
};
const suggestion = {
  ...{ id: "g1", text: "Hola Rosa", evidence_expires_at: null, findings: [] },
  handoff: { requested: false, reason: null },
};
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const photo = {
  ...{ id: "m1", direction: "INBOUND", sender_type: "CONTACT", message_type: "IMAGE" },
  ...{ body: "Imagen recibida", delivery_status: null, created_at: "2026-11-04T15:30:00Z" },
  attachment: { id: "a1", kind: "IMAGE", status: "DONE", mime_type: "image/jpeg", available: true },
};

function serve() {
  return mockApi({
    [`GET ${BASE}/`]: page([rosa]),
    [`GET ${BASE}/1/`]: { status: 200, body: rosa },
    [`GET ${BASE}/1/messages/`]: page([photo]), // con «Guardar» (su archivo)
    [`POST ${BASE}/1/assign/`]: { status: 409, body: { code: "ASSIGNMENT_CONFLICT" } },
    [`GET /api/v1/o/acme/members/?limit=200`]: page([]),
    [`POST ${BASE}/1/copilot/suggest/`]: { status: 200, body: suggestion },
    [`DELETE ${BASE}/1/human-request/`]: { status: 200, body: { id: "1", human_request: null } },
  });
}
const show = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <Inbox />
    </TenantProvider>,
  );
const openChat = async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz/ }));
  return (await screen.findByRole("heading", { level: 2 })).closest("header")!.parentElement!;
};
const tick = () => act(() => new Promise((resolve) => setTimeout(resolve, 10)));
const box = () => screen.getByRole("textbox", { name: "Responder al cliente" });
const button = (name: string) => screen.getByRole("button", { name });
const COMPACT = "max-sm:group-data-composing:hidden";
const inCompact = (element: Element) => element.closest(`.${CSS.escape(COMPACT)}`);

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });
// Por defecto, un teléfono táctil de 390 px; `width` ≥ 640, una tableta (sin `max-sm`).
const pointer = (touch: boolean, width = 390) =>
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("coarse") ? touch : query.includes("max-width: 639px") && width < 640,
  }));
beforeEach(() => (pointer(true), void (document.cookie = "csrftoken=token-de-prueba")));
afterEach(() => vi.unstubAllGlobals()); // `cleanup`, en vitest.setup.ts

describe("cabecera compacta mientras se escribe (UX-INBOX-03b)", () => {
  it("empieza con el foco en el cuadro, sigue en sus botones y termina al salir de él", async () => {
    serve();
    show();
    const panel = await openChat();
    expect(panel).not.toHaveAttribute("data-composing");
    fireEvent.change(await screen.findByRole("textbox"), { target: { value: "Hola" } });
    const send = screen.getByRole("button", { name: "Enviar" });
    act(() => send.focus()); // un botón del cuadro no empieza a escribir
    await tick();
    expect(panel).not.toHaveAttribute("data-composing");
    act(() => box().focus());
    expect(panel).toHaveAttribute("data-composing");
    act(() => send.focus()); // un toque en «Enviar»: la cabecera no cambia en medio
    expect(panel).toHaveAttribute("data-composing");
    await tick();
    expect(panel).toHaveAttribute("data-composing");
    act(() => screen.getByRole("region", { name: "Mensajes de la conversación" }).focus());
    expect(panel).toHaveAttribute("data-composing"); // se espera un turno antes de crecer
    await tick();
    expect(panel).not.toHaveAttribute("data-composing");
    act(() => box().focus());
    act(() => box().blur()); // fuera del panel
    await tick();
    expect(panel).not.toHaveAttribute("data-composing");
  });

  it("con ratón no se compacta: pulsar «Enviar» o volver no mueve nada antes del clic", async () => {
    pointer(false);
    serve();
    show();
    const panel = await openChat();
    fireEvent.change(await screen.findByRole("textbox"), { target: { value: "Hola" } });
    act(() => box().focus());
    act(() => (fireEvent.mouseDown(button("Enviar")), button("Enviar").focus()));
    await tick();
    expect(panel).not.toHaveAttribute("data-composing");
    act(() => box().focus());
    fireEvent.mouseDown(button("Volver a las conversaciones"));
    await tick();
    expect(panel).not.toHaveAttribute("data-composing");
  });

  it("escribiendo se oculta la fila de asignación, no su aviso", async () => {
    serve();
    show();
    await openChat();
    fireEvent.click(await screen.findByRole("button", { name: "Tomar" }));
    const alert = await screen.findByText(/Alguien la tomó antes/);
    act(() => box().focus());
    expect(alert).toHaveAttribute("role", "alert");
    expect(inCompact(alert)).toBeNull();
    expect(inCompact(button("Acciones"))).not.toBeNull();
    expect(inCompact(button("Tomar"))).not.toBeNull();
  });

  it("una fila: volver, nombre y red y «Más»; lo demás se oculta y «Más» lo devuelve", async () => {
    serve();
    show();
    const panel = await openChat();
    const header = panel.querySelector("header")!;
    const more = button("Más acciones de la conversación");
    expect(more).toHaveClass("hidden", "size-11", "max-sm:group-data-composing:inline-flex");
    expect(more).not.toHaveAttribute("aria-expanded"); // no abre nada: suelta lo compacto (03c)
    const hides = "Actualizar mensajes|Excluir de la IA|Cliente y oportunidades|Acciones".split(
      "|",
    );
    for (const name of hides) expect(inCompact(button(name)), name).not.toBeNull();
    expect(within(header).getByText("CONV-00001").parentElement).toHaveClass(COMPACT);
    expect(inCompact(within(header).getByText("Sin asignar"))).not.toBeNull();
    expect(inCompact(button("Volver a las conversaciones"))).toBeNull();
    expect(within(header).getAllByText("WhatsApp")).toHaveLength(1); // la red, en su fila
    act(() => box().focus());
    expect(panel).toHaveAttribute("data-composing");
    const [network] = within(header).getAllByText("WhatsApp"); // y, sola, bajo el nombre
    expect(network!.closest("p")).toHaveClass("hidden", "max-sm:flex");
    expect(fireEvent.mouseDown(more)).toBe(false); // pulsarlo no quita el foco antes del clic
    fireEvent.click(more);
    expect(panel).not.toHaveAttribute("data-composing");
    expect(document.activeElement).toBe(button("Actualizar mensajes"));
    await tick();
    expect(panel).not.toHaveAttribute("data-composing");
  });

  it("el aviso de atención humana queda en una línea con su acción, sin perder el texto", async () => {
    const api = serve();
    show();
    await openChat();
    const line = screen.getByText("Atención humana solicitada:").closest("p")!;
    expect(line).toHaveClass("max-sm:group-data-composing:truncate");
    expect(line).toHaveTextContent("Atención humana solicitada: La pidió quien atiende. por ti");
    const close = () =>
      screen.getAllByRole("button", { name: "Cerrar la solicitud de atención humana" });
    expect(close()).toHaveLength(1); // solo la de la fila mientras no se escribe
    act(() => box().focus());
    const [top, inLine] = close();
    expect(top).toHaveClass(COMPACT);
    expect(inLine).toHaveClass("hidden", "max-sm:inline-flex", "max-lg:size-11");
    expect(line.parentElement).toContainElement(inLine!);
    fireEvent.click(inLine!); // la misma acción que la de la fila: cierra la solicitud una vez
    const deletes = () => api.mock.calls.filter(([, init]) => init?.method === "DELETE");
    await waitFor(() => expect(deletes()).toHaveLength(1));
    await tick();
    expect(deletes()).toHaveLength(1);
  });

  it("«Acciones» y los botones del copiloto tienen 44 px por debajo de 1024 px", async () => {
    serve();
    show();
    const panel = await openChat();
    fireEvent.click(button("Acciones"));
    expect(button("Acciones")).toHaveClass("h-11", "lg:hidden");
    for (const name of ["Resumir", "Sugerir respuesta"]) {
      expect(button(name)).toHaveClass("h-9", "max-lg:h-11", "max-sm:w-11");
    }
    fireEvent.click(button("Sugerir respuesta"));
    await screen.findByText("Hola Rosa");
    for (const name of ["Insertar", "Descartar"]) expect(button(name)).toHaveClass("max-lg:h-11");
    fireEvent.click(button("Excluir de la IA"));
    await waitFor(() => expect(document.activeElement).toBe(button("Cancelar")));
    // Ningún botón del chat en 36–40 px bajo 1024 px: un `h-9` lleva su 44 px (`[…]max-lg:…-11`).
    for (const each of panel.querySelectorAll("button")) {
      const name = each.textContent ?? "";
      expect(each.className, name).not.toMatch(/(^| )(sm:min-h-9|sm:h-9|h-10|max-sm:w-10)( |$)/);
      if (/(^| )h-9( |$)/.test(each.className)) {
        expect(each.className, name).toMatch(/(^| )([a-z:-]*max-lg:)?(min-h|h|size)-11( |$)/);
      }
    }
  });
});

describe("pulido de la cabecera compacta (UX-INBOX-03c)", () => {
  it("compacto, pulsar un botón del panel no quita el foco al cuadro; fuera o sin compactar, sí", async () => {
    serve();
    show();
    const panel = await openChat();
    const row = within(screen.getByRole("region", { name: "Conversaciones" })).getByRole("button", {
      name: /Rosa Díaz/,
    });
    fireEvent.change(box(), { target: { value: "Hola" } });
    const pressed = (element: Element) => fireEvent.mouseDown(element); // `false`: sin foco nuevo
    for (const name of ["Enviar", "Volver a las conversaciones", "Adjuntar imagen o PDF"]) {
      expect(pressed(button(name)), name).toBe(true); // aún sin compactar
    }
    act(() => box().focus());
    expect(panel).toHaveAttribute("data-composing");
    for (const name of ["Enviar", "Volver a las conversaciones", "Adjuntar imagen o PDF"]) {
      expect(pressed(button(name).firstElementChild!), name).toBe(false); // su icono, dentro
    }
    expect(pressed(button("Más acciones de la conversación"))).toBe(false);
    expect(pressed(box())).toBe(true); // el cuadro mismo, ni lo que no es botón
    expect(pressed(row)).toBe(true); // fuera del panel
    const picker = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => {});
    fireEvent.click(button("Adjuntar imagen o PDF")); // el clip sigue abriendo el selector
    expect(picker).toHaveBeenCalledTimes(1);
    act(() => screen.getByRole("region", { name: "Mensajes de la conversación" }).focus());
    await tick();
    expect(panel).not.toHaveAttribute("data-composing");
    expect(pressed(button("Enviar"))).toBe(true); // ya sin compactar, lo de siempre
    pointer(false); // con ratón, el cuadro enfocado no compacta ni retiene el foco
    act(() => box().focus());
    expect(panel).not.toHaveAttribute("data-composing");
    expect(pressed(button("Enviar"))).toBe(true);
  });

  it("en una tableta táctil (≥640 px) no hay vista compacta: pulsar mueve el foco y la ventana de respuestas se cierra", async () => {
    pointer(true, 800);
    Object.assign(HTMLDialogElement.prototype, {
      show(this: HTMLDialogElement) {
        this.open = true; // jsdom no trae los métodos de `<dialog>`
      },
      close(this: HTMLDialogElement) {
        this.open = false;
        this.dispatchEvent(new Event("close"));
      },
    });
    serve();
    show();
    await openChat();
    act(() => box().focus());
    expect(fireEvent.mouseDown(button("Enviar"))).toBe(true);
    fireEvent.click(button("Respuestas rápidas"));
    const popup = await screen.findByRole("dialog", { name: "Respuestas rápidas" });
    const search = within(popup).getByRole("combobox");
    await waitFor(() => expect(search).toHaveFocus());
    const more = button("Actualizar mensajes");
    if (fireEvent.mouseDown(more)) act(() => more.focus()); // lo que hace el navegador si no se impide
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("en el teléfono, con el foco fuera del cuadro (el buscador de respuestas), pulsar no se impide", async () => {
    serve();
    show();
    const panel = await openChat();
    act(() => box().focus());
    expect(panel).toHaveAttribute("data-composing");
    const search = document.createElement("input"); // otro campo del redactor, enfocado
    box().closest("form")!.append(search);
    act(() => search.focus());
    expect(fireEvent.mouseDown(button("Enviar"))).toBe(true);
    act(() => box().focus());
    expect(fireEvent.mouseDown(button("Enviar"))).toBe(false);
    search.remove();
  });

  it("Android: si el teclado se cierra sin quitar el foco, se suelta el cuadro; girar o la barra no", async () => {
    const viewport = Object.assign(new EventTarget(), { width: 390, height: 780, scale: 1 });
    vi.stubGlobal("visualViewport", viewport);
    serve();
    show();
    const panel = await openChat();
    const resize = (width: number, height: number, scale = 1) =>
      act(
        () =>
          void Object.assign(viewport, { width, height, scale }).dispatchEvent(new Event("resize")),
      );
    act(() => box().focus());
    resize(390, 400); // se abre el teclado
    resize(390, 456); // la barra de direcciones se recoge (56 px)
    resize(844, 200); // se gira con el teclado abierto…
    resize(390, 400); // …y se vuelve: crece 200 px, pero con otro ancho
    resize(390, 370); // el teclado crece un poco (emojis)
    resize(390, 500); // y vuelve: 130 px
    resize(390, 250, 2); // se amplía con los dedos: lo visible en CSS se reduce, en pantalla no
    resize(390, 500);
    expect(box()).toHaveFocus();
    expect(panel).toHaveAttribute("data-composing");
    resize(390, 521); // 151 px más que lo menor visto: el teclado se cerró
    expect(box()).not.toHaveFocus();
    await tick();
    expect(panel).not.toHaveAttribute("data-composing");
    act(() => box().focus()); // otra vez: se mide desde aquí, no desde lo de antes
    expect(box()).toHaveFocus();
    pointer(false); // con ratón, cambiar el tamaño de la ventana no suelta nada
    act(() => box().focus());
    resize(390, 300);
    resize(390, 780);
    expect(box()).toHaveFocus();
  });

  it("con lo último a la vista, encoger el chat lo sigue: el desplazamiento al encoger no cuenta", async () => {
    vi.stubGlobal("visualViewport", Object.assign(new EventTarget(), { height: 780, scale: 1 }));
    const watched: [ResizeObserverCallback, Element[]][] = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        targets: Element[] = [];
        constructor(callback: ResizeObserverCallback) {
          watched.push([callback, this.targets]);
        }
        observe(target: Element) {
          this.targets.push(target);
        }
        disconnect() {}
      },
    );
    serve();
    show();
    await openChat();
    const scroller = screen.getByRole("region", { name: "Mensajes de la conversación" });
    const size = { clientHeight: 500, scrollHeight: 1000, scrollTop: 500 };
    for (const key of Object.keys(size) as (keyof typeof size)[]) {
      Object.defineProperty(scroller, key, {
        get: () => size[key],
        // como el navegador: `scrollTop` no pasa del final
        set: (value: number) =>
          (size[key] = Math.min(value, size.scrollHeight - size.clientHeight)),
      });
    }
    const resized = () =>
      act(() =>
        watched
          .filter(([, targets]) => targets.includes(scroller))
          .forEach(([callback]) => callback([], {} as ResizeObserver)),
      );
    resized(); // se mide el alto: lo último está a la vista
    fireEvent.scroll(scroller);
    size.clientHeight = 300; // se abre el teclado: el navegador avisa del desplazamiento antes
    fireEvent.scroll(scroller);
    resized();
    expect(size.scrollTop).toBe(700); // sigue en lo último
  });

  it("los márgenes del chat compacto son las medidas del AppShell, definidas una vez", async () => {
    serve();
    show();
    const panel = await openChat();
    const shell = [
      "max-sm:data-composing:-mx-(--shell-gutter)",
      "max-sm:data-composing:-mt-[calc(var(--shell-bar)+var(--shell-pad))]",
      "max-sm:data-composing:-mb-(--shell-pad)",
    ];
    expect(panel).toHaveClass(...shell);
    const css = readFileSync("src/app/globals.css", "utf8"); // vitest corre en frontend/
    for (const name of ["--shell-bar: 60px;", "--shell-gutter: 1rem;", "--shell-pad: 2rem;"]) {
      expect(css).toContain(name);
    }
  });

  it("escribiendo, la etiqueta del cuadro queda para lectores; «Reintentar» mide 44 px", async () => {
    mockApi({
      [`GET ${BASE}/`]: page([rosa]),
      [`GET ${BASE}/1/`]: { status: 200, body: rosa },
      [`GET ${BASE}/1/messages/`]: { status: 500, body: { code: "INTERNAL_ERROR" } },
    });
    show();
    await openChat();
    const label = screen.getByText("Responder al cliente", { selector: "label" });
    expect(label).toHaveClass("max-sm:group-data-composing:sr-only");
    const retry = await screen.findByRole("button", { name: "Reintentar" });
    expect(retry).toHaveClass("min-h-11", "lg:min-h-9");
    expect(retry).not.toHaveClass("sm:min-h-9");
  });
});
