import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Invitation, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { InvitationsList } from "./invitations-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const LIST = "GET /api/v1/o/acme/invitations/";
const STATUS = (id: string) => `PUT /api/v1/o/acme/invitations/${id}/status/`;
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
// Identificadores con prefijo propio: ninguno debe llegar al DOM.
const invitation = (name: string, status = "PENDING"): Invitation =>
  ({
    id: `fila-${name}`,
    email: `${name}@cliente.pe`,
    role_ids: ["rol-ventas"],
    status,
    expires_at: "2026-10-14T15:30:00Z",
    invited_by: "cuenta-ana",
    created_at: "2026-10-07T15:30:00Z",
  }) as Invitation;
const all = [
  invitation("lima"),
  invitation("tarde", "EXPIRED"),
  invitation("fuera", "REVOKED"),
  invitation("dentro", "ACCEPTED"),
];
const list = (results: Invitation[] = all) => ({ status: 200, body: { results, next: null } });
const revoked = (name: string) => ({ status: 200, body: invitation(name, "REVOKED") });
const ui = (context = tenant("users.invite", "users.manage")) => (
  <TenantProvider value={context}>
    <InvitationsList />
  </TenantProvider>
);
const card = (name: string) => screen.getByText(`${name}@cliente.pe`).closest("li") as HTMLElement;
// «Pendiente», «Revocada»…: el estado que enseña la fila.
const shown = (name: string) =>
  [...card(name).querySelectorAll("p > span.font-medium")][1]!.textContent; // tras el correo
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const sent = (api: ReturnType<typeof mockApi>) => calls(api, "PUT");
const body = (call?: unknown[]) => JSON.parse(String((call?.[1] as RequestInit).body));
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
const heading = () => screen.getByRole("heading", { level: 1 });
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
async function ask(name = "Revocar la invitación de lima@cliente.pe") {
  fireEvent.click(await screen.findByRole("button", { name }));
  return screen.getByRole("group", { name });
}
const confirm = (group: HTMLElement) => within(group).getByRole("button", { name: "Sí, revocar" });

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con el token ya entregado: la escritura no lo pide antes
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("InvitationRevokeAction", () => {
  it("solo se ofrece con los dos permisos, y en las filas pendientes o caducadas", async () => {
    for (const codes of [["users.invite"], ["users.manage"], ["roles.view", "users.view"]]) {
      mockApi({ [LIST]: list() });
      const view = renderApp(ui(tenant(...codes)));
      await screen.findByText("4 invitaciones en la lista");
      expect(screen.queryByRole("button", { name: /Revocar/ })).toBeNull();
      view.unmount();
    }
    mockApi({ [LIST]: list() });
    renderApp(ui());
    await screen.findByText("4 invitaciones en la lista");
    const offered = screen.getAllByRole("button", { name: /^Revocar la invitación de/ });
    expect(offered.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Revocar la invitación de lima@cliente.pe",
      "Revocar la invitación de tarde@cliente.pe", // caducada: sigue ocupando su correo
    ]);
    for (const button of offered) expect(button).toHaveTextContent(/^Revocar$/);
    const dom = new XMLSerializer().serializeToString(document.body);
    for (const hidden of ["fila-", "rol-", "cuenta-"]) expect(dom).not.toContain(hidden);
  });

  it("pide confirmación, dice la consecuencia y cancelar no envía nada", async () => {
    const api = mockApi({ [LIST]: list() });
    renderApp(ui());
    const trigger = await screen.findByRole("button", {
      name: "Revocar la invitación de lima@cliente.pe",
    });
    trigger.focus();
    const group = await ask();
    expect(group).toHaveAccessibleDescription(
      "¿Revocar la invitación de lima@cliente.pe? Dejará de poder aceptarse y ese correo se podrá invitar otra vez. No se puede deshacer.",
    );
    const cancel = within(group).getByRole("button", { name: "Cancelar" });
    await waitFor(() => expect(cancel).toHaveFocus()); // la opción que no cambia nada
    expect(screen.getAllByRole("group")).toHaveLength(1); // las demás filas, como estaban
    fireEvent.click(cancel);
    expect(screen.queryByRole("group")).toBeNull();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Revocar la invitación de lima@cliente.pe" }),
      ).toHaveFocus(),
    );
    expect(sent(api)).toHaveLength(0);
    expect(shown("lima")).toBe("Pendiente");
  });

  it("al confirmar envía una vez, cambia la fila sin volver a pedir la lista y lo anuncia la lista", async () => {
    const api = mockApi({ [LIST]: list(), [STATUS("fila-lima")]: revoked("lima") });
    renderApp(ui());
    const group = await ask();
    const release = hold(api);
    const button = confirm(group);
    button.focus();
    fireEvent.click(button);
    fireEvent.click(button); // dos pulsaciones más mientras se envía: una sola petición
    fireEvent.click(button);
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" })); // ni se cierra
    await tick();
    const busy = within(group).getByRole("button", { name: "Revocando…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    expect(busy).toHaveFocus(); // `aria-disabled` conserva el foco
    release();
    await waitFor(() => expect(shown("lima")).toBe("Revocada"));
    expect(sent(api)).toHaveLength(1);
    expect(body(sent(api)[0])).toEqual({ status: "REVOKED" });
    expect(calls(api, "GET")).toHaveLength(1); // la lista no se vuelve a pedir
    expect(within(card("lima")).queryByRole("button")).toBeNull(); // ya no hay qué revocar
    expect(screen.queryByRole("group")).toBeNull();
    const done = screen.getByText(
      "La invitación de lima@cliente.pe quedó revocada. Ese correo se puede invitar otra vez.",
    );
    expect(done).toHaveAttribute("role", "status");
    expect(done).not.toHaveClass("sr-only"); // se ve
    await waitFor(() => expect(heading()).toHaveFocus()); // los controles de la fila se fueron
    expect(shown("tarde")).toBe("Caducada"); // las demás filas, intactas
    await ask("Revocar la invitación de tarde@cliente.pe"); // la siguiente acción retira el anuncio
    expect(done).toHaveTextContent("");
    expect(done).toHaveClass("sr-only");
  });

  it("la fila enseña lo que respondió la API, y la respuesta de otra invitación no se aplica", async () => {
    let rows = all;
    const api = mockApi({
      [LIST]: () => list(rows),
      [STATUS("fila-lima")]: { status: 200, body: invitation("lima", "ACCEPTED") },
      [STATUS("fila-tarde")]: revoked("otra"),
    });
    renderApp(ui());
    fireEvent.click(confirm(await ask()));
    await waitFor(() => expect(shown("lima")).toBe("Aceptada")); // no lo que se pidió
    rows = [all[0]!, invitation("tarde", "REVOKED"), all[2]!, all[3]!];
    fireEvent.click(confirm(await ask("Revocar la invitación de tarde@cliente.pe")));
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2)); // se pregunta a la API
    await waitFor(() => expect(shown("tarde")).toBe("Revocada"));
  });

  it("sin permiso para esa invitación lo dice en la confirmación, y se puede reintentar", async () => {
    const api = mockApi({
      [LIST]: list(),
      [STATUS("fila-lima")]: { status: 403, body: { code: "PERMISSION_DENIED" } },
    });
    renderApp(ui());
    const group = await ask();
    fireEvent.click(confirm(group));
    expect(await within(group).findByRole("alert")).toHaveTextContent(
      "No tienes permiso para revocar esta invitación.",
    );
    expect(shown("lima")).toBe("Pendiente");
    await tick();
    fireEvent.click(confirm(group));
    await waitFor(() => expect(sent(api)).toHaveLength(2));
    expect(calls(api, "GET")).toHaveLength(1);
  });

  it("sin red lo dice, y volver a abrir empieza sin el error anterior", async () => {
    const api = mockApi({ [LIST]: list() });
    renderApp(ui());
    const group = await ask();
    onlineManager.setOnline(false); // una escritura no espera en cola a que vuelva la red
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(confirm(group));
    expect(await within(group).findByRole("alert")).toHaveTextContent("No hay conexión");
    expect(sent(api)).toHaveLength(1);
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    const reopened = await ask();
    expect(within(reopened).queryByRole("alert")).toBeNull();
  });

  it("sin sesión no enseña un error, va al login una vez y no se puede enviar de nuevo", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/invitaciones", search: "", assign });
    const api = mockApi({
      [LIST]: list(),
      [STATUS("fila-lima")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const group = await ask();
    const button = confirm(group);
    fireEvent.click(button);
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Finvitaciones"),
    );
    await tick();
    fireEvent.click(button); // sigue ocupado hasta que cambia la página
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    await tick();
    expect(sent(api)).toHaveLength(1);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(within(group).getByRole("button", { name: "Revocando…" })).toBeInTheDocument();
    expect(shown("lima")).toBe("Pendiente");
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it.each([
    [
      { status: 404, body: { code: "NOT_FOUND" } },
      "La invitación de lima@cliente.pe ya no está disponible. Revisa la lista.",
    ],
    [
      { status: 409, body: { code: "INVALID_TRANSITION" } },
      "La invitación de lima@cliente.pe ya no estaba pendiente. Revisa la lista.",
    ],
  ])("un %j vuelve a pedir la lista y lo explica fuera de la fila", async (answer, text) => {
    let rows = all;
    const api = mockApi({ [LIST]: () => list(rows), [STATUS("fila-lima")]: answer });
    renderApp(ui());
    const group = await ask();
    rows = [invitation("lima", "ACCEPTED"), ...all.slice(1)]; // lo que la API tiene de verdad
    fireEvent.click(confirm(group));
    (document.activeElement as HTMLElement).blur(); // el foco, en ninguna parte
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    await waitFor(() => expect(shown("lima")).toBe("Aceptada"));
    expect(screen.getByRole("alert")).toHaveTextContent(text);
    expect(screen.queryByRole("group")).toBeNull(); // la confirmación se cerró
    expect(heading()).toHaveFocus(); // el foco no se pierde
    expect(sent(api)).toHaveLength(1);
    await ask("Revocar la invitación de tarde@cliente.pe"); // el aviso se va con la siguiente acción
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("con la pantalla desfasada, el foco de quien ya se fue a otra parte no se toca", async () => {
    const api = mockApi({
      [LIST]: list(),
      [STATUS("fila-lima")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const group = await ask();
    const release = hold(api);
    fireEvent.click(confirm(group));
    await waitFor(() => expect(sent(api)).toHaveLength(1));
    const elsewhere = screen.getByRole("button", {
      name: "Revocar la invitación de tarde@cliente.pe",
    });
    elsewhere.focus(); // el usuario ya está en otra fila cuando llega la negativa
    release();
    expect(await screen.findByRole("alert")).toHaveTextContent("ya no está disponible");
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    await tick();
    expect(screen.queryByRole("group")).toBeNull();
    expect(elsewhere).toHaveFocus();
  });

  it("si la fila deja de estar pendiente con la confirmación abierta, se cierra y el foco va al título", async () => {
    let rows = all;
    const api = mockApi({ [LIST]: () => list(rows) });
    const view = renderApp(ui());
    const group = await ask();
    await waitFor(() =>
      expect(within(group).getByRole("button", { name: "Cancelar" })).toHaveFocus(),
    );
    rows = [invitation("lima", "REVOKED"), ...all.slice(1)]; // otro la revocó entretanto
    await refresh(view);
    await waitFor(() => expect(shown("lima")).toBe("Revocada"));
    expect(screen.queryByRole("group")).toBeNull();
    expect(within(card("lima")).queryByRole("button")).toBeNull();
    await waitFor(() => expect(heading()).toHaveFocus());
    expect(sent(api)).toHaveLength(0);
    expect(screen.queryByRole("alert")).toBeNull(); // no es un error: la fila ya lo dice
  });

  it("una lectura en vuelo no pisa la fila que acaba de cambiar", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [all[0]!, all[1]!], next: "abc" } },
      [`${LIST}?cursor=abc`]: list([invitation("tacna")]),
      [STATUS("fila-lima")]: revoked("lima"),
    });
    renderApp(ui());
    const group = await ask();
    const release = hold(api);
    const more = screen.getByRole("button", { name: "Cargar más" });
    fireEvent.click(more); // sale con la lista de antes de la escritura
    fireEvent.click(confirm(group));
    await waitFor(() => expect(shown("lima")).toBe("Revocada"));
    release();
    await tick();
    expect([shown("lima"), shown("tarde")]).toEqual(["Revocada", "Caducada"]);
    // Esa lectura se canceló: el botón vuelve a estar libre y la página se pide otra vez.
    await waitFor(() => expect(more).toHaveAttribute("aria-disabled", "false"));
    fireEvent.click(more);
    expect(await screen.findByText("tacna@cliente.pe")).toBeVisible();
    expect(shown("lima")).toBe("Revocada");
  });

  it("una relectura entera en vuelo se cancela y se repite tras el cambio", async () => {
    let rows = all;
    const api = mockApi({ [LIST]: () => list(rows), [STATUS("fila-lima")]: revoked("lima") });
    const view = renderApp(ui());
    const group = await ask();
    let releaseRead = () => {};
    api.mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => (releaseRead = resolve));
      return new Response(JSON.stringify({ results: all, next: null }), { status: 200 });
    });
    void view.client.refetchQueries();
    await tick();
    rows = [invitation("lima", "REVOKED"), ...all.slice(1)]; // lo que la API tiene después
    fireEvent.click(confirm(group));
    await waitFor(() => expect(shown("lima")).toBe("Revocada"));
    releaseRead(); // llega tarde: no devuelve «Pendiente» a la fila
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(3)); // y la lectura se repite
    await tick();
    expect(shown("lima")).toBe("Revocada");
  });

  it("Enter mantenido no vuelve a pulsar: ni reabre la confirmación ni reenvía", async () => {
    const api = mockApi({
      [LIST]: list(),
      [STATUS("fila-lima")]: { status: 403, body: { code: "PERMISSION_DENIED" } },
    });
    renderApp(ui());
    const group = await ask();
    fireEvent.click(confirm(group));
    await within(group).findByRole("alert");
    expect(fireEvent.keyDown(confirm(group), { key: "Enter", repeat: true })).toBe(false);
    expect(fireEvent.keyDown(confirm(group), { key: "Enter" })).toBe(true); // una pulsación, sí
    expect(sent(api)).toHaveLength(1);
  });
});
