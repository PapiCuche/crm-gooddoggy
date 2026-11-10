import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, Grant, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";

const BASE = "/api/v1/o/acme/conversations";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [],
};
const grants = (...codes: string[]): Grant[] =>
  codes.map((code) => ({ code, scopes: ["ORGANIZATION" as const] }));
const ASSIGN = ["conversations.view", "conversations.assign"];
const member = (id: string, first: string, status = "ACTIVE") => ({
  ...{ id: `m-${id}`, status, joined_at: "2026-01-01T00:00:00Z", default_branch: null, roles: [] },
  user: { id, email: `${id}@acme.pe`, first_name: first, last_name: "Ramos" },
});
const MEMBERS = [member("u1", "Ana"), member("u2", "Luis"), member("u3", "Marta", "SUSPENDED")];
const first = {
  ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
  ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" }, assignment_version: 3, version: 1 },
  ...{ channel: { id: "ch1", platform: "SANDBOX", name: "Sandbox" }, last_message_preview: null },
  ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: null },
  ...{ last_message_direction: null },
};

type Reply = { status: number; body?: unknown };
// La conversación vive en el "servidor": el detalle y la lista leen su estado actual y la
// asignación lo cambia, como la API.
function server(initial: Partial<Conversation> = {}, members: unknown[] = MEMBERS) {
  const state = { ...first, ...initial } as Record<string, unknown>;
  let reply: (() => Reply) | null = null;
  const api = mockApi({
    [`GET ${BASE}/`]: () => ({ status: 200, body: { results: [state], next: null } }),
    [`GET ${BASE}/1/`]: () => ({ status: 200, body: state }),
    [`GET ${BASE}/1/messages/`]: { status: 200, body: { results: [], next: null } },
    ["GET /api/v1/o/acme/members/?limit=200"]: {
      status: 200,
      body: { results: members, next: null },
    },
    [`POST ${BASE}/1/assign/`]: () => (reply ?? (() => ({ status: 500 })))(),
  });
  let release = () => {};
  let hold: Promise<void> | null = null;
  const inner = api.getMockImplementation()!;
  api.mockImplementation(async (input, init) => {
    if (init?.method === "POST") await hold;
    return inner(input, init);
  });
  const posts = () =>
    api.mock.calls
      .filter(([url, init]) => String(url).endsWith("/1/assign/") && init?.method === "POST")
      .map(([, init]) => JSON.parse(String(init!.body)) as Record<string, unknown>);
  const reads = () =>
    api.mock.calls.filter(([url, init]) => String(url) === `${BASE}/1/` && init?.method !== "POST")
      .length;
  const messageReads = () =>
    api.mock.calls.filter(([url]) => String(url) === `${BASE}/1/messages/`).length;
  return {
    api,
    state,
    posts,
    reads,
    messageReads,
    wait: () => void (hold = new Promise<void>((resolve) => (release = resolve))),
    go: () => release(),
    // La respuesta de la asignación: aplica `change` al estado y devuelve el estado de la asignación.
    answer: (change: Record<string, unknown> | Reply) => {
      reply = () => {
        if (
          "status" in change &&
          typeof change.status === "number" &&
          !("assigned_user_id" in change)
        ) {
          return change as Reply;
        }
        Object.assign(state, change, { assignment_version: Number(state.assignment_version) + 1 });
        return { status: 200, body: state };
      };
    },
  };
}
const show = (codes = [...ASSIGN, "users.view"]) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions: grants(...codes) }}>
      <Inbox />
    </TenantProvider>,
  );
