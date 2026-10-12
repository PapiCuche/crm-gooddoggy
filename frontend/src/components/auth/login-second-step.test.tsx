import { focusManager, onlineManager } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockApi, renderApp } from "@/test-utils";

import { LoginForm } from "./login-form";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const SESSION = "GET /api/v1/auth/session/";
const LOGIN = "POST /api/v1/auth/login/";
const STEP = "POST /api/v1/auth/login/mfa/";
const anonymous = { status: 401, body: { code: "NOT_AUTHENTICATED" } };
const pending = { status: 202, body: { mfa_required: true } };
const user = { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" };
const entered = { status: 200, body: { user } };
const wrong = {
  status: 400,
  body: { code: "VALIDATION_ERROR", fields: { code: [{ code: "invalid", message: "x" }] } },
};
type Routes = Parameters<typeof mockApi>[0];

// La contraseña correcta de una cuenta con segundo factor: la pantalla llega al paso del código.
async function toSecondStep(routes: Routes) {
  const api = mockApi({ [SESSION]: anonymous, [LOGIN]: pending, ...routes });
  const view = renderApp(<LoginForm next="/o/acme" />);
  fireEvent.change(screen.getByLabelText("Correo"), { target: { value: "ana@acme.pe" } });
  fireEvent.change(screen.getByLabelText("Contraseña"), { target: { value: "clave" } });
  fireEvent.click(screen.getByRole("button", { name: /Entrar/ }));
  const field = await screen.findByLabelText("Código");
  return { api, field, ...view };
}

function send(code: string) {
  fireEvent.change(screen.getByLabelText("Código"), { target: { value: code } });
  fireEvent.click(screen.getByRole("button", { name: "Verificar y entrar" }));
}

const sent = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.filter(([url]) => String(url).endsWith("/login/mfa/"));
const asked = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.filter(([url]) => String(url).endsWith("/session/")).length;
// La ventana vuelve a verse y la red vuelve: lo que hace que TanStack Query pregunte otra vez.
async function comesBack() {
  for (const on of [false, true]) {
    act(() => focusManager.setFocused(on));
    act(() => onlineManager.setOnline(on));
  }
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
}

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  router.replace.mockReset();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  focusManager.setFocused(undefined);
  onlineManager.setOnline(true);
});

