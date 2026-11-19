import { onlineManager } from "@tanstack/react-query";
import { act, configure, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockApi, renderApp } from "@/test-utils";

import { InvitationAccept } from "./invitation-accept";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const PREVIEW = "POST /api/v1/invitations/preview/";
const ACCEPT = "POST /api/v1/invitations/accept/";
const SESSION = "GET /api/v1/auth/session/";
const LOGIN = "POST /api/v1/auth/login/";
const MFA = "POST /api/v1/auth/login/mfa/";
const LOGOUT = "POST /api/v1/auth/logout/";
const LINK = "a".repeat(20) + "-_Z" + "9".repeat(20); // la forma de un secreto: 43 caracteres
const PASSWORD = "  una frase larga 2026 ";
const found = (needs: boolean) => ({
  status: 200,
  body: { organization_name: "Acme", email: "ana@acme.pe", needs_account: needs },
});
const refused = { status: 400, body: { code: "INVALID_INVITATION_LINK" } };
const exists = { status: 409, body: { code: "ACCOUNT_EXISTS" } };
const joined = { status: 200, body: { organization_name: "Acme", account_created: true } };
const member = { status: 200, body: { organization_name: "Acme", account_created: false } };
const user = { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "" };
const signedIn = { status: 200, body: { user } };
const anonymous = { status: 401, body: { code: "NOT_AUTHENTICATED" } };
const stranger = { status: 200, body: { user: { ...user, email: "otro@acme.pe" } } };
const leaveButton = () => screen.getByRole("button", { name: /Salir y entrar|Saliendo/ });
// Las respuestas de salir, cuando la prueba quiera; lo demás, de `routes`.
function heldLogout(routes: Parameters<typeof mockApi>[0]) {
  const outs: ((value: Response) => void)[] = [];
  const api = mockApi(routes);
  const others = api.getMockImplementation()!;
  api.mockImplementation((input, init) =>
    String(input).endsWith("/logout/")
      ? new Promise<Response>((resolve) => outs.push(resolve))
      : others(input, init),
  );
  return { api, outs };
}
const INVALID = "Esta invitación no vale.";
const dom = () => new XMLSerializer().serializeToString(document.documentElement);
const bodies = (api: ReturnType<typeof mockApi>, path: string) =>
  api.mock.calls
    .filter(([url]) => String(url).endsWith(path))
    .map(([, init]) => JSON.parse(String(init?.body)) as unknown);
const type = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

const reply = (body: unknown, status: number) => new Response(JSON.stringify(body), { status });
const create = () =>
  screen.getByRole("button", { name: /Crear cuenta y aceptar|Aceptar invitación|Aceptando/ });
// La vista previa contesta enseguida; cada aceptación, cuando la prueba quiera. Sin `needs`, el
// correo tiene cuenta y la sesión ya es la suya.
function held(needs = true) {
  const answers: ((value: Response) => void)[] = [];
  const api = mockApi({ [PREVIEW]: found(needs), [SESSION]: signedIn });
  const routes = api.getMockImplementation()!;
  api.mockImplementation((input, init) =>
    String(input).endsWith("/accept/")
      ? new Promise<Response>((resolve) => answers.push(resolve))
      : routes(input, init),
  );
  return { api, answers };
}
// Deja que la respuesta se lea sin dar tiempo a pintarla (dentro de `act`): solo microtareas.
// `pending`: sin respuesta, sigue pendiente.
async function arrive(view: ReturnType<typeof renderApp>, pending = false) {
  const status = () => view.client.getMutationCache().getAll().at(-1)?.state.status;
  for (let tick = 0; tick < 100 && status() === "pending"; tick += 1) await Promise.resolve();
  expect(status() === "pending").toBe(pending);
}

