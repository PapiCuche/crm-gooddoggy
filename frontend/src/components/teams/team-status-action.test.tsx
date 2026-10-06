import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext, Team } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { TeamsList } from "./teams-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const LIST = "GET /api/v1/o/acme/teams/";
const STATUS = (id: string) => `PATCH /api/v1/o/acme/teams/${id}/`;
const tenant = (
  codes: string[] = ["teams.view", "teams.manage"],
  roles: SelfContext["roles"] = [],
): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles,
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
// El `slug` es distinto del `id` y no cambia con el nombre: la ruta lleva el `id`.
const SLUGS: Record<string, string> = { t1: "soporte", t2: "ventas", t3: "cobranza" };
const team = (id: string, name: string, active = true): Team => ({
  id,
  slug: SLUGS[id] ?? id,
  name,
  description: "",
  assignment_strategy: "MANUAL",
  is_active: active,
});
const all = [team("t1", "Soporte"), team("t2", "Ventas"), team("t3", "Cobranza", false)];
const list = (results: Team[] = all) => ({ status: 200, body: { results, next: null } });
const ui = (context = tenant()) => (
  <TenantProvider value={context}>
    <TeamsList />
  </TenantProvider>
);
const card = (name: string) =>
  screen.getByText(name, { selector: "span.font-medium" }).closest("li") as HTMLElement;
