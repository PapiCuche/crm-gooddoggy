import { focusManager, onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Branch, Member, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { MembersList } from "./members-list";

const LIST = "GET /api/v1/o/acme/members/";
const BRANCHES = "GET /api/v1/o/acme/branches/?limit=200";
const PUT = "PUT /api/v1/o/acme/members/luis/branch/";
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "ana",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const branch = (id: string, name: string, is_active = true): Branch => ({
  id,
  code: `C-${id}`, // distinto del id: la ruta pide el id
  name,
  address: "",
  district: "",
  city: "",
  phone: "",
  timezone: "America/Lima",
  is_active,
});
const lima = branch("b1", "Lima Centro");
const cusco = branch("b2", "Cusco", false);
const ref = ({ id, code, name }: Branch) => ({ id, code, name });
const member = (id: string, at: Branch | null = null, extra: Partial<Member> = {}): Member => ({
  id,
  status: "ACTIVE",
  joined_at: "2026-10-04T15:49:34Z",
  default_branch: at ? ref(at) : null,
  user: { id: `u-${id}`, email: `${id}@acme.pe`, first_name: "", last_name: "" },
  roles: [],
  ...extra,
});
const list = (results: Member[]) => ({ status: 200, body: { results, next: null } });
const eva = () => member("eva", null, { status: "INVITED" });
const everyone = () => list([member("ana"), member("luis"), eva()]);
const all = { status: 200, body: { results: [lima, cusco], next: null } };
const saved = (at: Branch | null, id = "luis") => ({
  status: 200,
  body: { id, default_branch: at ? ref(at) : null },
});
const ui = (context = tenant("users.view", "users.manage", "organization.view")) => (
  <TenantProvider value={context}>
    <MembersList />
  </TenantProvider>
);
const row = (email: string) =>
  screen.getByText(email, { selector: ".font-medium" }).closest("li") as HTMLElement;
// La línea de la tarjeta, bajo los roles: no el anuncio ni un aviso, que también la nombran.
const shown = (email: string) =>
  within(row(email)).getByRole("list", { name: "Roles" }).nextElementSibling as HTMLElement;
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => init?.method === method).map(([url]) => String(url));
const sent = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.filter(([, init]) => init?.method === "PUT").map(([, init]) => init?.body);
const reads = (api: ReturnType<typeof mockApi>, part: string) =>
  calls(api, "GET").filter((url) => url.includes(part));
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
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
const TRIGGER = "Cambiar la sucursal de luis@acme.pe";
async function panel(name = "luis@acme.pe") {
  fireEvent.click(await screen.findByRole("button", { name: `Cambiar la sucursal de ${name}` }));
  const group = screen.getByRole("group", { name: `Sucursal de ${name}` });
  const select = (await within(group).findByRole("combobox")) as HTMLSelectElement;
  const save = within(group).getByRole("button", { name: "Guardar" });
  const cancel = within(group).getByRole("button", { name: "Cancelar" });
  const choose = (value: string) => fireEvent.change(select, { target: { value } });
  return { group, select, save, cancel, choose };
}

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con el token ya entregado: la escritura no lo pide antes
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
  focusManager.setFocused(undefined);
});