function open(fragment: string) {
  window.history.replaceState({ deNext: true }, "", `/invitacion?x=1${fragment}`);
  return renderApp(<InvitationAccept />);
}
async function register(first = "Ana", confirm = PASSWORD) {
  await screen.findByText(/Ese correo todavía no tiene cuenta/);
  fill(first, confirm);
}
// Sin cuenta, rellena y crea; con la sesión de la cuenta, pulsa «Aceptar invitación».
function go(needs: boolean) {
  if (needs) return fill();
  fireEvent.click(screen.getByRole("button", { name: "Aceptar invitación" }));
}
async function login(code?: string) {
  await screen.findByLabelText("Correo");
  type("Correo", "ana@acme.pe");
  type("Contraseña", "clave");
  fireEvent.click(screen.getByRole("button", { name: /Entrar/ }));
  if (!code) return;
  fireEvent.change(await screen.findByLabelText("Código"), { target: { value: code } });
  fireEvent.click(screen.getByRole("button", { name: "Verificar y entrar" }));
}
function fill(first = "Ana", confirm = PASSWORD) {
  type("Nombre", first);
  type("Apellidos (opcional)", " Paz ");
  type("Contraseña", PASSWORD);
  type("Repite la contraseña", confirm);
  fireEvent.click(screen.getByRole("button", { name: "Crear cuenta y aceptar" }));
}

