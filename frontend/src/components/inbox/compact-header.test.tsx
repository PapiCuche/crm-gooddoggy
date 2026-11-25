import { act, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
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
const pointer = (touch: boolean) =>
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: touch && query.includes("coarse") }));
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
    expect(more).toHaveAttribute("aria-expanded", "false");
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
