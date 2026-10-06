import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext, Team } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { TeamsList } from "./teams-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const LIST = "GET /api/v1/o/acme/teams/";
const EDIT = (id: string) => `PATCH /api/v1/o/acme/teams/${id}/`;
const LABELS = { name: "Nombre", description: "Descripción (opcional)" };
type Field = keyof typeof LABELS;
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
const team = (id: string, slug: string, name: string, extra: Partial<Team> = {}): Team => ({
  id, // distinto del `slug`: la ruta lleva el `id`
  slug,
  name,
  description: "",
  assignment_strategy: "MANUAL",
  is_active: true,
  ...extra,
});
const sales = team("t2", "ventas", "Ventas", {
  description: "Clientes nuevos",
  assignment_strategy: "ROUND_ROBIN",
});
const teams = [
  team("t1", "soporte", "Soporte"),
  sales,
  team("t3", "cobranza", "Cobranza", { is_active: false }),
];
const list = (results: Team[] = teams) => ({ status: 200, body: { results, next: null } });
const ui = (context = tenant()) => (
  <TenantProvider value={context}>
    <TeamsList />
  </TenantProvider>
);
const card = (name: string) =>
  screen.getByText(name, { selector: "span.font-medium" }).closest("li") as HTMLElement;
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const body = (call?: unknown[]) => JSON.parse(String((call?.[1] as RequestInit).body));
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
const trigger = (name = "Ventas") =>
  screen.getByRole("button", { name: `Editar el equipo ${name}` });