// «Activo» o «Inactivo»: el estado que enseña la tarjeta.
const state = (name: string) => card(name).querySelector("p.font-medium")!.textContent;
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const sent = (api: ReturnType<typeof mockApi>) => calls(api, "PATCH");
const body = (call?: unknown[]) => JSON.parse(String((call?.[1] as RequestInit).body));
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
// La región de esta acción, montada desde el principio: la segunda, tras la de «Editar».
const announced = (name: string) => within(card(name)).getAllByRole("status")[1]!.textContent;
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
async function ask(name = "Desactivar el equipo Ventas (ventas)") {
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

describe("TeamStatusAction", () => {
  it("solo se ofrece con el permiso, y lo contrario del estado de cada equipo", async () => {
    mockApi({ [LIST]: list() });
    // Ni ver equipos ni un rol «owner» la ofrecen: decide el permiso que dio la API.
    const view = renderApp(ui(tenant(["teams.view"], [{ code: "owner", name: "Owner" }])));
    await screen.findByRole("list", { name: "Equipos" });
    expect(screen.queryByRole("button", { name: /Desactivar|Reactivar/ })).not.toBeInTheDocument();
    view.unmount();
    renderApp(ui());
    await screen.findByRole("list", { name: "Equipos" });
    const offered = screen.getAllByRole("button", { name: /Desactivar|Reactivar/ });
    expect(offered.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Desactivar el equipo Soporte (soporte)",
      "Desactivar el equipo Ventas (ventas)",
      "Reactivar el equipo Cobranza (cobranza)",
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
      "¿Desactivar el equipo Ventas? Seguirá en la lista como inactivo, con sus integrantes, y podrás reactivarlo.",
    );
    expect(group).toHaveAccessibleDescription(/como inactivo, con sus integrantes/);
    expect(group.parentElement).toHaveClass("w-full"); // abierta ocupa su fila, bajo «Editar»
    const cancel = within(group).getByRole("button", { name: "Cancelar" });
    expect(cancel).toHaveFocus(); // la opción que no cambia nada
    expect(fireEvent.keyDown(cancel, { key: "Enter", repeat: true })).toBe(false); // mantenido
    expect(fireEvent.keyDown(cancel, { key: "Enter" })).toBe(true);
    expect(within(card("Ventas")).getByRole("button", { name: /^Editar/ })).toBeVisible(); // sigue
    fireEvent.click(cancel);
    expect(
      screen.getByRole("button", { name: "Desactivar el equipo Ventas (ventas)" }),
    ).toHaveFocus();
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    await tick();
    expect(sent(api)).toHaveLength(0);
    const again = await ask("Reactivar el equipo Cobranza (cobranza)");
    expect(again).toHaveTextContent(
      "¿Reactivar el equipo Cobranza? Volverá a figurar como activo.",
    );
  });

  it("al confirmar envía una vez, cambia la tarjeta sin volver a pedir la lista y lo anuncia", async () => {
    const api = mockApi({
      [LIST]: list(),
      [STATUS("t2")]: { status: 200, body: { ...all[1]!, is_active: false, name: "Otro nombre" } },
      [STATUS("t3")]: { status: 200, body: { ...all[2]!, is_active: true } },
    });
    renderApp(ui());
    const group = await ask();
    const release = hold(api);
    const confirm = within(group).getByRole("button", { name: "Sí, desactivar" });
    confirm.focus();
    fireEvent.click(confirm);
    fireEvent.click(confirm); // ocupado: la segunda pulsación no cuenta
    const cancel = within(group).getByRole("button", { name: "Cancelar" });
    fireEvent.click(cancel); // ya se envió, aunque la pantalla aún no lo diga: no se cierra
    await waitFor(() => expect(confirm).toHaveTextContent("Desactivando…"));
    for (const button of [confirm, cancel]) expect(button).toHaveAttribute("aria-disabled", "true");
    release();
    const next = await screen.findByRole("button", { name: "Reactivar el equipo Ventas (ventas)" });
    await waitFor(() => expect(next).toHaveFocus()); // el botón pulsado se fue
    expect(state("Ventas")).toBe("Inactivo"); // solo el estado: el nombre de la respuesta no entra
    expect(announced("Ventas")).toBe("El equipo Ventas quedó inactivo.");
    const [[url, init]] = sent(api) as [[string, RequestInit]];
    expect([url, init.body]).toEqual([
      "/api/v1/o/acme/teams/t2/", // el `id` que dio la API, no el `slug`
      JSON.stringify({ is_active: false }), // y nada más
    ]);
    expect(api).toHaveBeenCalledTimes(2); // la lista, una vez; y la escritura, una vez
    const back = await ask("Reactivar el equipo Cobranza (cobranza)");
    fireEvent.click(within(back).getByRole("button", { name: "Sí, reactivar" }));
    await screen.findByRole("button", { name: "Desactivar el equipo Cobranza (cobranza)" });
    expect(state("Cobranza")).toBe("Activo");
    expect(announced("Cobranza")).toBe("El equipo Cobranza quedó activo.");
    expect(body(sent(api)[1])).toEqual({ is_active: true });
    expect([state("Soporte"), state("Ventas")]).toEqual(["Activo", "Inactivo"]); // cada uno, lo suyo
  });

  it.each([
    [403, "PERMISSION_DENIED", "No tienes permiso para cambiar equipos."],
    [400, "VALIDATION_ERROR", "Algo salió mal de nuestro lado."], // no hay campos que revisar
    [500, "INTERNAL_ERROR", "Algo salió mal de nuestro lado."],
    [418, "TEAPOT", "Algo salió mal de nuestro lado."],
  ])("un %i %s se explica y no cambia la tarjeta", async (status, code, text) => {
    let reply = { status, body: { code, message: "texto de la API" } as unknown };
    const api = mockApi({ [LIST]: list(), [STATUS("t2")]: () => reply });
    renderApp(ui());
    const group = await ask();
    const confirm = within(group).getByRole("button", { name: "Sí, desactivar" });
    fireEvent.click(confirm);
    expect(await within(group).findByRole("alert")).toHaveTextContent(text);
    expect(group).not.toHaveTextContent("texto de la API");
    expect(state("Ventas")).toBe("Activo");
    expect(announced("Ventas")).toBe("");
    await tick(); // estos errores no vuelven a pedir la lista
    expect(calls(api, "GET")).toHaveLength(1);
    fireEvent.click(confirm); // falla otra vez
    await waitFor(() => expect(sent(api)).toHaveLength(2));
    await tick();
    expect(within(group).getByRole("alert")).toHaveTextContent(text);
    reply = { status: 200, body: { ...all[1]!, is_active: false } };
    fireEvent.click(confirm); // el mismo botón reintenta
    await screen.findByRole("button", { name: "Reactivar el equipo Ventas (ventas)" });
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
    vi.stubGlobal("location", { origin, pathname: "/o/acme/equipos", search: "", assign });
    const api = mockApi({
      [LIST]: list(),
      [STATUS("t2")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const group = await ask();
    const confirm = within(group).getByRole("button", { name: "Sí, desactivar" });
    fireEvent.click(confirm);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Fequipos"));
    await tick();
    fireEvent.click(confirm); // sigue ocupado hasta que cambia la página
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    await tick();
    expect(sent(api)).toHaveLength(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(within(group).getByRole("button", { name: "Desactivando…" })).toBeInTheDocument();
    expect(state("Ventas")).toBe("Activo");
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("un 404 vuelve a pedir la lista y lo explica fuera de la tarjeta", async () => {
    let rows = all;
    const api = mockApi({
      [LIST]: () => list(rows),
      [STATUS("t2")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const group = await ask();
    rows = [all[0]!, all[2]!]; // lo que la API tiene de verdad: el equipo ya no está
    fireEvent.click(within(group).getByRole("button", { name: "Sí, desactivar" }));
    (document.activeElement as HTMLElement).blur(); // el foco, en ninguna parte
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /equipo Ventas/ })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "No se pudo cambiar el equipo Ventas: ya no está disponible. Revisa la lista.",
    );
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus(); // el foco no se pierde
    await ask("Reactivar el equipo Cobranza (cobranza)"); // el aviso se va con la siguiente acción
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("con la pantalla desfasada la confirmación se cierra, y el foco de quien se fue no se toca", async () => {
    const api = mockApi({
      [LIST]: list(),
      [STATUS("t2")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const group = await ask();
    const release = hold(api);
    fireEvent.click(within(group).getByRole("button", { name: "Sí, desactivar" }));
    await waitFor(() => expect(sent(api)).toHaveLength(1));
    const elsewhere = screen.getByRole("button", { name: "Crear equipo" });
    elsewhere.focus(); // el usuario ya está en otra parte cuando llega la negativa
    release();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No se pudo cambiar el equipo Ventas: ya no está disponible",
    );
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    await tick();
    expect(screen.queryByRole("group")).not.toBeInTheDocument(); // se cerró sin esperar
    expect(
      screen.getByRole("button", { name: "Desactivar el equipo Ventas (ventas)" }),
    ).toBeVisible();
    expect(elsewhere).toHaveFocus();
    fireEvent.click(elsewhere); // abrir «Crear equipo» también retira el aviso
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("un 404 con el foco en «Editar», en la misma tarjeta, lleva el foco al título", async () => {
    let rows = all;
    mockApi({
      [LIST]: () => list(rows),
      [STATUS("t2")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const group = await ask();
    fireEvent.click(within(group).getByRole("button", { name: "Sí, desactivar" }));
    within(card("Ventas"))
      .getByRole("button", { name: "Editar el equipo Ventas (ventas)" })
      .focus();
    rows = [all[0]!, all[2]!];
    await screen.findByRole("alert");
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
  });

  it("una lectura en vuelo no pisa la tarjeta que acaba de cambiar", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [all[0]!, all[1]!], next: "abc" } },
      [`${LIST}?cursor=abc`]: list([team("t9", "Postventa")]),
      [STATUS("t2")]: { status: 200, body: { ...all[1]!, is_active: false } },
    });
    renderApp(ui());
    const group = await ask();
    const release = hold(api);
    const more = screen.getByRole("button", { name: "Cargar más" });
    fireEvent.click(more); // sale con la lista de antes de la escritura
    fireEvent.click(within(group).getByRole("button", { name: "Sí, desactivar" }));
    await screen.findByRole("button", { name: "Reactivar el equipo Ventas (ventas)" });
    release();
    await tick();
    expect([state("Ventas"), state("Soporte")]).toEqual(["Inactivo", "Activo"]);
    // Esa lectura se canceló: el botón vuelve a estar libre y la página se pide otra vez.
    await waitFor(() => expect(more).toHaveAttribute("aria-disabled", "false"));
    fireEvent.click(more);
    expect(await screen.findByText("Postventa", { selector: "span.font-medium" })).toBeVisible();
    expect(state("Ventas")).toBe("Inactivo");
    expect(screen.queryByRole("button", { name: "Cargar más" })).not.toBeInTheDocument();
  });

  it("una relectura entera en vuelo se cancela y se repite tras el cambio", async () => {
    let rows = all;
    const api = mockApi({
      [LIST]: () => list(rows),
      [STATUS("t2")]: { status: 200, body: { ...all[1]!, is_active: false } },
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
    await screen.findByRole("button", { name: "Reactivar el equipo Ventas (ventas)" });
    releaseRead(); // llega tarde: no devuelve «Activo» a la tarjeta
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(3)); // y la lectura se repite
    await tick();
    expect(state("Ventas")).toBe("Inactivo");
  });

  it("lo que se confirma queda fijado al abrir, aunque la lista cambie debajo", async () => {
    let [name, active] = ["Ventas", true];
    const api = mockApi({
      [LIST]: () => list([all[0]!, team("t2", name, active), all[2]!]),
      [STATUS("t2")]: { status: 200, body: { ...all[1]!, is_active: false } },
    });
    const view = renderApp(ui());
    await ask();
    active = false; // otro administrador lo desactivó con la confirmación abierta
    await refresh(view);
    expect(screen.queryByRole("group")).not.toBeInTheDocument(); // nada que confirmar ya
    expect(announced("Ventas")).toBe("El equipo Ventas quedó inactivo.");
    expect(sent(api)).toHaveLength(0);
    active = true;
    await refresh(view);
    const group = await ask();
    const release = hold(api);
    const confirm = within(group).getByRole("button", { name: "Sí, desactivar" });
    fireEvent.click(confirm);
    await waitFor(() => expect(confirm).toHaveTextContent("Desactivando…"));
    active = false; // y ahora, con la escritura en vuelo
    const other = screen.getByRole("button", { name: "Desactivar el equipo Soporte (soporte)" });
    other.focus();
    await refresh(view);
    expect(state("Ventas")).toBe("Inactivo"); // la lista ya lo dice
    expect(confirm).toHaveTextContent("Desactivando…"); // y la pregunta no se da la vuelta
    expect(screen.getByRole("group")).toHaveAccessibleName("Desactivar el equipo Ventas (ventas)");
    expect(screen.getByRole("group")).toHaveAccessibleDescription(/¿Desactivar el equipo Ventas\?/);
    release();
    await screen.findByRole("button", { name: "Reactivar el equipo Ventas (ventas)" });
    await tick();
    expect(body(sent(api)[0])).toEqual({ is_active: false });
    expect(announced("Ventas")).toBe("El equipo Ventas quedó inactivo.");
    expect(other).toHaveFocus(); // el usuario ya estaba en otra parte: el foco no se le quita
    name = "Ventas Lima"; // el equipo cambia de nombre después: el anuncio no se repite con él
    await refresh(view);
    expect(announced("Ventas Lima")).toBe("El equipo Ventas quedó inactivo.");
  });

  it("una pulsación entre la respuesta y el render no reenvía, ni la suelta un render ajeno", async () => {
    let reply: { status: number; body: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [all[0]!, all[1]!], next: "abc" } },
      [`${LIST}?cursor=abc`]: list([all[2]!]),
      [STATUS("t2")]: () => reply,
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
    const rows = screen.getByRole("list", { name: "Equipos" });
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

  it("la tarjeta enseña lo que respondió la API, y una respuesta de otro equipo no se aplica", async () => {
    let reply: unknown = { ...all[1]!, is_active: true };
    const api = mockApi({ [LIST]: list(), [STATUS("t2")]: () => ({ status: 200, body: reply }) });
    renderApp(ui());
    fireEvent.click(within(await ask()).getByRole("button", { name: "Sí, desactivar" }));
    await waitFor(() => expect(announced("Ventas")).toBe("El equipo Ventas quedó activo."));
    expect(state("Ventas")).toBe("Activo");
    reply = { ...all[2]!, is_active: true }; // el de Cobranza, que en la lista sigue inactivo
    fireEvent.click(within(await ask()).getByRole("button", { name: "Sí, desactivar" }));
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2)); // se pregunta a la API
    expect(announced("Ventas")).toBe("");
    expect(state("Cobranza")).toBe("Inactivo"); // el de la lista, no el recibido
    fireEvent.click(screen.getByRole("button", { name: "Cancelar" })); // la confirmación sigue viva
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });
});
