import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { SessionActions } from "@/components/app-shell/session-actions";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Member, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { MembersList } from "./members-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const LIST = "GET /api/v1/o/acme/members/";
const STATUS = (id: string) => `PUT /api/v1/o/acme/members/${id}/status/`;
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "ana",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const member = (id: string, status: Member["status"] = "ACTIVE"): Member => ({
  id,
  status,
  joined_at: "2026-10-04T15:49:34Z",
  user: { id: `u-${id}`, email: `${id}@acme.pe`, first_name: "", last_name: "" },
  roles: [],
});
const everyone = [
  member("ana"),
  member("luis"),
  member("eva", "SUSPENDED"),
  member("ines", "INVITED"),
  member("raul", "DEACTIVATED"),
];
const list = (results: Member[] = everyone) => ({ status: 200, body: { results, next: null } });
const ui = (context = tenant("users.view", "users.manage")) => (
  <TenantProvider value={context}>
    <MembersList />
  </TenantProvider>
);
const row = (email: string) =>
  screen.getByText(email, { selector: ".font-medium" }).closest("li") as HTMLElement;
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => init?.method === method);
const sent = (api: ReturnType<typeof mockApi>) => calls(api, "PUT");
const body = (call?: unknown[]) => JSON.parse(String((call?.[1] as RequestInit).body));
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
const real = (children: React.ReactNode) => (
  <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
    <Providers>{children}</Providers>
  </NextIntlClientProvider>
);
// Deja en espera la siguiente respuesta de la API hasta que se llama a lo que devuelve.
function hold(api: ReturnType<typeof mockApi>) {
  const answer = api.getMockImplementation()!;
  let release = () => {};
  api.mockImplementationOnce(async (...request) => {
    await new Promise<void>((resolve) => (release = resolve));
    return answer(...request);
  });
  return () => release();
}
// Como al volver a la pestaña: la lista se vuelve a pedir.
const refresh = (view: ReturnType<typeof renderApp>) =>
  act(async () => {
    await view.client.refetchQueries();
    await new Promise((resolve) => setTimeout(resolve));
  });