const field = (form: HTMLElement, name: Field) => within(form).getByLabelText(LABELS[name]);
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
async function opened(name = "Ventas") {
  await screen.findByRole("list", { name: "Equipos" });
  fireEvent.click(trigger(name));
  return screen.getByRole("form", { name: `Editar ${name}` });
}
function fill(form: HTMLElement, values: Partial<Record<Field, string>>) {
  for (const [name, value] of Object.entries(values)) {
    fireEvent.input(field(form, name as Field), { target: { value } });
  }
}
// Como una pulsación real: el botón recibe el foco antes del clic.
function send(form: HTMLElement) {
  const button = within(form).getByRole("button", { name: "Guardar" });
  button.focus();
  fireEvent.click(button);
}

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con token: una escritura no pide antes el CSRF
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("TeamEditAction", () => {
  it("se ofrece con el permiso, en cada equipo; abrir enseña lo que hay y no envía nada", async () => {
    const api = mockApi({ [LIST]: list() });
    // Ni ver equipos ni un rol «owner» la ofrecen: decide el permiso que dio la API.
    const denied = tenant(["teams.view", "users.view"], [{ code: "owner", name: "Owner" }]);
    const view = renderApp(ui(denied));
    await screen.findByRole("list", { name: "Equipos" });
    expect(screen.queryByRole("button", { name: /^Editar/ })).not.toBeInTheDocument();
    view.unmount();
    renderApp(ui());
    await screen.findByRole("list", { name: "Equipos" });
    expect(screen.getAllByRole("button", { name: /^Editar el equipo/ })).toHaveLength(3);
    expect(trigger("Cobranza")).toBeVisible(); // también uno inactivo
    const form = await opened();
    expect(within(card("Ventas")).queryByRole("button", { name: /^Editar/ })).toBeNull();
    expect(field(form, "name")).toHaveFocus();
    const shown = Object.keys(LABELS).map((name) => [
      (field(form, name as Field) as HTMLInputElement).value,
      field(form, name as Field).getAttribute("maxlength"),
    ]);
    expect(shown).toEqual([
      ["Ventas", "100"],
      ["Clientes nuevos", "255"],
    ]);
    // Ni el identificador ni la forma de asignar se editan aquí.
    expect(within(form).getAllByRole("textbox")).toHaveLength(2);
    expect(within(form).queryByRole("combobox")).not.toBeInTheDocument();
    expect(within(form).queryByLabelText("Identificador")).not.toBeInTheDocument();
    fill(form, { name: "A medias" });
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
    await tick();
    expect(calls(api, "PATCH")).toHaveLength(0);
    fireEvent.click(trigger()); // al reabrir, lo que hay: lo cancelado no se arrastra
    expect(field(screen.getByRole("form"), "name")).toHaveValue("Ventas");
  });

  it("guarda con un solo envío, cambia la tarjeta sin volver a pedir la lista y lo anuncia", async () => {
    // La respuesta trae además cosas que este formulario no edita: no llegan a la tarjeta.
    const saved = {
      ...sales,
      name: "Ventas Lima",
      description: "",
      slug: "otro",
      assignment_strategy: "MANUAL" as const,
      is_active: false,
    };
    const api = mockApi({ [LIST]: list(), [EDIT("t2")]: { status: 200, body: saved } });
    renderApp(ui());
    const form = await opened();
    fill(form, { name: "  Ventas   Lima ", description: "  " });
    const release = hold(api);
    send(form);
    fireEvent.submit(form); // Enter y un segundo clic mientras se envía: un solo cambio
    send(form);
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" })); // ni se cierra
    await tick();
    expect(within(form).getByRole("button", { name: "Guardando…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    release();
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(calls(api, "PATCH")).toHaveLength(1);
    const [url, init] = calls(api, "PATCH")[0]!;
    expect(String(url)).toBe("/api/v1/o/acme/teams/t2/"); // el `id` que dio la API, no el `slug`
    // Sin espacios exteriores; ni el identificador, ni la asignación, ni el estado viajan.
    expect(body([url, init])).toEqual({ name: "Ventas   Lima", description: "" });
    const lines = [...card("Ventas Lima").querySelectorAll("p")].map((p) => p.textContent);
    expect(lines.slice(0, 4)).toEqual([
      "Ventas Limaventas", // el identificador de la fila no lo toca la respuesta
      "Sin descripción",
      "Asignación: Por turnos", // ni lo que este formulario no edita
      "Activo",
    ]);
    const done = within(card("Ventas Lima")).getByText("Equipo «Ventas Lima» guardado.");
    expect(done).toHaveAttribute("role", "status");
    expect(done.textContent).toBe("Equipo «Ventas Lima» guardado."); // el nombre guardado
    await waitFor(() => expect(trigger("Ventas Lima")).toHaveFocus());
    expect(calls(api, "GET")).toHaveLength(1); // la lista no se vuelve a pedir
    expect(card("Cobranza")).toHaveTextContent("Inactivo"); // solo cambia ese equipo
    expect(card("Soporte")).toHaveTextContent("Sin descripción");
    fireEvent.click(trigger("Ventas Lima")); // al reabrir: lo guardado, y sin el anuncio
    expect(field(screen.getByRole("form"), "description")).toHaveValue("");
    expect(screen.queryByText(/guardado/)).not.toBeInTheDocument();
  });

  it("sin nombre no envía nada: lo dice junto al campo; la descripción puede quedar vacía", async () => {
    const api = mockApi({ [LIST]: list(), [EDIT("t2")]: { status: 200, body: sales } });
    renderApp(ui());
    const form = await opened();
    fill(form, { name: "  ", description: "" });
    send(form);
    expect(field(form, "name")).toHaveAccessibleDescription("Escribe un nombre para el equipo.");
    expect(field(form, "description")).not.toHaveAttribute("aria-invalid");
    expect(field(form, "name")).toHaveFocus();
    await tick();
    expect(calls(api, "PATCH")).toHaveLength(0);
  });

  it("lo que la API no acepta se dice junto a su campo, y lo demás en el formulario", async () => {
    let reply: { status: number; body: unknown } = {
      status: 400,
      body: {
        code: "VALIDATION_ERROR",
        fields: { description: [{ code: "invalid", message: "<b>texto de la API</b>" }] },
      },
    };
    const api = mockApi({ [LIST]: list(), [EDIT("t2")]: () => reply });
    renderApp(ui());
    const form = await opened();
    fill(form, { description: "Dos\tcolumnas" });
    send(form);
    await waitFor(() =>
      expect(field(form, "description")).toHaveAccessibleDescription(
        "Esa descripción no sirve. Usa texto en una línea, de hasta 255 caracteres.",
      ),
    );
    expect(field(form, "description")).toHaveFocus();
    expect(field(form, "name")).not.toHaveAttribute("aria-invalid");
    expect(within(form).getAllByRole("alert")).toHaveLength(1);
    reply = {
      status: 400,
      body: { code: "VALIDATION_ERROR", fields: { name: [{ code: "invalid" }] } },
    };
    send(form);
    await waitFor(() =>
      expect(field(form, "name")).toHaveAccessibleDescription(/Ese nombre no sirve/),
    );
    reply = { status: 403, body: { code: "PERMISSION_DENIED", message: "texto de la API" } };
    send(form);
    expect(
      await within(form).findByText("No tienes permiso para cambiar equipos."),
    ).toHaveAttribute("role", "alert");
    // Un campo que este formulario no tiene: un fallo nuestro, no un aviso sin sitio.
    reply = {
      status: 400,
      body: { code: "VALIDATION_ERROR", fields: { assignment_strategy: [{ code: "invalid" }] } },
    };
    send(form);
    expect(await within(form).findByText(/Algo salió mal/)).toBeVisible();
    reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    send(form);
    expect(await within(form).findByText(/Algo salió mal/)).toBeVisible();
    expect(form).not.toHaveTextContent("texto de la API"); // nunca el texto de la respuesta
    expect(field(form, "description")).toHaveValue("Dos\tcolumnas"); // lo escrito no se pierde
    expect(calls(api, "PATCH")).toHaveLength(5);
    expect(card("Ventas")).toHaveTextContent("Clientes nuevos"); // la tarjeta no cambia
  });

  it("sin red el intento falla y se dice: no queda en cola", async () => {
    const api = mockApi({ [LIST]: list(), [EDIT("t2")]: { status: 200, body: sales } });
    renderApp(ui());
    const form = await opened();
    onlineManager.setOnline(false);
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    send(form);
    expect(await within(form).findByRole("alert")).toHaveTextContent("No hay conexión");
    onlineManager.setOnline(true);
    send(form); // con red, el mismo botón lo consigue
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(calls(api, "PATCH")).toHaveLength(2);
  });

  it("una lectura en vuelo no pisa lo recién guardado: se cancela, y la lista entera se repite", async () => {
    const saved = { ...sales, name: "Ventas Lima" };
    let rows = teams;
    const api = mockApi({ [LIST]: () => list(rows), [EDIT("t2")]: { status: 200, body: saved } });
    const view = renderApp(ui());
    const form = await opened();
    fill(form, { name: "Ventas Lima" });
    // Una relectura de la lista en vuelo, que responderá con el nombre de antes.
    let releaseRead = () => {};
    api.mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => (releaseRead = resolve));
      return new Response(JSON.stringify({ results: teams, next: null }), { status: 200 });
    });
    void view.client.refetchQueries();
    await tick();
    rows = [teams[0]!, saved, teams[2]!]; // lo que la API tiene tras guardar
    send(form);
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(card("Ventas Lima")).toBeVisible();
    releaseRead(); // llega tarde: no devuelve «Ventas» a la tarjeta
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(3)); // y la lectura se repite
    await tick();
    expect(card("Ventas Lima")).toBeVisible();
    expect(screen.queryByText("Ventas", { selector: "span.font-medium" })).not.toBeInTheDocument();
  });

  it("con «Cargar más» en vuelo: se cancela, no pisa lo guardado y nada se vuelve a pedir", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [teams[0]!], next: "abc" } },
      [`${LIST}?cursor=abc`]: { status: 200, body: { results: [sales], next: "def" } },
      [`${LIST}?cursor=def`]: list([teams[2]!]),
      [EDIT("t2")]: { status: 200, body: { ...sales, name: "Ventas Lima" } },
    });
    renderApp(ui());
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    await screen.findByText("Ventas", { selector: "span.font-medium" });
    const form = await opened();
    fill(form, { name: "Ventas Lima" });
    const more = screen.getByRole("button", { name: "Cargar más" });
    const release = hold(api); // la página 3 sale con la lista de antes de guardar
    fireEvent.click(more);
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(3));
    send(form);
    more.focus(); // el usuario ya está en otra parte: al cerrarse no se le quita el foco
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    release();
    await tick();
    expect(card("Ventas Lima")).toBeVisible(); // en la página 2, y lo que llega tarde no lo pisa
    expect(more).toHaveAttribute("aria-disabled", "false"); // hay que pulsarlo otra vez
    expect(calls(api, "GET")).toHaveLength(3); // un «Cargar más» cancelado no se repite
    expect(more).toHaveFocus();
  });

  it("si la API responde con otro equipo, no se da por guardado: se dice y la lista se vuelve a pedir", async () => {
    const other = { ...teams[2]!, name: "Zeta" };
    const api = mockApi({ [LIST]: list(), [EDIT("t2")]: { status: 200, body: other } });
    renderApp(ui());
    const form = await opened();
    send(form);
    expect(await within(form).findByRole("alert")).toHaveTextContent("Algo salió mal");
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    expect(card("Ventas")).toBeVisible();
    expect(screen.queryByText("Zeta")).not.toBeInTheDocument();
  });

  it("lo que se edita queda fijado al abrir, aunque la lista cambie debajo", async () => {
    let rows = teams;
    mockApi({ [LIST]: () => list(rows) });
    const view = renderApp(ui());
    const form = await opened("Soporte");
    rows = [{ ...teams[0]!, name: "Soporte B", description: "Posventa" }, sales, teams[2]!];
    void view.client.refetchQueries();
    await screen.findByText("Soporte B", { selector: "span.font-medium" }); // la tarjeta sigue a la lista
    expect(screen.getByRole("form", { name: "Editar Soporte" })).toBe(form); // el formulario, no
    expect(field(form, "name")).toHaveValue("Soporte");
    expect(field(form, "description")).toHaveValue(""); // un campo vacío seguiría a la fila
    expect(within(form).getByText("Editar Soporte")).toBeVisible();
  });

  it("un 404 cierra el formulario, lo explica la lista y la vuelve a pedir; abrir otro retira el aviso", async () => {
    let rows = teams;
    const api = mockApi({
      [LIST]: () => list(rows),
      [EDIT("t2")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const form = await opened();
    rows = [teams[0]!, teams[2]!]; // ya no está
    card("Ventas").tabIndex = -1; // como la primera fila que trae «Cargar más», que admite el foco
    card("Ventas").focus(); // el foco está en la tarjeta, no en el formulario: desaparece con ella
    fireEvent.submit(form);
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent("El equipo Ventas ya no existe. Revisa la lista.");
    await waitFor(() =>
      expect(
        screen.queryByText("Ventas", { selector: "span.font-medium" }),
      ).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
    fireEvent.click(trigger("Cobranza")); // otra acción: el aviso anterior ya no aplica
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("un 404 cierra el formulario aunque el equipo siga en la lista, y no mueve el foco de otra parte", async () => {
    const api = mockApi({
      [LIST]: list(),
      [EDIT("t2")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const form = await opened();
    send(form);
    const create = screen.getByRole("button", { name: "Crear equipo" });
    create.focus(); // el usuario ya está en otra parte
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    expect(await screen.findByRole("alert")).toHaveTextContent("El equipo Ventas ya no existe");
    await tick();
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(trigger()).toBeVisible(); // la tarjeta sigue: la lista aún la trae
    expect(create).toHaveFocus();
    fireEvent.click(create); // abrir «Crear equipo» también retira el aviso
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("Enter mantenido no vuelve a pulsar: ni reabre el formulario ni reenvía", async () => {
    const api = mockApi({ [LIST]: list(), [EDIT("t2")]: { status: 500, body: { code: "X" } } });
    renderApp(ui());
    await screen.findByRole("list", { name: "Equipos" });
    expect(fireEvent.keyDown(trigger(), { key: "Enter", repeat: true })).toBe(false);
    const form = await opened();
    send(form);
    await within(form).findByRole("alert");
    const held = fireEvent.keyDown(within(form).getByRole("button", { name: "Guardar" }), {
      key: "Enter",
      repeat: true,
    });
    expect(held).toBe(false);
    expect(fireEvent.keyDown(field(form, "name"), { key: "a", repeat: true })).toBe(true);
    expect(calls(api, "PATCH")).toHaveLength(1);
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    fireEvent.click(trigger()); // al reabrir tras un fallo, el error no se arrastra
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
