import {
  act,
  cleanup,
  configure,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Grant, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";

const BASE = "/api/v1/o/acme/conversations";
const PUT = `PUT ${BASE}/1/ai-exclusion/`;
const STEP_UP = "POST /api/v1/auth/mfa/step-up/";
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
const AGENT = grants("conversations.view", "messages.send", "ai.use");
const MANAGER = [...AGENT, ...grants("ai.manage")];
const rosa = {
  ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
  ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" }, assignment_version: 1 },
  ...{
    channel: {
      id: "ch1",
      platform: "SANDBOX",
      name: "Sandbox",
      text_max: 4096,
      text_unit: "CHARACTERS",
      accepts_media: true,
    },
    human_request: null,
  },
  ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: null, version: 1 },
  ...{ last_message_preview: null, last_message_direction: null, standby_since: null },
  ai_excluded_at: null as string | null,
};
type Reply = { status: number; body: unknown };
type Sent = { excluded: boolean; version: number };
const STEP: Reply = {
  status: 403,
  body: { code: "STEP_UP_REQUIRED", detail: "TEXTO DEL SERVIDOR" },
};
const refusal = (status: number, code: string): Reply => ({ status, body: { code, detail: "x" } });

// La conversación vive en el "servidor": la ruta la cambia con lo enviado, salvo las respuestas de
// `refused`, que se dan una vez cada una y en orden; con otra versión, 409 como el backend. Con
// `state.reads = false`, releer falla; `other` es otra persona que cambia la marca.
function server(initial: Partial<typeof rosa> = {}, ...refused: Reply[]) {
  const state = { ...rosa, ...initial, reads: true, before: () => {} };
  const read = (body: unknown) => (state.reads ? { status: 200, body } : { status: 500, body: {} });
  const api: ReturnType<typeof mockApi> = mockApi({
    [`GET ${BASE}/`]: () => read({ results: [{ ...state }], next: null }),
    [`GET ${BASE}/1/`]: () => read({ ...state }),
    [`GET ${BASE}/1/messages/`]: { status: 200, body: { results: [], next: null } },
    [STEP_UP]: { status: 200, body: { step_up_expires_in: 900 } },
    [PUT]: () => {
      const answer = refused.shift();
      if (answer) return answer;
      state.before(); // otra persona, mientras viaja la petición
      const sent = api.mock.calls.at(-1)?.[1]?.body;
      const { excluded, version } = JSON.parse(String(sent)) as Sent;
      if (version !== state.version) return refusal(409, "CONVERSATION_VERSION_CONFLICT");
      state.ai_excluded_at = excluded ? "2026-11-22T15:30:00Z" : null;
      state.version += 1;
      return {
        status: 200,
        body: { id: "1", ai_excluded_at: state.ai_excluded_at, version: state.version },
      };
    },
  });
  const calls = (route: string) =>
    api.mock.calls
      .filter(([to, init]) => `${init?.method ?? "GET"} ${String(to)}` === route)
      .map(([, init]) => (init?.body ? JSON.parse(String(init.body)) : null));
  const other = (excluded: boolean) => {
    state.ai_excluded_at = excluded ? "2026-11-22T16:00:00Z" : null;
    state.version += 1;
  };
  return { calls, state, other };
}
const show = (permissions: Grant[]) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions }}>
      <Inbox />
    </TenantProvider>,
  );