async function ask(name = "Suspender a luis@acme.pe") {
  fireEvent.click(await screen.findByRole("button", { name }));
  return screen.getByRole("group", { name });
}

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con el token ya entregado: la escritura no lo pide antes
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("MemberStatusAction", () => {
  it("solo se ofrece con el permiso, a otros miembros y entre activo y suspendido", async () => {
    mockApi({ [LIST]: list() });
    const view = renderApp(ui(tenant("users.view")));
    await screen.findByRole("list", { name: "Miembros" });
    expect(screen.queryByRole("button", { name: /Suspender|Reactivar/ })).not.toBeInTheDocument();
    view.unmount();
    renderApp(ui());
    await screen.findByRole("list", { name: "Miembros" });
    const offered = screen.getAllByRole("button", { name: /Suspender|Reactivar/ });
    expect(offered.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Suspender a luis@acme.pe", // ni la fila propia, ni invitados, ni dados de baja
      "Reactivar a eva@acme.pe",
    ]);
    expect(offered.map((button) => button.textContent)).toEqual(["Suspender", "Reactivar"]);
  });

  it("pide confirmación, dice la consecuencia y cancelar no envía nada", async () => {
    const api = mockApi({ [LIST]: list() });
    renderApp(ui());
    const group = await ask();
    expect(group).toHaveTextContent(
      "¿Suspender a luis@acme.pe? Perderá el acceso a Acme SAC y se cerrarán sus sesiones.",
    );
    const cancel = within(group).getByRole("button", { name: "Cancelar" });
    expect(cancel).toHaveFocus(); // la opción que no cambia nada
    fireEvent.click(cancel);
    expect(screen.getByRole("button", { name: "Suspender a luis@acme.pe" })).toHaveFocus();
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(sent(api)).toHaveLength(0);
    const again = await ask("Reactivar a eva@acme.pe");
    expect(again).toHaveTextContent("Volverá a entrar a Acme SAC con los roles que tenía.");
  });

  it("al confirmar envía una vez, cambia la fila sin volver a pedir la lista y lo anuncia", async () => {
    const api = mockApi({
      [LIST]: list(),
      [STATUS("luis")]: { status: 200, body: { id: "luis", status: "SUSPENDED" } },
      [STATUS("eva")]: { status: 200, body: { id: "eva", status: "ACTIVE" } },
    });
    renderApp(ui());
    const group = await ask();
    expect(group).toHaveAccessibleDescription(/Perderá el acceso a Acme SAC/); // lo oye al entrar
    const release = hold(api);
    const confirm = within(group).getByRole("button", { name: "Sí, suspender" });
    confirm.focus();
    fireEvent.click(confirm);
    fireEvent.click(confirm); // ocupado: la segunda pulsación no cuenta
    await waitFor(() => expect(confirm).toHaveTextContent("Suspendiendo…"));
    expect(confirm).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" })); // ya se envió
    expect(screen.getByRole("group")).toBeInTheDocument();
    release();
    const next = await screen.findByRole("button", { name: "Reactivar a luis@acme.pe" });
    await waitFor(() => expect(next).toHaveFocus()); // el botón pulsado se fue
    expect(row("luis@acme.pe")).toHaveTextContent("Suspendido");
    expect(within(row("luis@acme.pe")).getByRole("status")).toHaveTextContent(
      "luis@acme.pe quedó suspendido.",
    );
    const [[url, init]] = sent(api) as [[string, RequestInit]];
    expect([url, init.body]).toEqual([
      "/api/v1/o/acme/members/luis/status/",
      JSON.stringify({ status: "SUSPENDED" }),
    ]);
    expect(api).toHaveBeenCalledTimes(2); // la lista, una vez; y la escritura, una vez
    const back = await ask("Reactivar a eva@acme.pe");
    fireEvent.click(within(back).getByRole("button", { name: "Sí, reactivar" }));
    await screen.findByRole("button", { name: "Suspender a eva@acme.pe" });
    expect(row("eva@acme.pe")).toHaveTextContent("Activo");
    expect(within(row("eva@acme.pe")).getByRole("status")).toHaveTextContent("quedó activo.");
    expect(body(sent(api)[1])).toEqual({ status: "ACTIVE" });
    for (const [email, text] of [
      ["ana@acme.pe", "Activo"],
      ["luis@acme.pe", "Suspendido"],
    ])
      expect(row(email!)).toHaveTextContent(text!); // solo cambia la fila de cada respuesta
  });

  it.each([
    [403, "PERMISSION_DENIED", "No tienes permiso para cambiar a este miembro.", 1],
    [409, "LAST_OWNER", "Debe quedar al menos un Owner activo en la organización.", 1],
    [400, "VALIDATION_ERROR", "Algo salió mal de nuestro lado.", 1], // no hay campos que revisar
    [500, "INTERNAL_ERROR", "Algo salió mal de nuestro lado.", 1],
    [418, "TEAPOT", "Algo salió mal de nuestro lado.", 1],
  ])("un %i %s se explica y no cambia la fila", async (status, code, text, lists) => {
    let reply = { status, body: { code } as unknown };
    const api = mockApi({ [LIST]: list(), [STATUS("luis")]: () => reply });
    renderApp(ui());
    const group = await ask();
    const confirm = within(group).getByRole("button", { name: "Sí, suspender" });
    fireEvent.click(confirm);
    expect(await within(group).findByRole("alert")).toHaveTextContent(text);
    expect(row("luis@acme.pe")).toHaveTextContent("Activo");
    expect(within(row("luis@acme.pe")).getByRole("status")).toBeEmptyDOMElement();
    await tick(); // estos errores no vuelven a pedir la lista
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(lists));
    reply = { status: 200, body: { id: "luis", status: "SUSPENDED" } };
    fireEvent.click(confirm); // el mismo botón reintenta
    await screen.findByRole("button", { name: "Reactivar a luis@acme.pe" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("sin red lo dice, y volver a abrir empieza sin el error anterior", async () => {
    const api = mockApi({ [LIST]: list() });
    renderApp(ui());
    const group = await ask();
    onlineManager.setOnline(false); // una escritura no espera en cola a que vuelva la red
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(within(group).getByRole("button", { name: "Sí, suspender" }));
    expect(await within(group).findByRole("alert")).toHaveTextContent("No hay conexión");
    expect(sent(api)).toHaveLength(1);
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    const reopened = await ask();
    expect(within(reopened).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("sin sesión no enseña un error, va al login una vez y no se puede enviar de nuevo", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/miembros", search: "", assign });
    const api = mockApi({
      [LIST]: list(),
      [STATUS("luis")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
      "POST /api/v1/auth/logout/": { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    render(
      real(
        <>
          {ui()}
          <SessionActions switcher={false} />
        </>,
      ),
    );
    const group = await ask();
    const confirm = within(group).getByRole("button", { name: "Sí, suspender" });
    fireEvent.click(confirm);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Fmiembros"));
    await tick();
    fireEvent.click(confirm); // sigue ocupado hasta que cambia la página
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    await tick();
    expect(sent(api)).toHaveLength(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(within(group).getByRole("button", { name: "Suspendiendo…" })).toBeInTheDocument();
    expect(row("luis@acme.pe")).toHaveTextContent("Activo");
    // El cierre de sesión trata su propio 401: va a `/login`, sin vuelta a esta pantalla.
    fireEvent.click(screen.getByRole("button", { name: "Cerrar sesión" }));
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login"));
    await tick();
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("sin sesión al leer la lista también va al login", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/miembros", search: "", assign });
    mockApi({ [LIST]: { status: 401, body: { code: "NOT_AUTHENTICATED" } } });
    render(real(ui()));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Fmiembros"));
  });

  it.each([
    ["INVALID_TRANSITION", 409, [member("ana"), member("luis", "DEACTIVATED"), member("eva")]],
    ["NOT_FOUND", 404, [member("ana"), member("eva")]],
  ])("un %s vuelve a pedir la lista y lo explica fuera de la fila", async (code, status, now) => {
    let rows = [member("ana"), member("luis"), member("eva")];
    const api = mockApi({
      [LIST]: () => list(rows),
      [STATUS("luis")]: { status, body: { code } },
    });
    renderApp(ui());
    const group = await ask();
    rows = now; // lo que la API tiene de verdad: la acción, o la fila, ya no existe
    fireEvent.click(within(group).getByRole("button", { name: "Sí, suspender" }));
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /luis@acme.pe/ })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "El estado de luis@acme.pe ya había cambiado. Revisa la lista.",
    );
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus(); // el foco no se pierde
    await ask("Suspender a eva@acme.pe"); // el aviso se va con la siguiente acción
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("con la pantalla desfasada la confirmación se cierra, y el foco de quien se fue no se toca", async () => {
    let rows = [member("ana"), member("luis"), member("eva", "SUSPENDED")];
    const api = mockApi({
      [LIST]: () => list(rows),
      [STATUS("luis")]: { status: 409, body: { code: "INVALID_TRANSITION" } },
      [STATUS("eva")]: { status: 200, body: { id: "eva", status: "ACTIVE" } },
    });
    renderApp(ui());
    const luis = await ask();
    const eva = await ask("Reactivar a eva@acme.pe");
    const answerEva = hold(api);
    fireEvent.click(within(eva).getByRole("button", { name: "Sí, reactivar" })); // en vuelo
    await waitFor(() => expect(sent(api)).toHaveLength(1));
    const answerLuis = hold(api);
    fireEvent.click(within(luis).getByRole("button", { name: "Sí, suspender" }));
    await waitFor(() => expect(sent(api)).toHaveLength(2));
    const elsewhere = within(eva).getByRole("button", { name: "Cancelar" });
    elsewhere.focus(); // el usuario ya está en otra fila cuando llega la negativa
    const reread = hold(api); // la lista que pide la fila desfasada queda en vuelo
    rows = [member("ana"), member("luis", "DEACTIVATED"), member("eva", "SUSPENDED")];
    answerLuis();
    expect(await screen.findByRole("alert")).toHaveTextContent("El estado de luis@acme.pe");
    expect(screen.getAllByRole("group")).toHaveLength(1); // la de luis se cerró sin esperar
    expect(screen.getByRole("button", { name: "Suspender a luis@acme.pe" })).toBeInTheDocument();
    expect(elsewhere).toHaveFocus();
    // El éxito de otra fila cancela esa lectura (traería a eva como antes) y la vuelve a pedir.
    rows = [member("ana"), member("luis", "DEACTIVATED"), member("eva")]; // la API ya la reactivó
    answerEva();
    await screen.findByRole("button", { name: "Suspender a eva@acme.pe" });
    reread();
    await waitFor(() => expect(row("luis@acme.pe")).toHaveTextContent("Desactivado"));
    expect(calls(api, "GET")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: /luis@acme.pe/ })).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeVisible();
  });

  it("una lectura en vuelo no pisa la fila que acaba de cambiar", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [member("ana"), member("luis")], next: "abc" } },
      [`${LIST}?cursor=abc`]: { status: 200, body: { results: [member("rosa")], next: null } },
      [STATUS("luis")]: { status: 200, body: { id: "luis", status: "SUSPENDED" } },
    });
    renderApp(ui());
    const group = await ask();
    const release = hold(api);
    const more = screen.getByRole("button", { name: "Cargar más" });
    fireEvent.click(more); // sale con la lista de antes de la escritura
    fireEvent.click(within(group).getByRole("button", { name: "Sí, suspender" }));
    await screen.findByRole("button", { name: "Reactivar a luis@acme.pe" });
    release();
    await tick();
    expect(row("luis@acme.pe")).toHaveTextContent("Suspendido");
    expect(row("ana@acme.pe")).toHaveTextContent("Activo");
    // Esa lectura se canceló: el botón vuelve a estar libre y la página se pide otra vez.
    await waitFor(() => expect(more).toHaveAttribute("aria-disabled", "false"));
    fireEvent.click(more);
    expect(await screen.findByText("rosa@acme.pe", { selector: ".font-medium" })).toBeVisible();
    expect(row("luis@acme.pe")).toHaveTextContent("Suspendido");
    expect(screen.queryByRole("button", { name: "Cargar más" })).not.toBeInTheDocument();
  });

  it("lo que se confirma queda fijado al abrir, aunque la lista cambie debajo", async () => {
    let luis: Member["status"] = "ACTIVE";
    const api = mockApi({
      [LIST]: () => list([member("ana"), member("luis", luis), member("eva")]),
      [STATUS("luis")]: { status: 200, body: { id: "luis", status: "SUSPENDED" } },
    });
    const view = renderApp(ui());
    await ask();
    luis = "SUSPENDED"; // otro administrador lo suspendió con la confirmación abierta
    await refresh(view);
    expect(screen.queryByRole("group")).not.toBeInTheDocument(); // nada que confirmar ya
    expect(within(row("luis@acme.pe")).getByRole("status")).toHaveTextContent("quedó suspendido.");
    expect(sent(api)).toHaveLength(0);
    luis = "ACTIVE";
    await refresh(view);
    const group = await ask();
    const release = hold(api);
    const confirm = within(group).getByRole("button", { name: "Sí, suspender" });
    fireEvent.click(confirm);
    await waitFor(() => expect(confirm).toHaveTextContent("Suspendiendo…"));
    luis = "SUSPENDED"; // y ahora, con la escritura en vuelo
    const other = screen.getByRole("button", { name: "Suspender a eva@acme.pe" });
    other.focus();
    await refresh(view);
    expect(row("luis@acme.pe")).toHaveTextContent("Suspendido"); // la lista ya lo dice
    expect(confirm).toHaveTextContent("Suspendiendo…"); // y la pregunta no se da la vuelta
    expect(screen.getByRole("group")).toHaveAccessibleName("Suspender a luis@acme.pe");
    expect(screen.getByRole("group")).toHaveAccessibleDescription(/¿Suspender a luis@acme.pe/);
    release();
    await screen.findByRole("button", { name: "Reactivar a luis@acme.pe" });
    await tick();
    expect(body(sent(api)[0])).toEqual({ status: "SUSPENDED" });
    expect(within(row("luis@acme.pe")).getByRole("status")).toHaveTextContent("quedó suspendido.");
    expect(other).toHaveFocus(); // el usuario ya estaba en otra parte: el foco no se le quita
  });

  it("una pulsación entre la respuesta y el render no reenvía, ni la suelta un render ajeno", async () => {
    let reply: { status: number; body: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [member("ana"), member("luis")], next: "abc" } },
      [`${LIST}?cursor=abc`]: list([member("eva")]),
      [STATUS("luis")]: () => reply,
    });
    renderApp(ui());
    const group = await ask();
    const confirm = within(group).getByRole("button", { name: "Sí, suspender" });
    const release = hold(api);
    fireEvent.click(confirm);
    await tick();
    await act(async () => {
      release();
      for (let turn = 0; turn < 100; turn++) await null; // llega el error; aún no hay render
      fireEvent.click(confirm); // una pulsación entre la respuesta y el render
    });
    await tick();
    expect(sent(api)).toHaveLength(1);
    expect(within(group).getByRole("alert")).toBeVisible(); // y el mismo botón reintenta después
    // Un render que no viene de un evento (llega la página 2) justo antes de pulsar.
    reply = { status: 200, body: { id: "luis", status: "SUSPENDED" } };
    const more = hold(api);
    hold(api); // la escritura no llega a responder
    const rows = screen.getByRole("list", { name: "Miembros" });
    const watch = new MutationObserver(() => confirm.click()); // pulsa tras ese render
    watch.observe(rows, { childList: true });
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    await tick();
    more();
    await waitFor(() => expect(sent(api)).toHaveLength(2));
    watch.disconnect();
    fireEvent.click(confirm); // otra pulsación con la escritura en vuelo
    await tick();
    expect(sent(api)).toHaveLength(2);
  });

  it("la fila enseña lo que respondió la API, y una respuesta de otro miembro no se aplica", async () => {
    let reply: unknown = { id: "luis", status: "ACTIVE" };
    const api = mockApi({ [LIST]: list(), [STATUS("luis")]: () => ({ status: 200, body: reply }) });
    renderApp(ui());
    fireEvent.click(within(await ask()).getByRole("button", { name: "Sí, suspender" }));
    const status = () => within(row("luis@acme.pe")).getByRole("status");
    await waitFor(() => expect(status()).toHaveTextContent("luis@acme.pe quedó activo."));
    expect(row("luis@acme.pe")).toHaveTextContent("Activo");
    reply = { id: "eva", status: "SUSPENDED" };
    fireEvent.click(within(await ask()).getByRole("button", { name: "Sí, suspender" }));
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2)); // se pregunta a la API
    expect(status()).toBeEmptyDOMElement();
    expect(row("eva@acme.pe")).toHaveTextContent("Suspendido"); // el de la lista, no el recibido
  });
});