const openChat = async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz/ }));
  return screen.findByRole("heading", { level: 2 });
};
const header = () => within(screen.getByRole("region", { name: "Conversación" }));
// La línea «Asignada a …» (el nombre va en un <bdi>, así que se compara el texto entero del párrafo).
const line = async (text: string) => {
  const find = () =>
    header().getByText((_, one) => one?.tagName === "P" && one.textContent === text);
  await waitFor(() => expect(find()).toBeInTheDocument());
  return find();
};

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("asignación en la pantalla (F6-05b)", () => {
  it("en 390 px las acciones van tras «Acciones»: aria-expanded, abre y cierra, Escape devuelve el foco", async () => {
    server();
    show();
    await openChat();
    const toggle = await header().findByRole("button", { name: "Acciones" });
    const controls = toggle.getAttribute("aria-controls")!;
    const panel = () => document.getElementById(controls)!;
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(panel()).toHaveClass("max-lg:hidden"); // plegado en pantallas estrechas
    expect(panel()).toContainElement(header().getByRole("button", { name: "Tomar" }));
    expect(panel()).toContainElement(header().getByLabelText("Transferir a"));
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(panel()).not.toHaveClass("max-lg:hidden");
    expect(panel()).toHaveClass("max-lg:absolute"); // se abre sobre los mensajes, sin empujarlos
    expect(toggle).toHaveClass("lg:hidden"); // en escritorio no hay botón: siempre abiertas
    expect(toggle).toHaveClass("h-10"); // 40 px en móvil
    header().getByRole("button", { name: "Tomar" }).focus();
    fireEvent.keyDown(panel(), { key: "Escape" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveFocus();
  });

  it("sin permiso o con la conversación cerrada no hay botón «Acciones»", async () => {
    server({ status: "CLOSED" });
    show();
    await openChat();
    expect(await line("Sin asignar")).toBeVisible();
    expect(header().queryByRole("button", { name: "Acciones" })).toBeNull();
    cleanup();
    server();
    show(["conversations.view", "users.view"]);
    await openChat();
    expect(await line("Sin asignar")).toBeVisible();
    expect(header().queryByRole("button", { name: "Acciones" })).toBeNull();
  });

  it("sin `conversations.assign` se ve quién la tiene, pero no hay botones", async () => {
    server();
    show(["conversations.view", "users.view"]);
    await openChat();
    expect(await line("Sin asignar")).toBeVisible();
    for (const name of ["Tomar", "Soltar", "Transferir"]) {
      expect(header().queryByRole("button", { name })).toBeNull();
    }
    expect(header().queryByLabelText("Transferir a")).toBeNull();
  });

  it("tomar envía la versión leída, se ve al instante y vuelve a leer detalle, lista y mensajes", async () => {
    const s = server();
    s.answer({ assigned_user_id: "u1", assignee_kind: "USER" });
    show();
    await openChat();
    expect(header().queryByRole("button", { name: "Soltar" })).toBeNull();
    const reads = s.reads();
    const messages = s.messageReads();
    const actions = await header().findByRole("button", { name: "Acciones" });
    fireEvent.click(actions); // en 390 px se abren antes de usarlas
    expect(actions).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(await header().findByRole("button", { name: "Tomar" }));
    expect(await header().findByText("Tomaste la conversación.")).toBeVisible();
    expect(s.posts()).toEqual([{ action: "TAKE", assignment_version: 3 }]);
    await waitFor(async () => expect(await line("Asignada a ti")).toHaveFocus()); // el botón pulsado desaparece
    expect(header().getByRole("button", { name: "Acciones" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(header().queryByRole("button", { name: "Tomar" })).toBeNull();
    expect(header().getByRole("button", { name: "Soltar" })).toBeVisible();
    await waitFor(() => expect(s.reads()).toBeGreaterThan(reads)); // el detalle se vuelve a leer
    await waitFor(() => expect(s.messageReads()).toBeGreaterThan(messages)); // y los mensajes
    // la fila de la lista dice lo mismo
    expect(screen.getByRole("button", { name: /Rosa Díaz/ })).toHaveTextContent("Asignada a ti");
  });

  it("un segundo clic con la petición en vuelo no envía otra", async () => {
    const s = server();
    s.answer({ assigned_user_id: "u1", assignee_kind: "USER" });
    show();
    await openChat();
    s.wait();
    const take = await header().findByRole("button", { name: "Tomar" });
    fireEvent.click(take);
    await waitFor(() => expect(take).toHaveAttribute("aria-disabled", "true"));
    fireEvent.click(take);
    s.go();
    expect(await line("Asignada a ti")).toBeVisible();
    expect(s.posts()).toHaveLength(1);
  });

  it("una cerrada sin `conversations.assign` no muestra el aviso de asignación", async () => {
    server({ status: "CLOSED" });
    show(["conversations.view"]);
    await openChat();
    expect(await line("Sin asignar")).toBeVisible();
    expect(header().queryByText(/no se puede asignar/)).toBeNull();
  });

  it("si otra persona la tomó antes lo explica, vuelve a leer y ya no ofrece tomar", async () => {
    const s = server();
    show();
    await openChat();
    await header().findByRole("button", { name: "Tomar" });
    Object.assign(s.state, {
      assigned_user_id: "u2",
      assignee_kind: "USER",
      assignment_version: 4,
    });
    s.answer({ status: 409, body: { code: "ASSIGNMENT_CONFLICT" } });
    fireEvent.click(header().getByRole("button", { name: "Tomar" }));
    expect(await header().findByRole("alert")).toHaveTextContent("Alguien la tomó antes");
    expect(await line("Asignada a Luis Ramos")).toBeVisible();
    await waitFor(() => expect(header().queryByRole("button", { name: "Tomar" })).toBeNull());
    expect(s.posts()).toEqual([{ action: "TAKE", assignment_version: 3 }]); // nada se reenvía solo
  });

  it("transferir ofrece solo miembros activos distintos de quien la tiene, con la versión leída", async () => {
    const s = server({ assigned_user_id: "u1", assignee_kind: "USER" });
    s.answer({ assigned_user_id: "u2", assignee_kind: "USER" });
    show();
    await openChat();
    const pick = await header().findByLabelText("Transferir a");
    const names = within(pick)
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(names).toEqual(["Elige a una persona…", "Luis Ramos"]); // ni Marta (suspendida) ni yo
    const go = header().getByRole("button", { name: "Transferir" });
    expect(go).toBeDisabled();
    fireEvent.change(pick, { target: { value: "u2" } });
    fireEvent.click(go);
    expect(await header().findByText("Transferiste la conversación a Luis Ramos.")).toBeVisible();
    expect(s.posts()).toEqual([{ action: "TRANSFER", user_id: "u2", assignment_version: 3 }]);
    expect(await line("Asignada a Luis Ramos")).toBeVisible();
    // ahora la tiene Luis, el único destino: no queda a quién transferirla
    expect(header().queryByLabelText("Transferir a")).toBeNull();
  });

  it("tras transferir, el selector vuelve a «Elige a una persona…» y «Transferir» a deshabilitado", async () => {
    const people = [...MEMBERS, member("u4", "Pedro")];
    const s = server({ assigned_user_id: "u1", assignee_kind: "USER" }, people);
    s.answer({ assigned_user_id: "u2", assignee_kind: "USER" });
    show();
    await openChat();
    const pick = await header().findByLabelText("Transferir a");
    fireEvent.change(pick, { target: { value: "u2" } });
    fireEvent.click(header().getByRole("button", { name: "Transferir" }));
    expect(await header().findByText("Transferiste la conversación a Luis Ramos.")).toBeVisible();
    await waitFor(() => expect(header().getByLabelText("Transferir a")).toHaveValue(""));
    expect(header().getByRole("button", { name: "Transferir" })).toBeDisabled();
  });

  it("sin el detalle leído no se ofrece ninguna acción (la versión sale del detalle), solo quién la tiene", async () => {
    const s = server();
    const inner = s.api.getMockImplementation()!;
    s.api.mockImplementation(async (input, init) =>
      String(input) === `${BASE}/1/`
        ? new Response(JSON.stringify({ code: "INTERNAL_ERROR" }), { status: 500 })
        : inner(input, init),
    );
    show();
    await openChat();
    expect(await header().findByRole("alert")).toBeVisible(); // el detalle no llegó
    expect(header().getByText("Sin asignar")).toBeVisible();
    expect(header().queryByRole("button", { name: "Tomar" })).toBeNull();
    expect(s.posts()).toEqual([]);
  });

  it("una conversación del equipo, sin persona, lo dice y se puede tomar", async () => {
    server({ assignee_kind: "TEAM", assigned_team_id: "t1" });
    show();
    const row = await screen.findByRole("button", { name: /Rosa Díaz/ });
    expect(row).toHaveTextContent("Asignada a un equipo");
    await openChat();
    expect(await line("Asignada a un equipo")).toBeVisible();
    expect(header().getByRole("button", { name: "Tomar" })).toBeVisible();
  });

  it("un miembro sin nombre se nombra por su correo", async () => {
    const nameless = { ...member("u2", ""), user: { ...member("u2", "").user, last_name: "" } };
    server({ assigned_user_id: "u2", assignee_kind: "USER" }, [nameless]);
    show();
    await openChat();
    expect(await line("Asignada a u2@acme.pe")).toBeVisible();
  });

  it("un destino inválido (400 en user_id) se explica sin tocar lo mostrado", async () => {
    const s = server({ assigned_user_id: "u1", assignee_kind: "USER" });
    s.answer({ status: 400, body: { code: "VALIDATION_ERROR", fields: { user_id: ["x"] } } });
    show();
    await openChat();
    fireEvent.change(await header().findByLabelText("Transferir a"), { target: { value: "u2" } });
    fireEvent.click(header().getByRole("button", { name: "Transferir" }));
    expect(await header().findByRole("alert")).toHaveTextContent(
      "No se puede transferir a esa persona",
    );
    expect(await line("Asignada a ti")).toBeVisible();
    expect(header().getByLabelText("Transferir a")).toHaveValue("u2");
  });

  it("soltar la deja sin responsable", async () => {
    const s = server({ assigned_user_id: "u1", assignee_kind: "USER" });
    s.answer({ assigned_user_id: null, assignee_kind: "UNASSIGNED", status: "PENDING" });
    show();
    await openChat();
    fireEvent.click(await header().findByRole("button", { name: "Soltar" }));
    expect(await header().findByText("Soltaste la conversación; vuelve a la cola.")).toBeVisible();
    expect(s.posts()).toEqual([{ action: "RELEASE", assignment_version: 3 }]);
    expect(await line("Sin asignar")).toBeVisible();
    expect(header().getByRole("button", { name: "Tomar" })).toBeVisible();
  });

  it("una conversación cerrada no se asigna y lo dice", async () => {
    const s = server({ status: "CLOSED" });
    show();
    await openChat();
    expect(
      await header().findByText("La conversación está cerrada: no se puede asignar."),
    ).toBeVisible();
    for (const name of ["Tomar", "Soltar", "Transferir"]) {
      expect(header().queryByRole("button", { name })).toBeNull();
    }
    expect(s.posts()).toEqual([]);
  });

  it("sin `users.view` no hay nombres ni destinos; el nombre ajeno se pinta como texto aislado", async () => {
    server({ assigned_user_id: "u2", assignee_kind: "USER" });
    show(ASSIGN);
    await openChat();
    expect(await line("Asignada")).toBeVisible();
    expect(header().queryByLabelText("Transferir a")).toBeNull();
    expect(header().getByRole("button", { name: "Soltar" })).toBeVisible();
    cleanup();
    const hostile = { ...member("u2", "<b>Luis</b>‮") };
    server({ assigned_user_id: "u2", assignee_kind: "USER" }, [hostile]);
    show();
    const row = await screen.findByRole("button", { name: /Rosa Díaz/ });
    await waitFor(() => expect(row).toHaveTextContent("Asignada a <b>Luis</b>"));
    const bdi = Array.from(row.querySelectorAll("bdi")).find((one) =>
      one.textContent?.includes("<b>"),
    );
    expect(bdi).toHaveAttribute("dir", "auto");
    expect(row.querySelector("b")).toBeNull();
  });
});