const openChat = async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz/ }));
  return screen.findByRole("heading", { level: 2 });
};
// Abre la confirmación (el foco va a «Cancelar») y devuelve el botón de confirmar.
const ask = async (name: string) => {
  fireEvent.click(await screen.findByRole("button", { name }));
  const group = screen.getByRole("group", { name });
  const cancel = within(group).getByRole("button", { name: "Cancelar" });
  await waitFor(() => expect(document.activeElement).toBe(cancel)); // lo seguro primero
  return within(group).getByRole("button", { name: "Confirmar" });
};
const verify = async () => {
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText(/^Código/), { target: { value: "123456" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Verificar" }));
};

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });
beforeAll(() => {
  // jsdom no implementa el `<dialog>` modal (el del código de segundo factor): se refleja `open`.
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.open = false;
    this.dispatchEvent(new Event("close"));
  };
});
beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("exclusión de la IA en el Inbox (F8-13b)", () => {
  it("quien atiende la excluye tras confirmar; el copiloto se va y no puede deshacerlo", async () => {
    const s = server();
    show(AGENT);
    await openChat();
    expect(await screen.findByRole("button", { name: "Sugerir respuesta" })).toBeVisible();
    fireEvent.click(await ask("Excluir de la IA"));
    const done = await screen.findByText("Conversación excluida de la IA.");
    expect(done).toHaveAttribute("role", "status");
    await waitFor(() => expect(document.activeElement).toBe(done)); // el botón pulsado ya no está
    expect(s.calls(PUT)).toEqual([{ excluded: true, version: 1 }]);
    expect(screen.getByText("Excluida de la IA")).toBeVisible();
    expect(screen.getByRole("note")).toHaveTextContent("excluida de la IA");
    expect(screen.queryByRole("button", { name: "Sugerir respuesta" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Resumir" })).toBeNull();
    // Sin `ai.manage` no se ofrece volver a incluirla, ni con la marca puesta.
    expect(screen.queryByRole("button", { name: /Volver a incluir/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Excluir de la IA" })).toBeNull();
    // La lista, al releerse, la marca.
    expect(await screen.findByText("Sin IA")).toBeVisible();
  });

  it("cancelar no envía nada, y un doble clic en confirmar es una sola petición", async () => {
    const s = server();
    show(AGENT);
    await openChat();
    await ask("Excluir de la IA");
    expect(screen.getByRole("group", { name: "Excluir de la IA" })).toHaveAccessibleDescription(
      /Vale solo para esta conversación, no para el contacto/,
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancelar" }));
    expect(screen.queryByRole("group", { name: "Excluir de la IA" })).toBeNull();
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Excluir de la IA" })),
    ); // vuelve a quien la abrió
    expect(s.calls(PUT)).toEqual([]);
    const yes = await ask("Excluir de la IA");
    fireEvent.click(yes);
    fireEvent.click(yes);
    expect(await screen.findByText("Conversación excluida de la IA.")).toBeVisible();
    expect(s.calls(PUT)).toEqual([{ excluded: true, version: 1 }]);
  });

  it("volver a incluirla pide `ai.manage`, la confirmación y el segundo factor", async () => {
    const s = server({ ai_excluded_at: "2026-11-21T10:00:00Z", version: 4 }, STEP);
    show(MANAGER);
    await openChat();
    expect(await screen.findByText("Excluida de la IA")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Sugerir respuesta" })).toBeNull();
    fireEvent.click(await ask("Volver a incluir en la IA"));
    await verify();
    const done = await screen.findByText("La conversación vuelve a estar incluida en la IA.");
    await waitFor(() => expect(document.activeElement).toBe(done)); // no el botón, ya con otro texto
    expect(s.calls(STEP_UP)).toEqual([{ code: "123456" }]);
    // la primera la rechazó el servidor por el segundo factor; se repite una vez, con la misma versión
    expect(s.calls(PUT)).toEqual([
      { excluded: false, version: 4 },
      { excluded: false, version: 4 },
    ]);
    expect(await screen.findByRole("button", { name: "Sugerir respuesta" })).toBeVisible();
    expect(screen.queryByText("Excluida de la IA")).toBeNull();
  });

  it("si la marca cambia con el código abierto, la pregunta sigue siendo la leída", async () => {
    const s = server({ ai_excluded_at: "2026-11-21T10:00:00Z" }, STEP);
    show(MANAGER);
    await openChat();
    fireEvent.click(await ask("Volver a incluir en la IA"));
    const dialog = await screen.findByRole("dialog");
    s.other(false); // otra persona la vuelve a incluir mientras se pide el código
    fireEvent.click(screen.getByRole("button", { name: "Actualizar mensajes" }));
    await waitFor(() => expect(screen.queryByText("Excluida de la IA")).toBeNull());
    const group = screen.getByRole("group", { name: "Volver a incluir en la IA" });
    expect(group).toHaveTextContent("¿Volver a incluir esta conversación en la IA?");
    expect(group).not.toHaveTextContent("¿Excluir");
    // Al cancelar el código, lo leído ya no vale: se cierra sin enviar nada más.
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Otra persona cambió esta conversación",
    );
    expect(screen.queryByRole("group", { name: "Volver a incluir en la IA" })).toBeNull();
    expect(s.calls(PUT)).toEqual([{ excluded: false, version: 1 }]);
  });

  it("cancelar el segundo factor no cambia nada ni lo trata como un fallo", async () => {
    const s = server({ ai_excluded_at: "2026-11-21T10:00:00Z" }, STEP);
    show(MANAGER);
    await openChat();
    fireEvent.click(await ask("Volver a incluir en la IA"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // Solo la que respondió `STEP_UP_REQUIRED`: tras cancelar no se envía nada más.
    expect(s.calls(PUT)).toEqual([{ excluded: false, version: 1 }]);
    expect(s.calls(STEP_UP)).toEqual([]);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/vuelve a estar incluida/)).toBeNull();
    expect(screen.getByText("Excluida de la IA")).toBeVisible();
  });

  // Escenario del propietario (#1081), volver a incluir: el código abierto, otra persona cambia la
  // marca; la pregunta sigue siendo la leída y tras el código se envía la foto y se muestra el 409.
  it("con el código abierto otra persona la incluye: tras el código va lo leído, 409 y nada más", async () => {
    const s = server({ ai_excluded_at: "2026-11-21T10:00:00Z" }, STEP);
    show(MANAGER);
    await openChat();
    fireEvent.click(await ask("Volver a incluir en la IA"));
    await screen.findByRole("dialog");
    s.other(false); // versión 2, ya incluida
    fireEvent.click(screen.getByRole("button", { name: "Actualizar mensajes" }));
    await waitFor(() => expect(screen.queryByText("Excluida de la IA")).toBeNull());
    expect(screen.getByRole("group", { name: "Volver a incluir en la IA" })).toHaveTextContent(
      "¿Volver a incluir esta conversación en la IA?",
    );
    await verify();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Otra persona cambió esta conversación",
    );
    expect(screen.queryByRole("group", { name: "Volver a incluir en la IA" })).toBeNull();
    await act(async () => {});
    expect(s.calls(PUT)).toEqual([
      { excluded: false, version: 1 }, // la que pidió el código
      { excluded: false, version: 1 }, // la repetición: la foto, no la acción contraria
    ]);
    expect(s.calls(STEP_UP)).toEqual([{ code: "123456" }]);
  });

  // El mismo escenario al excluir: excluir no pide código (ADR-034 §5), así que el cambio de otra
  // persona llega mientras viaja la petición.
  it("si otra persona la excluye mientras viaja la petición, va lo leído, 409 y nada más", async () => {
    const s = server();
    show(MANAGER);
    await openChat();
    const yes = await ask("Excluir de la IA");
    s.state.before = () => s.other(true);
    fireEvent.click(yes);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Otra persona cambió esta conversación",
    );
    expect(screen.queryByRole("group", { name: "Excluir de la IA" })).toBeNull();
    await act(async () => {});
    expect(s.calls(PUT)).toEqual([{ excluded: true, version: 1 }]);
    expect(s.calls(STEP_UP)).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("si la repetición también pide el segundo factor, lo dice sin bucle", async () => {
    const s = server({ ai_excluded_at: "2026-11-21T10:00:00Z" }, STEP, STEP);
    show(MANAGER);
    await openChat();
    fireEvent.click(await ask("Volver a incluir en la IA"));
    await verify();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Confirma tu identidad con un código de segundo factor para volver a incluirla.",
    );
    expect(s.calls(PUT)).toHaveLength(2);
  });

  it("el copiloto cambia con la respuesta, aunque la relectura falle", async () => {
    const s = server();
    show(AGENT);
    await openChat();
    expect(await screen.findByRole("button", { name: "Sugerir respuesta" })).toBeVisible();
    const yes = await ask("Excluir de la IA");
    s.state.reads = false; // solo la respuesta del PUT puede cambiar la pantalla
    fireEvent.click(yes);
    expect(await screen.findByText("Conversación excluida de la IA.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Sugerir respuesta" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Resumir" })).toBeNull();
    expect(screen.getByText("Excluida de la IA")).toBeVisible();
  });

  it("una versión vieja lo dice, cierra la confirmación y no reenvía; al reabrir usa la nueva", async () => {
    const s = server();
    show(AGENT);
    await openChat();
    const yes = await ask("Excluir de la IA");
    s.state.version = 2; // otra persona cambió otra cosa de la conversación después de leerla
    fireEvent.click(yes);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Otra persona cambió esta conversación");
    expect(alert).not.toHaveTextContent("x");
    expect(screen.queryByRole("group", { name: "Excluir de la IA" })).toBeNull(); // nada listo
    expect(screen.queryByText("Excluida de la IA")).toBeNull();
    const opener = screen.getByRole("button", { name: "Excluir de la IA" });
    await waitFor(() => expect(document.activeElement).toBe(opener));
    expect(s.calls(PUT)).toEqual([{ excluded: true, version: 1 }]); // nada se reenvía solo
    await waitFor(() => expect(s.calls(`GET ${BASE}/1/`).length).toBeGreaterThan(1)); // relee
    await act(async () => {}); // y la relectura llega a la caché
    fireEvent.click(await ask("Excluir de la IA"));
    expect(await screen.findByText("Conversación excluida de la IA.")).toBeVisible();
    expect(s.calls(PUT)).toEqual([
      { excluded: true, version: 1 },
      { excluded: true, version: 2 },
    ]);
  });

  // La revisión de #1081: con la confirmación abierta, una relectura (tiempo real, «Actualizar»)
  // con la marca cambiada no convierte «Confirmar» en la acción contraria.
  it.each([
    ["Excluir de la IA", false],
    ["Volver a incluir en la IA", true],
  ])(
    "si otra persona cambia la marca con «%s» abierto, se cierra sin enviar",
    async (name, was) => {
      const s = server({ ai_excluded_at: was ? "2026-11-21T10:00:00Z" : null });
      show(MANAGER);
      await openChat();
      await ask(name);
      s.other(!was);
      fireEvent.click(screen.getByRole("button", { name: "Actualizar mensajes" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Otra persona cambió esta conversación",
      );
      expect(screen.queryByRole("group", { name })).toBeNull();
      expect(screen.queryByRole("button", { name: "Confirmar" })).toBeNull();
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(s.calls(PUT)).toEqual([]);
      expect(s.calls(STEP_UP)).toEqual([]);
    },
  );

  it.each([
    ["Excluir de la IA", null, { excluded: true, version: 1 }],
    ["Volver a incluir en la IA", "2026-11-21T10:00:00Z", { excluded: false, version: 1 }],
  ])("un 409 en «%s» cierra la confirmación y no reenvía", async (name, at, sent) => {
    const s = server({ ai_excluded_at: at });
    show(MANAGER);
    await openChat();
    const yes = await ask(name);
    s.state.version = 2; // otra cosa cambió; la marca, no
    fireEvent.click(yes);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Otra persona cambió esta conversación",
    );
    expect(screen.queryByRole("group", { name })).toBeNull();
    await act(async () => {});
    expect(s.calls(PUT)).toEqual([sent]);
    expect(s.calls(STEP_UP)).toEqual([]);
  });

  it("con la marca igual pero otra versión, confirmar envía lo leído y la API lo rechaza", async () => {
    const s = server();
    show(MANAGER);
    await openChat();
    await ask("Excluir de la IA");
    s.state.version = 2;
    fireEvent.click(screen.getByRole("button", { name: "Actualizar mensajes" }));
    await waitFor(() => expect(s.calls(`GET ${BASE}/1/`).length).toBeGreaterThan(1));
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "Confirmar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Otra persona cambió esta conversación",
    );
    expect(s.calls(PUT)).toEqual([{ excluded: true, version: 1 }]);
  });

  it.each([
    ["AI_CALL_IN_PROGRESS", "La IA sigue respondiendo en esta conversación. No se cambió nada"],
    ["CONVERSATION_BUSY", "La conversación está ocupada. No se cambió nada"], // F8-13d
  ])("con %s lo dice, no cambia nada y reintentar funciona", async (code, text) => {
    const s = server({}, refusal(409, code));
    show(AGENT);
    await openChat();
    fireEvent.click(await ask("Excluir de la IA"));
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(s.state.ai_excluded_at).toBeNull();
    expect(screen.queryByText("Excluida de la IA")).toBeNull();
    // F8-13d: la confirmación sigue abierta con lo leído; confirmar otra vez sí excluye
    fireEvent.click(screen.getByRole("button", { name: "Confirmar" }));
    expect(await screen.findByText("Conversación excluida de la IA.")).toBeVisible();
    expect(s.state.ai_excluded_at).not.toBeNull();
    expect(s.calls(PUT)).toEqual([
      { excluded: true, version: 1 },
      { excluded: true, version: 1 },
    ]);
  });

  it("el aviso de lo hecho se va si otra persona cambia la marca", async () => {
    const s = server();
    show(MANAGER);
    await openChat();
    fireEvent.click(await ask("Excluir de la IA"));
    expect(await screen.findByText("Conversación excluida de la IA.")).toBeVisible();
    s.other(false);
    fireEvent.click(screen.getByRole("button", { name: "Actualizar mensajes" }));
    await waitFor(() => expect(screen.queryByText("Excluida de la IA")).toBeNull());
    expect(screen.queryByText("Conversación excluida de la IA.")).toBeNull();
  });

  it.each([
    [403, "PERMISSION_DENIED", "Excluir de la IA", null],
    [404, "NOT_FOUND", "Excluir de la IA", null],
    [403, "PERMISSION_DENIED", "Volver a incluir en la IA", "2026-11-21T10:00:00Z"],
    [404, "NOT_FOUND", "Volver a incluir en la IA", "2026-11-21T10:00:00Z"],
  ])("un %i (%s) en «%s» dice que no puede cambiarla", async (status, code, name, at) => {
    const s = server({ ai_excluded_at: at }, refusal(status, code));
    show(MANAGER);
    await openChat();
    fireEvent.click(await ask(name));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "No puedes cambiar esta conversación. Puede que ya no esté en tu alcance.",
    );
    expect(alert).not.toHaveTextContent("x");
    await act(async () => {});
    expect(s.calls(PUT)).toEqual([{ excluded: at === null, version: 1 }]); // nada se reenvía
    expect(s.calls(STEP_UP)).toEqual([]);
  });
});
