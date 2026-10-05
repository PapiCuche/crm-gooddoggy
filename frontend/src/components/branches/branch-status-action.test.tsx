import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Branch, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { BranchesList } from "./branches-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const LIST = "GET /api/v1/o/acme/branches/";
const STATUS = (id: string) => `PATCH /api/v1/o/acme/branches/${id}/`;
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const branch = (id: string, name: string, active = true): Branch => ({
  id,
  code: id.toUpperCase(),
  name,
  address: "",
  district: "",
  city: "",
  phone: "",
  timezone: "America/Lima",
  is_active: active,
});
const all = [branch("b1", "Arequipa"), branch("b2", "Lima"), branch("b3", "Cusco", false)];
const list = (results: Branch[] = all) => ({ status: 200, body: { results, next: null } });
const ui = (context = tenant("organization.view", "branches.manage")) => (
  <TenantProvider value={context}>
    <BranchesList />
  </TenantProvider>
);
const card = (name: string) =>
  screen.getByText(name, { selector: "span.font-medium" }).closest("li") as HTMLElement;
// «Activa» o «Inactiva»: el estado que enseña la tarjeta.
const state = (name: string) => card(name).querySelector("p.font-medium")!.textContent;
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const sent = (api: ReturnType<typeof mockApi>) => calls(api, "PATCH");
const body = (call?: unknown[]) => JSON.parse(String((call?.[1] as RequestInit).body));
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
const announced = (name: string) =>
  within(card(name))
    .getAllByRole("status")
    .map((region) => region.textContent)
    .join("");
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
async function ask(name = "Desactivar la sucursal Lima") {
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

describe("BranchStatusAction", () => {
  it("solo se ofrece con el permiso, y lo contrario del estado de cada sucursal", async () => {
    mockApi({ [LIST]: list() });
    const view = renderApp(ui(tenant("organization.view")));
    await screen.findByRole("list", { name: "Sucursales" });
    expect(screen.queryByRole("button", { name: /Desactivar|Reactivar/ })).not.toBeInTheDocument();
    view.unmount();
    renderApp(ui());
    await screen.findByRole("list", { name: "Sucursales" });
    const offered = screen.getAllByRole("button", { name: /Desactivar|Reactivar/ });
    expect(offered.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Desactivar la sucursal Arequipa",
      "Desactivar la sucursal Lima",
      "Reactivar la sucursal Cusco",
    ]);
    expect(offered.map((button) => button.textContent)).toEqual([
      "Desactivar",
      "Desactivar",
      "Reactivar",
    ]);
    expect(document.body).toHaveFocus(); // al montar, nada toma el foco
  });

  it("pide confirmación, dice la consecuencia y cancelar no envía nada", async () => {
    const api = mockApi({ [LIST]: list() });
    renderApp(ui());
    const group = await ask();
    expect(group).toHaveTextContent(
      "¿Desactivar la sucursal Lima? Seguirá en la lista como inactiva y podrás reactivarla.",
    );
    expect(group).toHaveAccessibleDescription(/Seguirá en la lista como inactiva/);
    const cancel = within(group).getByRole("button", { name: "Cancelar" });
    expect(cancel).toHaveFocus(); // la opción que no cambia nada
    expect(within(card("Lima")).getByRole("button", { name: /^Editar/ })).toBeVisible(); // sigue
    fireEvent.click(cancel);
    expect(screen.getByRole("button", { name: "Desactivar la sucursal Lima" })).toHaveFocus();
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    await tick();
    expect(sent(api)).toHaveLength(0);
    const again = await ask("Reactivar la sucursal Cusco");
    expect(again).toHaveTextContent("¿Reactivar la sucursal Cusco? Volverá a figurar como activa.");
  });

  it("al confirmar envía una vez, cambia la tarjeta sin volver a pedir la lista y lo anuncia", async () => {
    const api = mockApi({
      [LIST]: list(),
      [STATUS("b2")]: { status: 200, body: { ...all[1]!, is_active: false, name: "Otro nombre" } },
      [STATUS("b3")]: { status: 200, body: { ...all[2]!, is_active: true } },
    });
    renderApp(ui());
    const group = await ask();
    const release = hold(api);
    const confirm = within(group).getByRole("button", { name: "Sí, desactivar" });
    confirm.focus();
    fireEvent.click(confirm);
    fireEvent.click(confirm); // ocupado: la segunda pulsación no cuenta
    await waitFor(() => expect(confirm).toHaveTextContent("Desactivando…"));
    expect(confirm).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" })); // ya se envió
    expect(screen.getByRole("group")).toBeInTheDocument();
    release();
    const next = await screen.findByRole("button", { name: "Reactivar la sucursal Lima" });
    await waitFor(() => expect(next).toHaveFocus()); // el botón pulsado se fue
    expect(state("Lima")).toBe("Inactiva"); // solo el estado: el nombre de la respuesta no entra
    expect(announced("Lima")).toBe("La sucursal Lima quedó inactiva.");
    const [[url, init]] = sent(api) as [[string, RequestInit]];
    expect([url, init.body]).toEqual([
      "/api/v1/o/acme/branches/b2/", // el id que dio la API
      JSON.stringify({ is_active: false }), // y nada más
    ]);
    expect(api).toHaveBeenCalledTimes(2); // la lista, una vez; y la escritura, una vez
    const back = await ask("Reactivar la sucursal Cusco");
    fireEvent.click(within(back).getByRole("button", { name: "Sí, reactivar" }));
    await screen.findByRole("button", { name: "Desactivar la sucursal Cusco" });
    expect(state("Cusco")).toBe("Activa");
    expect(announced("Cusco")).toBe("La sucursal Cusco quedó activa.");
    expect(body(sent(api)[1])).toEqual({ is_active: true });
    expect([state("Arequipa"), state("Lima")]).toEqual(["Activa", "Inactiva"]); // cada una, lo suyo
  });

  it.each([
    [403, "PERMISSION_DENIED", "No tienes permiso para cambiar sucursales."],
    [400, "VALIDATION_ERROR", "Algo salió mal de nuestro lado."], // no hay campos que revisar
    [500, "INTERNAL_ERROR", "Algo salió mal de nuestro lado."],
    [418, "TEAPOT", "Algo salió mal de nuestro lado."],
  ])("un %i %s se explica y no cambia la tarjeta", async (status, code, text) => {
    let reply = { status, body: { code, message: "texto de la API" } as unknown };
    const api = mockApi({ [LIST]: list(), [STATUS("b2")]: () => reply });
    renderApp(ui());
    const group = await ask();
    const confirm = within(group).getByRole("button", { name: "Sí, desactivar" });
    fireEvent.click(confirm);
    expect(await within(group).findByRole("alert")).toHaveTextContent(text);
    expect(group).not.toHaveTextContent("texto de la API");
    expect(state("Lima")).toBe("Activa");
    expect(announced("Lima")).toBe("");
    await tick(); // estos errores no vuelven a pedir la lista
    expect(calls(api, "GET")).toHaveLength(1);
    fireEvent.click(confirm); // falla otra vez
    await waitFor(() => expect(sent(api)).toHaveLength(2));
    await tick();
    expect(within(group).getByRole("alert")).toHaveTextContent(text);
    reply = { status: 200, body: { ...all[1]!, is_active: false } };
    fireEvent.click(confirm); // el mismo botón reintenta
    await screen.findByRole("button", { name: "Reactivar la sucursal Lima" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("sin red lo dice, y volver a abrir empieza sin el error anterior", async () => {
    const api = mockApi({ [LIST]: list() });
    renderApp(ui());
    const group = await ask();
    onlineManager.setOnline(false); // una escritura no espera en cola a que vuelva la red
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(within(group).getByRole("button", { name: "Sí, desactivar" }));
    expect(await within(group).findByRole("alert")).toHaveTextContent("No hay conexión");
    expect(sent(api)).toHaveLength(1);
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    const reopened = await ask();
    expect(within(reopened).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("sin sesión no enseña un error, va al login una vez y no se puede enviar de nuevo", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/sucursales", search: "", assign });
    const api = mockApi({
      [LIST]: list(),
      [STATUS("b2")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const group = await ask();
    const confirm = within(group).getByRole("button", { name: "Sí, desactivar" });
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Fsucursales"),
    );
    await tick();
    fireEvent.click(confirm); // sigue ocupado hasta que cambia la página
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    await tick();
    expect(sent(api)).toHaveLength(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(within(group).getByRole("button", { name: "Desactivando…" })).toBeInTheDocument();
    expect(state("Lima")).toBe("Activa");
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("un 404 vuelve a pedir la lista y lo explica fuera de la tarjeta", async () => {
    let rows = all;
    const api = mockApi({
      [LIST]: () => list(rows),
      [STATUS("b2")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const group = await ask();
    rows = [all[0]!, all[2]!]; // lo que la API tiene de verdad: la sucursal ya no existe
    fireEvent.click(within(group).getByRole("button", { name: "Sí, desactivar" }));
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /sucursal Lima/ })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "La sucursal Lima ya no existe. Revisa la lista.",
    );
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus(); // el foco no se pierde
    await ask("Reactivar la sucursal Cusco"); // el aviso se va con la siguiente acción
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("con la pantalla desfasada la confirmación se cierra, y el foco de quien se fue no se toca", async () => {
    const api = mockApi({
      [LIST]: list(),
      [STATUS("b2")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const group = await ask();
    const release = hold(api);
    fireEvent.click(within(group).getByRole("button", { name: "Sí, desactivar" }));
    await waitFor(() => expect(sent(api)).toHaveLength(1));
    const elsewhere = screen.getByRole("button", { name: "Crear sucursal" });
    elsewhere.focus(); // el usuario ya está en otra parte cuando llega la negativa
    release();
    expect(await screen.findByRole("alert")).toHaveTextContent("La sucursal Lima ya no existe");
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    await tick();
    expect(screen.queryByRole("group")).not.toBeInTheDocument(); // se cerró sin esperar
    expect(screen.getByRole("button", { name: "Desactivar la sucursal Lima" })).toBeVisible();
    expect(elsewhere).toHaveFocus();
    fireEvent.click(elsewhere); // abrir «Crear sucursal» también retira el aviso
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("un 404 con el foco en «Editar», en la misma tarjeta, lleva el foco al título", async () => {
    let rows = all;
    mockApi({
      [LIST]: () => list(rows),
      [STATUS("b2")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const group = await ask();
    fireEvent.click(within(group).getByRole("button", { name: "Sí, desactivar" }));
    within(card("Lima")).getByRole("button", { name: "Editar la sucursal Lima" }).focus();
    rows = [all[0]!, all[2]!];
    await screen.findByRole("alert");
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
  });

  it("una lectura en vuelo no pisa la tarjeta que acaba de cambiar", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [all[0]!, all[1]!], next: "abc" } },
      [`${LIST}?cursor=abc`]: list([branch("b9", "Tacna")]),
      [STATUS("b2")]: { status: 200, body: { ...all[1]!, is_active: false } },
    });
    renderApp(ui());
    const group = await ask();
    const release = hold(api);
    const more = screen.getByRole("button", { name: "Cargar más" });
    fireEvent.click(more); // sale con la lista de antes de la escritura
    fireEvent.click(within(group).getByRole("button", { name: "Sí, desactivar" }));
    await screen.findByRole("button", { name: "Reactivar la sucursal Lima" });
    release();
    await tick();
    expect([state("Lima"), state("Arequipa")]).toEqual(["Inactiva", "Activa"]);
    // Esa lectura se canceló: el botón vuelve a estar libre y la página se pide otra vez.
    await waitFor(() => expect(more).toHaveAttribute("aria-disabled", "false"));
    fireEvent.click(more);
    expect(await screen.findByText("Tacna", { selector: "span.font-medium" })).toBeVisible();
    expect(state("Lima")).toBe("Inactiva");
    expect(screen.queryByRole("button", { name: "Cargar más" })).not.toBeInTheDocument();
  });

  it("una relectura entera en vuelo se cancela y se repite tras el cambio", async () => {
    let rows = all;
    const api = mockApi({
      [LIST]: () => list(rows),
      [STATUS("b2")]: { status: 200, body: { ...all[1]!, is_active: false } },
    });
    const view = renderApp(ui());
    const group = await ask();
    let releaseRead = () => {};
    api.mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => (releaseRead = resolve));
      return new Response(JSON.stringify({ results: all, next: null }), { status: 200 });
    });
    void view.client.refetchQueries();
    await tick();
    rows = [all[0]!, { ...all[1]!, is_active: false }, all[2]!]; // lo que la API tiene después
    fireEvent.click(within(group).getByRole("button", { name: "Sí, desactivar" }));
    await screen.findByRole("button", { name: "Reactivar la sucursal Lima" });
    releaseRead(); // llega tarde: no devuelve «Activa» a la tarjeta
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(3)); // y la lectura se repite
    await tick();
    expect(state("Lima")).toBe("Inactiva");
  });

  it("lo que se confirma queda fijado al abrir, aunque la lista cambie debajo", async () => {
    let active = true;
    const api = mockApi({
      [LIST]: () => list([all[0]!, branch("b2", "Lima", active), all[2]!]),
      [STATUS("b2")]: { status: 200, body: { ...all[1]!, is_active: false } },
    });
    const view = renderApp(ui());
    await ask();
    active = false; // otro administrador la desactivó con la confirmación abierta
    await refresh(view);
    expect(screen.queryByRole("group")).not.toBeInTheDocument(); // nada que confirmar ya
    expect(announced("Lima")).toBe("La sucursal Lima quedó inactiva.");
    expect(sent(api)).toHaveLength(0);
    active = true;
    await refresh(view);
    const group = await ask();
    const release = hold(api);
    const confirm = within(group).getByRole("button", { name: "Sí, desactivar" });
    fireEvent.click(confirm);
    await waitFor(() => expect(confirm).toHaveTextContent("Desactivando…"));
    active = false; // y ahora, con la escritura en vuelo
    const other = screen.getByRole("button", { name: "Desactivar la sucursal Arequipa" });
    other.focus();
    await refresh(view);
    expect(state("Lima")).toBe("Inactiva"); // la lista ya lo dice
    expect(confirm).toHaveTextContent("Desactivando…"); // y la pregunta no se da la vuelta
    expect(screen.getByRole("group")).toHaveAccessibleName("Desactivar la sucursal Lima");
    expect(screen.getByRole("group")).toHaveAccessibleDescription(/¿Desactivar la sucursal Lima/);
    release();
    await screen.findByRole("button", { name: "Reactivar la sucursal Lima" });
    await tick();
    expect(body(sent(api)[0])).toEqual({ is_active: false });
    expect(announced("Lima")).toBe("La sucursal Lima quedó inactiva.");
    expect(other).toHaveFocus(); // el usuario ya estaba en otra parte: el foco no se le quita
  });

  it("una pulsación entre la respuesta y el render no reenvía, ni la suelta un render ajeno", async () => {
    let reply: { status: number; body: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [all[0]!, all[1]!], next: "abc" } },
      [`${LIST}?cursor=abc`]: list([all[2]!]),
      [STATUS("b2")]: () => reply,
    });
    renderApp(ui());
    const group = await ask();
    const confirm = within(group).getByRole("button", { name: "Sí, desactivar" });
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
    reply = { status: 200, body: { ...all[1]!, is_active: false } };
    const more = hold(api);
    hold(api); // la escritura no llega a responder
    const rows = screen.getByRole("list", { name: "Sucursales" });
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

  it("la tarjeta enseña lo que respondió la API, y una respuesta de otra sucursal no se aplica", async () => {
    let reply: unknown = { ...all[1]!, is_active: true };
    const api = mockApi({ [LIST]: list(), [STATUS("b2")]: () => ({ status: 200, body: reply }) });
    renderApp(ui());
    fireEvent.click(within(await ask()).getByRole("button", { name: "Sí, desactivar" }));
    await waitFor(() => expect(announced("Lima")).toBe("La sucursal Lima quedó activa."));
    expect(state("Lima")).toBe("Activa");
    reply = { ...all[2]!, is_active: true }; // la de Cusco, que en la lista sigue inactiva
    fireEvent.click(within(await ask()).getByRole("button", { name: "Sí, desactivar" }));
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2)); // se pregunta a la API
    expect(announced("Lima")).toBe("");
    expect(state("Cusco")).toBe("Inactiva"); // el de la lista, no el recibido
    fireEvent.click(screen.getByRole("button", { name: "Cancelar" })); // la confirmación sigue viva
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });
});