// La consola nunca ve el secreto; el navegador no guarda nada.
let logged: ReturnType<typeof vi.spyOn>[] = [];
beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
  logged = (["log", "info", "warn", "error", "debug"] as const).map((name) =>
    vi.spyOn(console, name),
  );
});
afterEach(() => {
  for (const spy of logged) expect(JSON.stringify(spy.mock.calls)).not.toContain(LINK);
  expect([localStorage.length, sessionStorage.length]).toEqual([0, 0]);
  // Ninguna petición lleva el secreto en la URL, en ningún recorrido.
  const calls = vi.isMockFunction(fetch) ? vi.mocked(fetch).mock.calls : [];
  expect(calls.filter(([url]) => String(url).includes(LINK))).toEqual([]);
  expect(router.replace).not.toHaveBeenCalled(); // nunca navega: perdería el enlace
  router.replace.mockReset();
  vi.restoreAllMocks();
  configure({ reactStrictMode: false });
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

describe("InvitationAccept", () => {
  it("saca el secreto de la barra al abrir y pide la vista previa una vez, en el cuerpo", async () => {
    configure({ reactStrictMode: true }); // los efectos dobles no gastan otro intento
    const api = mockApi({ [PREVIEW]: found(true) });
    window.history.replaceState({ deNext: true }, "", `/invitacion?x=1#${LINK}`);
    const entries = window.history.length;
    const strip = vi.spyOn(window.history, "replaceState");
    const real = History.prototype.replaceState.bind(window.history);
    strip.mockImplementationOnce((...args) => {
      // Hasta sacar el secreto de la barra, solo dice que abre la invitación.
      expect(document.body.textContent).toBe("Abriendo la invitación…");
      real(...args);
    });
    renderApp(<InvitationAccept />);
    expect(window.location.href).toBe(`${window.location.origin}/invitacion?x=1`);
    expect(window.location.hash).toBe("");
    expect(window.history.length).toBe(entries);
    await Promise.resolve();
    expect(strip.mock.calls).toEqual([
      [{ deNext: true }, "", "/invitacion?x=1"],
      [null, "", "/invitacion?x=1"],
    ]);
    expect(await screen.findByText(/Acme te invitó a su espacio/)).toHaveTextContent("ana@acme.pe");
    expect(bodies(api, "/preview/")).toEqual([{ token: LINK }]);
    const [, init] = api.mock.calls[0] ?? [];
    expect(new Headers(init?.headers).get("X-CSRFToken")).toBe("token-de-prueba");
    expect(screen.getByLabelText("Nombre")).toHaveFocus();
    expect(dom()).not.toContain(LINK);
    expect(bodies(api, "/accept/")).toEqual([]); // abrir no acepta
  });

  it.each([
    ["", "Abre el enlace del correo."],
    ["#", "Abre el enlace del correo."],
    ["#corto", INVALID],
    [`#${LINK}x`, INVALID],
  ])("con el fragmento %j no pregunta a la API", (hash, title) => {
    const api = mockApi({});
    open(hash);
    expect(screen.getByRole("heading", { level: 2, name: title })).toHaveFocus();
    expect(api).not.toHaveBeenCalled();
  });

  it("cualquier enlace que no vale, en la vista previa o al aceptar, ve la misma pantalla", async () => {
    mockApi({ [PREVIEW]: refused });
    const early = open(`#${LINK}`);
    await screen.findByRole("heading", { name: INVALID });
    const said = early.container.textContent;
    early.unmount();
    mockApi({ [PREVIEW]: found(true), [ACCEPT]: refused });
    const late = open(`#${LINK}`);
    await register();
    await screen.findByRole("heading", { name: INVALID });
    expect(late.container.textContent).toBe(said);
    expect(said).toContain("Pide a quien te invitó que te envíe otra.");
  });

  it("sin cuenta: la crea con lo escrito, acepta y manda a entrar, sin iniciar sesión", async () => {
    const api = mockApi({ [PREVIEW]: found(true), [ACCEPT]: joined });
    const view = open(`#${LINK}`);
    await register();
    const title = await screen.findByRole("heading", { name: "Ya eres parte de Acme." });
    await waitFor(() => expect(title).toHaveFocus());
    expect(screen.getByText(/Entra con ana@acme.pe y la contraseña que elegiste/)).toBeVisible();
    expect(screen.getByRole("link", { name: "Entrar" })).toHaveAttribute("href", "/login");
    expect(bodies(api, "/accept/")).toEqual([
      { token: LINK, first_name: "Ana", last_name: "Paz", password: PASSWORD },
    ]);
    expect(api.mock.calls.map(([url]) => String(url))).toEqual([
      "/api/v1/invitations/preview/",
      "/api/v1/invitations/accept/",
    ]);
    expect(dom()).not.toContain(PASSWORD.trim());
    await waitFor(() => expect(view.client.getMutationCache().getAll()).toHaveLength(0));
  });

  it("sin cuenta: nombre, contraseña y confirmación se revisan antes de enviar", async () => {
    const api = mockApi({ [PREVIEW]: found(true) });
    open(`#${LINK}`);
    await register(" ", "otra");
    expect(screen.getByLabelText("Nombre")).toHaveFocus();
    expect(screen.getByText("Escribe tu nombre.")).toBeVisible();
    expect(screen.getByText("Las dos contraseñas no coinciden.")).toBeVisible();
    expect(bodies(api, "/accept/")).toEqual([]);
    const confirm = screen.getByLabelText("Repite la contraseña");
    const password = screen.getByLabelText("Contraseña");
    expect([password, confirm].map((field) => field.getAttribute("type"))).toEqual([
      "password",
      "password",
    ]);
    confirm.focus(); // corregir un aviso no lleva el foco al otro campo con aviso
    fireEvent.input(confirm, { target: { value: PASSWORD } });
    expect(screen.queryByText("Las dos contraseñas no coinciden.")).not.toBeInTheDocument();
    expect(confirm).toHaveFocus();
    fireEvent.click(create());
    type("Repite la contraseña", "otra");
    fireEvent.click(create());
    expect(confirm).toHaveAttribute("aria-invalid", "true");
    fireEvent.input(password, { target: { value: "otra" } }); // al cambiar la contraseña, también
    expect(confirm).not.toHaveAttribute("aria-invalid");
  });

  it.each([
    ["password", "Contraseña", "Esa contraseña no sirve. Revisa las reglas y elige otra."],
    ["last_name", "Apellidos (opcional)", "Revisa los apellidos: como mucho 150 caracteres."],
  ])(
    "lo que la API no acepta en %s se dice en su campo, nunca con su texto",
    async (name, label, said) => {
      const fields = { [name]: [{ code: "invalid", message: "texto de la API" }] };
      mockApi({
        [PREVIEW]: found(true),
        [ACCEPT]: { status: 400, body: { code: "VALIDATION_ERROR", fields } },
      });
      open(`#${LINK}`);
      expect(await screen.findByText(/Al menos 12 caracteres/)).toBeVisible(); // las reglas, antes
      await register();
      const field = screen.getByLabelText(label);
      await waitFor(() => expect(field).toHaveFocus());
      expect(field).toHaveAccessibleDescription(said);
      expect(dom()).not.toContain("texto de la API");
      fireEvent.input(field, { target: { value: "otra" } }); // al corregir, el aviso se va
      expect(field).not.toHaveAttribute("aria-invalid");
    },
  );

  it("con cuenta: entra aquí mismo con el segundo paso y acepta al pulsar, con el CSRF nuevo", async () => {
    const api = mockApi({
      [PREVIEW]: found(false),
      [SESSION]: anonymous,
      [LOGIN]: { status: 202, body: { mfa_required: true } },
      [MFA]: () => {
        document.cookie = "csrftoken=token-rotado"; // Django rota el token al abrir la sesión
        return signedIn;
      },
      [ACCEPT]: member,
    });
    open(`#${LINK}`);
    expect(await screen.findByText(/Ese correo ya tiene cuenta. Entra con ella/)).toBeVisible();
    await login("123456");
    const button = await screen.findByRole("button", { name: "Aceptar invitación" });
    await waitFor(() => expect(button).toHaveFocus());
    expect(bodies(api, "/accept/")).toEqual([]); // entrar no acepta
    fireEvent.click(button);
    const title = await screen.findByRole("heading", { name: "Ya eres parte de Acme." });
    await waitFor(() => expect(title).toHaveFocus());
    expect(screen.getByText("Ya puedes trabajar en su espacio.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Ir a mis organizaciones" })).toHaveAttribute(
      "href",
      "/o",
    );
    expect(bodies(api, "/accept/")).toEqual([{ token: LINK }]);
    const [, init] = api.mock.calls.find(([url]) => String(url).endsWith("/accept/")) ?? [];
    expect(new Headers(init?.headers).get("X-CSRFToken")).toBe("token-rotado");
    expect(bodies(api, "/login/")).toEqual([{ email: "ana@acme.pe", password: "clave" }]);
  });

  it("sin cuenta al abrir pero con cuenta al aceptar (409): pide entrar con ella", async () => {
    const temporary = { status: 403, body: { code: "PASSWORD_CHANGE_REQUIRED" } };
    mockApi({ [PREVIEW]: found(true), [ACCEPT]: exists, [SESSION]: anonymous, [LOGIN]: temporary });
    open(`#${LINK}`);
    await register();
    expect(await screen.findByText(/Ese correo ya tiene cuenta. Entra con ella/)).toBeVisible();
    await login();
    expect(await screen.findByRole("alert")).toHaveTextContent("«¿Olvidaste tu contraseña?»");
    // Lo que dice el aviso está a la vista, en otra pestaña: esta guarda el enlace.
    const forgot = screen.getByRole("link", { name: "¿Olvidaste tu contraseña?" });
    expect(forgot).toHaveAttribute("href", "/recuperar");
    expect(forgot).toHaveAttribute("target", "_blank");
  });

  it("con la sesión de otra cuenta: lo dice sin gastar un intento, deja salir y entrar con la buena sin reabrir el enlace", async () => {
    let session: typeof stranger | typeof anonymous = stranger;
    const api = mockApi({
      [PREVIEW]: found(false),
      [SESSION]: () => session,
      [ACCEPT]: member,
      [LOGIN]: signedIn,
      [LOGOUT]: () => {
        session = anonymous;
        return { status: 204 };
      },
    });
    open(`#${LINK}`);
    const said = await screen.findByText(/Esta invitación es para ana@acme.pe/);
    expect(said).toHaveTextContent("Sal de ella y entra con ana@acme.pe.");
    await waitFor(() => expect(said).toHaveFocus());
    expect(bodies(api, "/accept/")).toEqual([]);
    fireEvent.click(leaveButton());
    await login(); // la caché de la otra sesión no cuenta: se pide la clave
    fireEvent.click(await screen.findByRole("button", { name: "Aceptar invitación" }));
    await screen.findByRole("heading", { name: "Ya eres parte de Acme." });
    expect(bodies(api, "/accept/")).toEqual([{ token: LINK }]);
    expect(bodies(api, "/preview/")).toHaveLength(1);
  });

  it("salir: también sin red; varias pulsaciones, un cierre; su error se retira al reintentar; un 401 es haber salido", async () => {
    let session: typeof stranger | typeof anonymous = stranger;
    const { api, outs } = heldLogout({ [PREVIEW]: found(false), [SESSION]: () => session });
    open(`#${LINK}`);
    await screen.findByText(/Esta invitación es para/);
    onlineManager.setOnline(false); // `networkMode: "always"`: no queda en pausa
    try {
      fireEvent.click(leaveButton());
      fireEvent.click(leaveButton()); // antes de que se pinte la primera
      await waitFor(() => expect(outs).toHaveLength(1));
      expect(leaveButton()).toHaveTextContent("Saliendo…");
      await act(async () => outs[0]!(reply({ code: "X" }, 500)));
      expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal");
      fireEvent.click(leaveButton());
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      await waitFor(() => expect(outs).toHaveLength(2));
      session = anonymous;
      await act(async () => outs[1]!(reply(anonymous.body, 401)));
      expect(await screen.findByLabelText("Correo")).toBeVisible();
    } finally {
      onlineManager.setOnline(true);
    }
    expect(api.mock.calls.filter(([url]) => String(url).endsWith("/logout/"))).toHaveLength(2);
  });

  it("una respuesta de salir que llega con otro enlace ya abierto no lo pisa", async () => {
    const { outs } = heldLogout({ [PREVIEW]: found(false), [SESSION]: stranger });
    open(`#${LINK}`);
    await screen.findByText(/Esta invitación es para/);
    fireEvent.click(leaveButton());
    await waitFor(() => expect(outs).toHaveLength(1));
    mockApi({ [PREVIEW]: found(true) });
    await act(async () => {
      window.location.hash = "d".repeat(43);
    });
    await screen.findByLabelText("Nombre");
    await act(async () => outs[0]!(new Response(null, { status: 204 })));
    expect(screen.getByLabelText("Nombre")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Entrar/ })).not.toBeInTheDocument();
  });

  it.each([
    ["sigue abierta: es de otra cuenta", signedIn, /Esta invitación es para ana@acme.pe/],
    ["ya no existe: se vuelve a entrar", anonymous, /Tu sesión terminó/],
  ])("un 409 con la sesión de la cuenta pregunta por ella; si %s", async (_, after, said) => {
    const mixed = { status: 200, body: { user: { ...user, email: "Ana@Acme.PE" } } };
    const sessions = [mixed];
    mockApi({
      [PREVIEW]: found(false),
      [SESSION]: () => sessions.shift() ?? after,
      [ACCEPT]: exists,
    });
    open(`#${LINK}`);
    fireEvent.click(await screen.findByRole("button", { name: "Aceptar invitación" })); // mismo correo
    expect(await screen.findByText(said)).toBeVisible();
    if (after === anonymous) expect(await screen.findByLabelText("Correo")).toBeVisible();
    else expect(leaveButton()).toBeVisible();
  });

  it("mientras se pregunta por la sesión tras un 409, otra pulsación no envía otra aceptación", async () => {
    const asks: ((value: Response) => void)[] = [];
    const api = mockApi({ [PREVIEW]: found(false), [ACCEPT]: exists });
    const others = api.getMockImplementation()!;
    api.mockImplementation((input, init) => {
      if (!String(input).endsWith("/session/")) return others(input, init);
      if (!api.mock.calls.some(([url]) => String(url).endsWith("/accept/"))) {
        return Promise.resolve(reply(signedIn.body, 200)); // la del login, al abrir
      }
      return new Promise<Response>((resolve) => asks.push(resolve));
    });
    open(`#${LINK}`);
    fireEvent.click(await screen.findByRole("button", { name: "Aceptar invitación" }));
    await waitFor(() => expect(asks).toHaveLength(1));
    expect(create()).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(create());
    await act(async () => asks[0]!(reply(signedIn.body, 200)));
    expect(await screen.findByText(/Esta invitación es para/)).toBeVisible();
    expect(bodies(api, "/accept/")).toHaveLength(1);
  });

  it("demasiados intentos: dice cuánto esperar y deja volver a preguntar", async () => {
    const limited = {
      status: 429,
      body: { code: "RATE_LIMITED" },
      headers: { "Retry-After": "600" },
    };
    let answer: typeof limited | ReturnType<typeof found> = limited;
    const api = mockApi({ [PREVIEW]: () => answer });
    open(`#${LINK}`);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Podrás volver a probar en 10 minutos.",
    );
    answer = found(true);
    fireEvent.click(screen.getByRole("button", { name: "Volver a intentar" }));
    expect(await screen.findByLabelText("Nombre")).toBeVisible();
    expect(bodies(api, "/preview/")).toEqual([{ token: LINK }, { token: LINK }]);
  });

  it("tras un intento sin respuesta, un enlace que ya no vale avisa de que pudo aceptarse", async () => {
    const api = mockApi({ [ACCEPT]: refused });
    api.mockImplementationOnce(async () => new Response(JSON.stringify(found(true).body)));
    api.mockImplementationOnce(async () => Promise.reject(new TypeError("sin red")));
    open(`#${LINK}`);
    await register();
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay conexión con el servidor.");
    fireEvent.click(screen.getByRole("button", { name: "Crear cuenta y aceptar" }));
    expect(await screen.findByText(/puede que ya estés dentro/)).toBeVisible();
    expect(screen.getByRole("link", { name: "Entrar" })).toHaveAttribute("href", "/login");
  });

  it.each([true, false])(
    "pulsar varias veces envía una sola aceptación, también entre la respuesta y su pantalla (sin cuenta: %s)",
    async (needs) => {
      const { api, answers } = held(needs);
      const view = open(`#${LINK}`);
      if (needs) await register();
      else {
        await screen.findByRole("button", { name: "Aceptar invitación" });
        go(false);
      }
      expect(create()).toHaveTextContent("Aceptando…");
      expect(create()).toHaveAttribute("aria-disabled", "true");
      fireEvent.click(create());
      await waitFor(() => expect(answers).toHaveLength(1));
      await act(async () => {
        answers[0]!(reply({ code: "X" }, 500));
        await arrive(view);
        expect(create()).toHaveTextContent("Aceptando…"); // la respuesta llegó; la pantalla aún no
        fireEvent.click(create());
      });
      expect(screen.getByRole("alert")).toHaveTextContent("Algo salió mal");
      expect(bodies(api, "/accept/")).toHaveLength(1);
    },
  );

  it("sin respuesta de la vista previa a tiempo deja volver a preguntar; una justo antes del plazo manda", async () => {
    const answers: ((value: Response) => void)[] = [];
    const api = mockApi({});
    api.mockImplementation(() => new Promise<Response>((resolve) => answers.push(resolve)));
    vi.useFakeTimers();
    try {
      const view = open(`#${LINK}`);
      await act(() => vi.advanceTimersByTimeAsync(29_000));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // aún espera
      await act(() => vi.advanceTimersByTimeAsync(1_500));
      expect(screen.getByRole("alert")).toHaveTextContent("tarda demasiado");
      await act(async () => {
        answers[0]!(reply(found(true).body, 200)); // la que se dejó de esperar
        await vi.advanceTimersByTimeAsync(10);
      });
      expect(screen.queryByLabelText("Nombre")).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Volver a intentar" }));
      await act(() => vi.advanceTimersByTimeAsync(29_999));
      expect(screen.getByRole("status")).toHaveFocus(); // el botón ya no está
      await act(async () => {
        answers[1]!(reply(found(true).body, 200));
        await arrive(view);
        vi.advanceTimersByTime(1); // el plazo vence con la respuesta leída y sin pintar: manda ella
      });
      expect(screen.getByLabelText("Nombre")).toHaveFocus();
    } finally {
      vi.useRealTimers();
    }
    expect(api).toHaveBeenCalledTimes(2);
  });

  const limited = { status: 429, body: { code: "RATE_LIMITED" } };
  it.each(
    [true, false].flatMap((needs) => [
      ["sin respuesta a tiempo", needs, null, "tarda demasiado", true] as const,
      [
        "una aceptación justo antes del plazo",
        needs,
        joined,
        "Ya eres parte de Acme.",
        false,
      ] as const,
      ["un rechazo justo antes del plazo", needs, limited, "Espera un momento", false] as const,
    ]),
  )(
    "al aceptar, %s (sin cuenta: %s): se dice, y la duda queda solo sin respuesta",
    async (_, needs, first, said, unsure) => {
      const { answers } = held(needs);
      const view = open(`#${LINK}`);
      await screen.findByRole("button", { name: /Crear cuenta y aceptar|Aceptar invitación/ });
      vi.useFakeTimers();
      try {
        go(needs);
        await act(() => vi.advanceTimersByTimeAsync(29_999));
        expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // aún espera
        await act(async () => {
          if (first) answers[0]!(reply(first.body, first.status));
          await arrive(view, !first);
          vi.advanceTimersByTime(1); // el plazo vence con la respuesta leída y sin pintar
        });
        expect(document.body).toHaveTextContent(said);
        if (first?.status === 200) return;
        if (!first) {
          await act(async () => {
            answers[0]!(reply(joined.body, 200)); // la que se dejó de esperar no cambia la pantalla
            await vi.advanceTimersByTimeAsync(250);
          });
          expect(screen.queryByText("Ya eres parte de Acme.")).not.toBeInTheDocument();
        }
        fireEvent.click(create());
        await act(() => vi.advanceTimersByTimeAsync(10));
        await act(async () => {
          answers[1]!(reply(refused.body, 400));
          await vi.advanceTimersByTimeAsync(10);
        });
        expect(screen.getByText(INVALID)).toBeVisible();
        expect(screen.queryAllByText(/puede que ya estés dentro/)).toHaveLength(unsure ? 1 : 0);
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it("otro enlace abierto con una aceptación en vuelo: manda el nuevo y la respuesta vieja no cuenta", async () => {
    const { api, answers } = held();
    open(`#${LINK}`);
    await register();
    await waitFor(() => expect(answers).toHaveLength(1));
    const other = "c".repeat(43);
    await act(async () => {
      window.location.hash = other;
    });
    expect(await screen.findByLabelText("Nombre")).toHaveValue(""); // la del enlace nuevo
    await act(async () => answers[0]!(reply(joined.body, 200)));
    expect(screen.queryByText("Ya eres parte de Acme.")).not.toBeInTheDocument();
    fill();
    await waitFor(() => expect(answers).toHaveLength(2));
    expect(bodies(api, "/accept/").map((body) => (body as { token: string }).token)).toEqual([
      LINK,
      other,
    ]);
  });

  it("otro enlace abierto en la misma pestaña se lee, se quita y se pregunta", async () => {
    const api = mockApi({ [PREVIEW]: found(true) });
    open(`#${LINK}`);
    await screen.findByLabelText("Nombre");
    const other = "b".repeat(43);
    await act(async () => {
      window.location.hash = other; // el navegador avisa con `hashchange`
    });
    await waitFor(() => expect(bodies(api, "/preview/")).toHaveLength(2));
    expect(bodies(api, "/preview/")[1]).toEqual({ token: other });
    expect(window.location.hash).toBe("");
  });
});