describe("MemberBranchAction", () => {
  it("se ofrece con los dos permisos, a otros miembros, y no pide nada hasta abrirse", async () => {
    const named = member("luis", cusco, {
      user: { id: "u-luis", email: "luis@acme.pe", first_name: "Luis", last_name: "Paz" },
    });
    const api = mockApi({ [LIST]: list([member("ana"), named, eva()]), [BRANCHES]: all });
    for (const lacking of ["users.manage", "organization.view"]) {
      const view = renderApp(ui(tenant("users.view", lacking, "roles.view")));
      await screen.findByRole("list", { name: "Miembros" });
      expect(screen.queryByRole("button", { name: /Cambiar la sucursal/ })).toBeNull();
      view.unmount();
    }
    renderApp(ui());
    const offered = await screen.findAllByRole("button", { name: /Cambiar la sucursal/ });
    expect(offered.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Cambiar la sucursal de Luis Paz (luis@acme.pe)", // con el correo: los nombres se repiten
      "Cambiar la sucursal de eva@acme.pe", // ni la fila propia; el estado del miembro no importa
    ]);
    expect(offered[0]).toHaveTextContent(/^Sucursal$/);
    expect(reads(api, "/branches/")).toEqual([]);
    const { group, select, cancel } = await panel("Luis Paz (luis@acme.pe)");
    expect(cancel).toHaveFocus(); // lo que no cambia nada
    expect(within(group).getByLabelText("Sucursal de Luis Paz (luis@acme.pe)")).toBe(select);
    expect([...select.options].map((option) => [option.value, option.text])).toEqual([
      ["", "Sin sucursal"],
      ["b1", "Lima Centro (C-b1)"],
      ["b2", "Cusco (C-b2), inactiva"], // se puede elegir: la API la admite
    ]);
    expect(select).toHaveValue("b2"); // la que tiene
    expect(select).toHaveClass("text-[16px]", "h-11");
    fireEvent.click(cancel);
    expect(offered[0]).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Cambiar la sucursal de Luis/ })).toHaveFocus();
    expect(calls(api, "PUT")).toEqual([]); // abrir y cancelar no envían nada
  });

  it("guarda una vez lo elegido, cambia la fila sin volver a pedir la lista y lo anuncia", async () => {
    let reply = saved(lima);
    const api = mockApi({ [LIST]: everyone(), [BRANCHES]: all, [PUT]: () => reply });
    renderApp(ui());
    const { group, select, save, cancel } = await panel();
    fireEvent.click(save); // sin cambiar nada: se cierra y no envía
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(calls(api, "PUT")).toEqual([]);
    const again = await panel();
    again.choose("b2"); // se pide Cusco y la API responde Lima Centro: vale lo que respondió
    const release = hold(api);
    again.save.focus();
    fireEvent.click(again.save);
    fireEvent.click(again.save); // ocupado: la segunda pulsación no cuenta
    await waitFor(() => expect(again.save).toHaveTextContent("Guardando…"));
    for (const control of [again.save, again.cancel, again.select])
      expect(control).toHaveAttribute("aria-disabled", "true");
    again.choose(""); // mientras se envía, lo elegido no cambia
    expect(again.select).toHaveValue("b2");
    fireEvent.click(again.cancel); // ya se envió
    expect(screen.getByRole("group")).toBeInTheDocument();
    expect(again.save).toHaveFocus(); // `aria-disabled`, no `disabled`
    release();
    await waitFor(() => expect(shown("luis@acme.pe")).toHaveTextContent(/^Sucursal: Lima Centro/));
    expect(shown("luis@acme.pe")).toHaveTextContent("Sucursal: Lima Centro (C-b1)");
    expect(screen.queryByRole("group")).not.toBeInTheDocument(); // el panel se cierra
    expect(screen.getByRole("button", { name: TRIGGER })).toHaveFocus();
    const status = () => within(row("luis@acme.pe")).getAllByRole("status").at(-1);
    expect(status()).toHaveTextContent(
      /^La sucursal de luis@acme.pe ahora es Lima Centro \(C-b1\)\.$/,
    );
    expect(sent(api)).toEqual([JSON.stringify({ branch_id: "b2" })]);
    expect(reads(api, "/members/")).toHaveLength(1);
    expect(shown("eva@acme.pe")).toHaveTextContent(/^Sin sucursal$/); // solo la fila de la respuesta
    reply = saved(null);
    const third = await panel(); // la fila guardó la que respondió la API: al reabrir es la elegida
    expect(status()).toHaveTextContent(/^$/); // el anuncio anterior se va al abrir
    expect(third.select).toHaveValue("b1");
    third.choose("");
    fireEvent.click(third.save);
    await waitFor(() => expect(shown("luis@acme.pe")).toHaveTextContent(/^Sin sucursal$/));
    expect(status()).toHaveTextContent(/^luis@acme.pe quedó sin sucursal\.$/);
    expect(sent(api).at(-1)).toBe(JSON.stringify({ branch_id: null }));
    expect(calls(api, "PUT")).toHaveLength(2);
    for (const gone of [group, select, save, cancel]) expect(gone).not.toBeInTheDocument();
  });

  it.each([
    [403, { code: "PERMISSION_DENIED" }, "No tienes permiso para cambiar la sucursal de este"],
    [409, { code: "LAST_OWNER" }, "Debe quedar al menos un Owner activo en la organización."],
    [500, { code: "INTERNAL_ERROR" }, "Algo salió mal de nuestro lado."],
    // Las sucursales salen de la API y no se borran: un 400 es un fallo nuestro, no del usuario.
    [400, { code: "VALIDATION_ERROR", fields: { branch_id: ["x"] } }, "Algo salió mal de nuestro"],
  ])(
    "un %i se explica, no cambia la fila y el mismo botón reintenta",
    async (status, body, text) => {
      let reply: { status: number; body: unknown } = { status, body };
      const api = mockApi({ [LIST]: everyone(), [BRANCHES]: all, [PUT]: () => reply });
      renderApp(ui());
      const { group, select, save, choose } = await panel();
      choose("b1");
      fireEvent.click(save);
      expect(await within(group).findByRole("alert")).toHaveTextContent(text);
      expect(shown("luis@acme.pe")).toHaveTextContent(/^Sin sucursal$/);
      expect(select).toHaveValue("b1"); // lo elegido sigue ahí
      await tick(); // estos errores no vuelven a pedir nada
      expect(reads(api, "/members/")).toHaveLength(1);
      expect(reads(api, "/branches/")).toHaveLength(1);
      reply = saved(lima);
      fireEvent.click(save);
      await waitFor(() => expect(shown("luis@acme.pe")).toHaveTextContent("Lima Centro"));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    },
  );

  it("con la pantalla desfasada lo explica la lista, que se vuelve a pedir", async () => {
    const api = mockApi({
      [LIST]: everyone(),
      [BRANCHES]: all,
      [PUT]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const { save, choose } = await panel();
    choose("b1");
    fireEvent.click(save);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /^No se pudo cambiar la sucursal de luis@acme.pe: ya no está disponible. Revisa la lista.$/,
    );
    expect(screen.queryByRole("group")).not.toBeInTheDocument(); // el panel se cierra
    await waitFor(() => expect(reads(api, "/members/")).toHaveLength(2));
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();
    await panel("eva@acme.pe"); // el aviso se va con la siguiente acción
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lo elegido queda fijado al abrir, aunque la lista cambie debajo", async () => {
    let luis = member("luis");
    const api = mockApi({
      [LIST]: () => list([member("ana"), luis]),
      [BRANCHES]: { status: 200, body: { results: [lima], next: null } },
      [PUT]: saved(null, "eva"), // y la API responde por otra membresía: no se aplica
    });
    const view = renderApp(ui());
    const { select, save } = await panel();
    luis = member("luis", cusco); // otro administrador se la asignó con el panel abierto
    await act(async () => {
      await view.client.refetchQueries({ queryKey: ["/api/v1/o/acme/members/", "pages"] });
      await new Promise((resolve) => setTimeout(resolve));
    });
    expect(shown("luis@acme.pe")).toHaveTextContent("Sucursal: Cusco (C-b2)"); // la fila ya lo dice
    expect(select).toHaveValue(""); // el selector no cambió bajo el dedo
    expect([...select.options].map((option) => option.value)).toEqual(["", "b1"]);
    fireEvent.click(save); // sigue sin cambios respecto a lo que leyó: no envía
    expect(calls(api, "PUT")).toEqual([]);
    const again = await panel(); // al reabrir, la que tiene figura aunque la lectura no la traiga
    expect([...again.select.options].map((option) => option.text)).toEqual([
      "Sin sucursal",
      "Lima Centro (C-b1)",
      "Cusco (C-b2)",
    ]);
    expect(again.select).toHaveValue("b2");
    again.choose("");
    fireEvent.click(again.save);
    await waitFor(() => expect(reads(api, "/members/")).toHaveLength(3)); // se vuelve a pedir
    expect(shown("luis@acme.pe")).toHaveTextContent("Sucursal: Cusco (C-b2)");
  });

  it("una lectura en vuelo no pisa la fila que acaba de cambiar", async () => {
    let luis = member("luis");
    const api = mockApi({
      [LIST]: () => ({ status: 200, body: { results: [member("ana"), luis], next: "abc" } }),
      [`${LIST}?cursor=abc`]: list([eva()]),
      [BRANCHES]: all,
      [PUT]: saved(lima),
    });
    const view = renderApp(ui());
    const first = await panel();
    const more = screen.getByRole("button", { name: "Cargar más" });
    let release = hold(api); // «Cargar más» sale ahora, con la lista de antes de la escritura
    fireEvent.click(more);
    await waitFor(() => expect(reads(api, "/members/")).toHaveLength(2));
    first.choose("b1");
    fireEvent.click(first.save);
    await waitFor(() => expect(shown("luis@acme.pe")).toHaveTextContent("Lima Centro"));
    release();
    await tick();
    expect(shown("luis@acme.pe")).toHaveTextContent("Lima Centro"); // esa lectura se canceló
    await waitFor(() => expect(more).toHaveAttribute("aria-disabled", "false"));
    // Si lo que estaba en vuelo era la lista entera, se repite después de la escritura.
    const second = await panel();
    release = hold(api);
    act(() => void view.client.refetchQueries({ queryKey: ["/api/v1/o/acme/members/", "pages"] }));
    await waitFor(() => expect(reads(api, "/members/")).toHaveLength(3));
    luis = member("luis", lima); // lo que la API tendrá tras la escritura
    second.choose("b2");
    fireEvent.click(second.save);
    await waitFor(() => expect(screen.queryByRole("group")).not.toBeInTheDocument());
    release();
    await waitFor(() => expect(reads(api, "/members/")).toHaveLength(4));
  });

  it("una pulsación entre la respuesta y el render no reenvía, ni una tecla mantenida reabre", async () => {
    const api = mockApi({ [LIST]: everyone(), [BRANCHES]: all, [PUT]: saved(lima) });
    renderApp(ui());
    const { save, choose } = await panel();
    expect(fireEvent.keyDown(save, { key: "Enter", repeat: true })).toBe(false);
    expect(fireEvent.keyDown(save, { key: "Enter" })).toBe(true); // la primera pulsación sí activa
    expect(fireEvent.keyDown(save, { key: "Tab", repeat: true })).toBe(true); // y Tab navega
    choose("b1");
    const release = hold(api);
    fireEvent.click(save);
    await tick();
    await act(async () => {
      release();
      for (let turn = 0; turn < 100; turn++) await null; // llega la respuesta; aún no hay render
      fireEvent.click(save); // una pulsación entre la respuesta y el render
    });
    await tick();
    expect(calls(api, "PUT")).toHaveLength(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    const trigger = screen.getByRole("button", { name: TRIGGER });
    expect(fireEvent.keyDown(trigger, { key: "Enter", repeat: true })).toBe(false);
  });

  it("un render ajeno justo antes de la pulsación no suelta la marca", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [member("ana"), member("luis")], next: "abc" } },
      [`${LIST}?cursor=abc`]: list([eva()]),
      [BRANCHES]: all,
      [PUT]: saved(lima),
    });
    renderApp(ui());
    const { save, choose } = await panel();
    choose("b1");
    const more = hold(api); // la página 2 llega en un render que no viene de un evento
    hold(api); // y la escritura no llega a responder
    const rows = screen.getByRole("list", { name: "Miembros" });
    new MutationObserver(() => save.click()).observe(rows, { childList: true }); // pulsa tras ese render
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    await tick();
    more();
    await waitFor(() => expect(calls(api, "PUT")).toHaveLength(1));
    fireEvent.click(save); // otra pulsación con la primera en vuelo
    await tick();
    expect(calls(api, "PUT")).toHaveLength(1);
  });

  it("las sucursales del selector: todas sus páginas, ninguna, y un fallo que se reintenta", async () => {
    let reply: { status: number; body: unknown } = { status: 500, body: { code: "X" } };
    let stuck = 0;
    const api = mockApi({
      [LIST]: everyone(),
      [BRANCHES]: () => reply,
      [`${BRANCHES}&cursor=abc`]: { status: 200, body: { results: [cusco], next: null } },
      [`${BRANCHES}&cursor=zzz`]: () => (++stuck < 9 ? reply : all), // se da a sí misma por siguiente
    });
    renderApp(ui());
    fireEvent.click(await screen.findByRole("button", { name: TRIGGER }));
    const group = screen.getByRole("group", { name: "Sucursal de luis@acme.pe" });
    expect(within(group).getByText("Cargando sucursales…")).toBeVisible();
    expect(within(group).queryByRole("button", { name: "Guardar" })).toBeNull(); // nada que guardar
    expect(await within(group).findByRole("alert")).toHaveTextContent("Algo salió mal");
    expect(reads(api, "/branches/")).toHaveLength(2); // la aplicación reintenta una lectura una vez
    reply = { status: 200, body: { results: [lima], next: "abc" } };
    const retry = within(group).getByRole("button", { name: "Reintentar" });
    retry.focus();
    act(() => onlineManager.setOnline(false));
    act(() => onlineManager.setOnline(true)); // vuelve la red: no se repide solo ni se lleva el foco
    act(() => focusManager.setFocused(true)); // ni al volver a la pestaña
    await tick();
    expect(retry).toHaveFocus();
    const release = hold(api);
    fireEvent.click(retry);
    await waitFor(() => expect(retry).toHaveTextContent("Cargando sucursales…"));
    expect(retry).toHaveFocus(); // no se desmonta mientras reintenta
    release();
    const cancel = within(group).getByRole("button", { name: "Cancelar" });
    await waitFor(() => expect(cancel).toHaveFocus());
    const select = within(group).getByRole("combobox") as HTMLSelectElement;
    expect([...select.options].map((option) => option.value)).toEqual(["", "b1", "b2"]); // página 2
    expect(within(group).queryByText("Esta organización no tiene sucursales.")).toBeNull();
    fireEvent.click(cancel);
    reply = { status: 200, body: { results: [], next: null } };
    await panel(); // cada apertura vuelve a pedirlas
    expect(await screen.findByText("Esta organización no tiene sucursales.")).toBeVisible();
    expect(reads(api, "cursor=abc")).toHaveLength(1);
    reply = { status: 200, body: { results: [lima], next: "zzz" } }; // un cursor que no avanza
    api.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Cancelar" }));
    fireEvent.click(screen.getByRole("button", { name: TRIGGER }));
    await waitFor(() => expect(reads(api, "/branches/")).toHaveLength(4)); // dos intentos, dos páginas
    await tick();
    expect(reads(api, "/branches/")).toHaveLength(4); // y ahí se detiene: no es una lista sin fin
  });

  it("sin red lo dice; sin sesión no enseña un error, va al login una vez y no reenvía", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/miembros", search: "", assign });
    const api = mockApi({
      [LIST]: everyone(),
      [BRANCHES]: all,
      [PUT]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const first = await panel();
    first.choose("b1");
    onlineManager.setOnline(false); // una escritura no espera en cola a que vuelva la red
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(first.save);
    expect(await within(first.group).findByRole("alert")).toHaveTextContent("No hay conexión");
    onlineManager.setOnline(true);
    fireEvent.click(first.cancel);
    const { save, cancel, choose } = await panel(); // reabrir empieza sin el error anterior
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    // Y vuelve a pedir las sucursales también con el `staleTime` de la aplicación (30 s).
    await waitFor(() => expect(reads(api, "/branches/")).toHaveLength(2));
    choose("b1");
    fireEvent.click(save);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Fmiembros"));
    await tick();
    fireEvent.click(save); // sigue ocupado hasta que cambia la página
    fireEvent.click(cancel);
    await tick();
    expect(calls(api, "PUT")).toHaveLength(2); // la que no llegó a salir y la del 401
    expect(assign).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("group")).toBeInTheDocument();
    expect(shown("luis@acme.pe")).toHaveTextContent(/^Sin sucursal$/);
  });
});