describe("el segundo paso del acceso", () => {
  it("tras la contraseña pide el código, sin navegar, y con él entra al destino", async () => {
    const { api, field, client } = await toSecondStep({ [STEP]: entered });
    expect(router.replace).not.toHaveBeenCalled(); // todavía no hay sesión
    expect(screen.queryByLabelText("Contraseña")).toBeNull(); // la contraseña ya no está
    expect(field).toHaveFocus();
    expect(field).toHaveAttribute("autocomplete", "one-time-code");
    expect(screen.getByText(/verificación en dos pasos/)).toBeInTheDocument();
    client.setQueryData(["leído sin sesión"], true);
    send("  123 456 ");
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/o/acme"));
    expect(client.getQueryData(["leído sin sesión"])).toBeUndefined(); // la caché no sobrevive
    const init = sent(api)[0]?.[1];
    expect(JSON.parse(String(init?.body))).toEqual({ code: "123 456" }); // sin espacios alrededor
    expect(new Headers(init?.headers).get("X-CSRFToken")).toBe("token-de-prueba");
    expect(location.search).toBe(""); // ni el código ni la contraseña en la URL
    expect(localStorage.length + sessionStorage.length).toBe(0); // ni guardados
  });

  it("dos pulsaciones envían una sola petición, y después de entrar ninguna más", async () => {
    const { api } = await toSecondStep({ [STEP]: entered });
    fireEvent.change(screen.getByLabelText("Código"), { target: { value: "123456" } });
    const button = screen.getByRole("button", { name: "Verificar y entrar" });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(router.replace).toHaveBeenCalledTimes(1));
    // Otra petición llegaría con la cookie anterior y la API cerraría la sesión recién abierta.
    fireEvent.submit(screen.getByLabelText("Código").closest("form")!);
    await act(() => new Promise((resolve) => setTimeout(resolve, 0))); // una petición no sale en el acto
    expect(sent(api)).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Verificando…" })).toHaveAttribute("aria-disabled");
    fireEvent.click(screen.getByRole("button", { name: "Volver a la contraseña" }));
    expect(screen.getByLabelText("Código")).toBeInTheDocument(); // ni se vuelve: está entrando
  });

  it("un código que no vale se dice en el campo y deja probar otro", async () => {
    let reply: { status: number; body: unknown } = wrong;
    const { api, field } = await toSecondStep({ [STEP]: () => reply });
    fireEvent.click(screen.getByRole("button", { name: "Verificar y entrar" }));
    expect(field).toHaveAccessibleDescription("Escribe el código."); // vacío: no se envía
    expect(sent(api)).toHaveLength(0);
    send("000000");
    await waitFor(() =>
      expect(field).toHaveAccessibleDescription(
        "Ese código no vale. Prueba con el que muestra ahora tu aplicación, o con otro de tus códigos de respaldo.",
      ),
    );
    expect(field).toHaveFocus();
    const { selectionStart, selectionEnd } = field as HTMLInputElement;
    expect([selectionStart, selectionEnd]).toEqual([0, 6]); // seleccionado: escribir lo sustituye
    expect(screen.getAllByRole("alert")).toHaveLength(1); // el del campo, sin fallo general
    expect(router.replace).not.toHaveBeenCalled();
    fireEvent.input(field, { target: { value: "1" } });
    expect(field).not.toHaveAccessibleDescription(); // al corregir, el aviso se va
    reply = entered;
    send("123456");
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/o/acme"));
    expect(sent(api)).toHaveLength(2);
  });

  it("si el acceso a medias ya no vale, vuelve a la contraseña y lo dice", async () => {
    const { api } = await toSecondStep({
      [STEP]: { status: 401, body: { code: "MFA_LOGIN_NOT_STARTED" } },
    });
    const asked = api.mock.calls.filter(([url]) => String(url).endsWith("/session/")).length;
    send("123456");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El acceso caducó o dejó de valer. Escribe tu contraseña otra vez.",
    );
    expect(screen.getByLabelText("Contraseña")).toBeInTheDocument();
    expect(screen.queryByLabelText("Código")).toBeNull();
    expect(screen.getByRole("button", { name: /Entrar/ })).toBeEnabled(); // se puede empezar otra
    // Por si una respuesta que no se esperó abrió la sesión: se vuelve a preguntar.
    await waitFor(() =>
      expect(
        api.mock.calls.filter(([url]) => String(url).endsWith("/session/")).length,
      ).toBeGreaterThan(asked),
    );
    expect(router.replace).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Correo")).toHaveValue("ana@acme.pe"); // el correo sigue ahí
    expect(screen.getByLabelText("Contraseña")).toHaveValue(""); // la contraseña no se guardó
    expect(screen.getByLabelText("Contraseña")).toHaveFocus();
    fireEvent.change(screen.getByLabelText("Contraseña"), { target: { value: "clave" } });
    fireEvent.click(screen.getByRole("button", { name: /Entrar/ }));
    expect(screen.queryByRole("alert")).toBeNull(); // el aviso se retira al enviar, no al contestar
    expect(await screen.findByLabelText("Código")).toBeInTheDocument(); // y el aviso ya no está
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("el límite de intentos dice cuánto esperar, en minutos o en horas", async () => {
    let wait = "61";
    await toSecondStep({
      [STEP]: () => ({
        status: 429,
        body: { code: "RATE_LIMITED" },
        headers: { "Retry-After": wait },
      }),
    });
    send("123456");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Demasiados intentos. Podrás volver a probar en 2 minutos.",
    );
    wait = "86400"; // el tope de códigos de la cuenta: hasta un día
    send("123456");
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Demasiados intentos. Podrás volver a probar en 24 horas.",
      ),
    );
    wait = "3601";
    send("123456");
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("en 2 horas."));
    wait = "3600"; // una hora justa todavía se dice en minutos
    send("123456");
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("en 60 minutos."));
    expect(screen.getByLabelText("Código")).toBeInTheDocument(); // sigue en el paso del código
  });

  it("un fallo que no es del código se anuncia con el mensaje general", async () => {
    await toSecondStep({ [STEP]: { status: 500, body: { code: "INTERNAL_ERROR" } } });
    send("123456");
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado.");
    expect(screen.getByRole("button", { name: "Verificar y entrar" })).toBeInTheDocument();
  });

  it("sin red lo dice enseguida y no deja el código en cola", async () => {
    const { api } = await toSecondStep({});
    onlineManager.setOnline(false); // el navegador avisó de que no hay red
    api.mockRejectedValue(new TypeError("sin red"));
    send("123456");
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay conexión");
    expect(sent(api)).toHaveLength(1); // salió: no espera a que vuelva la red para enviarse
  });

  it("deja de esperar a un servidor que no contesta", async () => {
    const { api } = await toSecondStep({});
    api.mockImplementation(() => new Promise<Response>(() => {})); // nunca responde
    vi.useFakeTimers();
    send("123456");
    await act(() => vi.advanceTimersByTimeAsync(29_000));
    expect(screen.queryByRole("alert")).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(1_500));
    expect(screen.getByRole("alert")).toHaveTextContent("El servidor tarda demasiado");
    expect(screen.getByRole("button", { name: "Verificar y entrar" })).toBeInTheDocument();
  });

  it("se puede volver a la contraseña a voluntad, sin aviso", async () => {
    await toSecondStep({ [STEP]: entered });
    fireEvent.click(screen.getByRole("button", { name: "Volver a la contraseña" }));
    expect(screen.getByLabelText("Contraseña")).toBeInTheDocument();
    expect(screen.queryByLabelText("Código")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: /Entrar/ })).toBeEnabled();
  });

  it("con un código en vuelo no se vuelve a la contraseña, y su respuesta cuenta", async () => {
    let answer: (value: Response) => void = () => {};
    const { api } = await toSecondStep({});
    const usual = api.getMockImplementation()!;
    api.mockImplementation((input, init) =>
      String(input).endsWith("/login/mfa/")
        ? new Promise<Response>((resolve) => (answer = resolve))
        : usual(input, init),
    );
    const before = asked(api);
    send("123456");
    await waitFor(() => expect(sent(api)).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "Volver a la contraseña" }));
    expect(screen.getByLabelText("Código")).toBeInTheDocument();
    await act(async () => answer(new Response(JSON.stringify({ user }), { status: 200 })));
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/o/acme"));
    expect(asked(api)).toBe(before); // nada salió con la cookie anterior
  });

  // Llevaría la cookie del acceso a medias: si llega con el acceso ya completado, la API borra
  // la cookie de la sesión recién abierta.
  it("en el paso del código no pregunta por la sesión por su cuenta", async () => {
    const { api } = await toSecondStep({ [STEP]: entered });
    const before = asked(api);
    await comesBack();
    expect(asked(api)).toBe(before);
    fireEvent.click(screen.getByRole("button", { name: "Volver a la contraseña" }));
    await waitFor(() => expect(asked(api)).toBe(before + 1)); // al volver, sí: una vez
    await comesBack();
    expect(asked(api)).toBeGreaterThan(before + 1); // y en la contraseña, como siempre
  });
});
