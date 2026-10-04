import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Member, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { MembersList } from "./members-list";

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
const sent = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.filter(([, init]) => init?.method === "PUT");

async function ask(name = "Suspender a luis@acme.pe") {
  fireEvent.click(await screen.findByRole("button", { name }));
  return screen.getByRole("group", { name });
}

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con el token ya entregado: la escritura no lo pide antes
});
afterEach(() => vi.unstubAllGlobals());

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
    let release = () => {};
    const api = mockApi({
      [LIST]: list(),
      [STATUS("luis")]: { status: 200, body: { id: "luis", status: "SUSPENDED" } },
      [STATUS("eva")]: { status: 200, body: { id: "eva", status: "ACTIVE" } },
    });
    renderApp(ui());
    const group = await ask();
    const answer = api.getMockImplementation()!;
    api.mockImplementationOnce(async (...request) => {
      await new Promise<void>((resolve) => (release = resolve));
      return answer(...request);
    });
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
    expect(JSON.parse(String(sent(api)[1]?.[1]?.body))).toEqual({ status: "ACTIVE" });
  });

  it.each([
    [403, "PERMISSION_DENIED", "No tienes permiso para cambiar a este miembro.", 1],
    [409, "LAST_OWNER", "Debe quedar al menos un Owner activo en la organización.", 1],
    [409, "INVALID_TRANSITION", "El estado de este miembro ya había cambiado.", 2],
    [404, "NOT_FOUND", "No encontramos lo que buscas.", 2],
    [500, "INTERNAL_ERROR", "Algo salió mal de nuestro lado.", 1],
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
    // Solo si la pantalla ya no refleja a la API se vuelve a pedir la lista.
    await waitFor(() =>
      expect(api.mock.calls.filter(([, init]) => init?.method === "GET")).toHaveLength(lists),
    );
    reply = { status: 200, body: { id: "luis", status: "SUSPENDED" } };
    fireEvent.click(confirm); // el mismo botón reintenta
    await screen.findByRole("button", { name: "Reactivar a luis@acme.pe" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("sin red lo dice, y volver a abrir empieza sin el error anterior", async () => {
    const api = mockApi({ [LIST]: list() });
    renderApp(ui());
    const group = await ask();
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(within(group).getByRole("button", { name: "Sí, suspender" }));
    expect(await within(group).findByRole("alert")).toHaveTextContent("No hay conexión");
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    const reopened = await ask();
    expect(within(reopened).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("sin sesión no enseña un error y va al login con vuelta a la pantalla", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/miembros", search: "", assign });
    mockApi({
      [LIST]: list(),
      [STATUS("luis")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const group = await ask();
    fireEvent.click(within(group).getByRole("button", { name: "Sí, suspender" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Fmiembros"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(within(group).getByRole("button", { name: "Suspendiendo…" })).toBeInTheDocument();
    expect(row("luis@acme.pe")).toHaveTextContent("Activo");
  });
});
