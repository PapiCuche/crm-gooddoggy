import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { loginPath, safeNext } from "@/lib/next-path";
import { mockApi, renderApp } from "@/test-utils";

import { LoginForm } from "./login-form";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const SESSION = "GET /api/v1/auth/session/";
const LOGIN = "POST /api/v1/auth/login/";
const anonymous = { status: 401, body: { code: "NOT_AUTHENTICATED" } };
const user = { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" };

function fill(email: string, password: string) {
  fireEvent.change(screen.getByLabelText("Correo"), { target: { value: email } });
  fireEvent.change(screen.getByLabelText("Contraseña"), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: /Entrar/ }));
}

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  router.replace.mockReset();
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("LoginForm", () => {
  it("envía las credenciales con CSRF y entra al destino", async () => {
    const api = mockApi({ [SESSION]: anonymous, [LOGIN]: { status: 200, body: { user } } });
    const { client } = renderApp(<LoginForm next="/o/acme" />);
    client.setQueryData(["leído sin sesión"], true);
    fill("  ana@acme.pe ", " clave con espacios ");
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/o/acme"));
    expect(client.getQueryData(["leído sin sesión"])).toBeUndefined(); // la caché no sobrevive
    const [, init] = api.mock.calls.find(([url]) => String(url).endsWith("/login/")) ?? [];
    expect(JSON.parse(String(init?.body))).toEqual({
      email: "ana@acme.pe", // el correo sin espacios exteriores
      password: " clave con espacios ", // la contraseña, tal cual
    });
    expect(new Headers(init?.headers).get("X-CSRFToken")).toBe("token-de-prueba");
    expect(init?.credentials).toBe("same-origin");
    expect(screen.getByRole("button")).toBeDisabled(); // hasta que la navegación la sustituye
  });

  it("un rechazo se anuncia sin decir qué falló y deja reintentar", async () => {
    mockApi({
      [SESSION]: anonymous,
      [LOGIN]: { status: 401, body: { code: "INVALID_CREDENTIALS" } },
    });
    renderApp(<LoginForm next="/o" />);
    screen.getByLabelText("Contraseña").focus(); // ya está en otro campo
    fill("ana@acme.pe", "mala");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El correo o la contraseña no son correctos.",
    );
    expect(screen.getByRole("button", { name: /Entrar/ })).toBeEnabled();
    expect(screen.getByLabelText("Contraseña")).toHaveValue("mala"); // no borra lo escrito
    expect(screen.getByLabelText("Contraseña")).toHaveFocus(); // ni la consulta de sesión se lo quita
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("pide lo que falta sin llamar a la API y lleva el foco al primer campo vacío", async () => {
    const api = mockApi({ [SESSION]: anonymous });
    renderApp(<LoginForm next="/o" />);
    const [email, password] = [
      screen.getByLabelText("Correo"),
      screen.getByLabelText("Contraseña"),
    ];
    await waitFor(() => expect(email).toHaveFocus()); // sin sesión: listo para escribir
    fill("", "");
    expect(email).toHaveAccessibleDescription("Escribe tu correo.");
    expect(password).toBeInvalid();
    expect(email).toHaveFocus(); // no se queda en el botón
    fireEvent.input(email, { target: { value: "ana@acme.pe" } });
    expect(email).toBeValid(); // corregido: el aviso se retira sin esperar a otro envío
    expect(password).toBeInvalid();
    fireEvent.click(screen.getByRole("button", { name: "Entrar" })); // la flecha no se lee
    expect(password).toHaveFocus();
    expect(api).toHaveBeenCalledTimes(1); // solo la consulta de sesión
  });

  it("los campos sirven a un gestor de contraseñas y no admiten más que el contrato", () => {
    mockApi({ [SESSION]: anonymous });
    renderApp(<LoginForm next="/o" />);
    const [email, password] = [
      screen.getByLabelText("Correo"),
      screen.getByLabelText("Contraseña"),
    ];
    expect(document.forms[0]).toHaveAttribute("method", "post"); // sin JavaScript, nada a la URL
    expect(email).toHaveAttribute("autocomplete", "username");
    expect(email).toHaveAttribute("maxlength", "254");
    expect(password).toHaveAttribute("type", "password");
    expect(password).toHaveAttribute("autocomplete", "current-password");
    expect(password).toHaveAttribute("maxlength", "1024");
  });

  it("muestra un mensaje propio para la red caída y para el límite de intentos", async () => {
    let reply: Parameters<typeof mockApi>[0][string] = {
      status: 429,
      body: { code: "RATE_LIMITED" },
      headers: { "Retry-After": "61" },
    };
    mockApi({ [SESSION]: anonymous, [LOGIN]: () => reply as { status: number } });
    renderApp(<LoginForm next="/o" />);
    fill("ana@acme.pe", "x");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Demasiados intentos. Podrás volver a probar en 2 minutos.", // la espera, hacia arriba
    );
    reply = { status: 429, body: { code: "RATE_LIMITED" } }; // sin cabecera: el mensaje general
    fireEvent.click(screen.getByRole("button", { name: /Entrar/ }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Espera un momento"));
    reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    fireEvent.click(screen.getByRole("button", { name: /Entrar/ }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Algo salió mal"));
  });

  it("dos envíos seguidos hacen un solo intento", async () => {
    const api = mockApi({ [SESSION]: anonymous, [LOGIN]: { status: 200, body: { user } } });
    renderApp(<LoginForm next="/o" />);
    fill("ana@acme.pe", "clave");
    expect(fireEvent.submit(document.forms[0] as HTMLFormElement)).toBe(false); // nunca nativo
    await waitFor(() => expect(router.replace).toHaveBeenCalled());
    expect(api.mock.calls.filter(([url]) => String(url).endsWith("/login/"))).toHaveLength(1);
  });

  it("un servidor que no contesta no deja el formulario ocupado para siempre", async () => {
    mockApi({ [SESSION]: anonymous });
    const pending = vi.fn(() => new Promise<Response>(() => {})); // nunca responde
    const answered = vi.mocked(fetch);
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
      String(input).endsWith("/login/") ? pending() : answered(input, init),
    );
    renderApp(<LoginForm next="/o" />);
    await waitFor(() => expect(screen.getByLabelText("Correo")).toHaveFocus());
    vi.useFakeTimers();
    try {
      fill("ana@acme.pe", "clave");
      expect(screen.getByRole("button")).toBeDisabled();
      await act(() => vi.advanceTimersByTimeAsync(29_000));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // aún espera
      await act(() => vi.advanceTimersByTimeAsync(1_500));
      expect(screen.getByRole("alert")).toHaveTextContent("tarda demasiado");
      expect(screen.getByRole("button", { name: "Entrar" })).toBeEnabled(); // puede reintentar
      pending.mockResolvedValueOnce(new Response("{}", { status: 500 })); // ahora contesta
      fireEvent.click(screen.getByRole("button", { name: "Entrar" }));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // el aviso anterior se retira
      await act(() => vi.advanceTimersByTimeAsync(31_000));
      expect(screen.getByRole("alert")).toHaveTextContent("Algo salió mal"); // y no "tarda"
    } finally {
      vi.useRealTimers();
    }
    expect(pending).toHaveBeenCalledTimes(2);
  });

  it("sin red lo dice enseguida y no deja el intento en cola", async () => {
    mockApi({ [SESSION]: anonymous });
    renderApp(<LoginForm next="/o" />);
    onlineManager.setOnline(false); // el navegador avisó de que no hay red
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("sin red")));
    fill("ana@acme.pe", "clave");
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay conexión");
    expect(fetch).toHaveBeenCalledTimes(1); // el intento salió: no espera a que vuelva la red
  });

  it("con la sesión ya abierta va directo al destino", async () => {
    mockApi({ [SESSION]: { status: 200, body: { user } } });
    renderApp(<LoginForm next="/o/acme" />);
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/o/acme"));
    expect(screen.getByRole("button")).toBeDisabled(); // no pide la clave a quien ya entró
    expect(screen.getByLabelText("Correo")).not.toHaveFocus(); // ni le roba el foco
  });
});

describe("safeNext", () => {
  it.each(["/o", "/o/acme", "/o/acme/clientes?vista=2", "/o/acme#notas", "/loginx", "/o//x"])(
    "acepta %s",
    (path) => {
      expect(safeNext(path)).toBe(path);
    },
  );

  it.each([
    undefined,
    "",
    "o/acme",
    "//", // ni siquiera se puede resolver
    "//evil.example",
    "/\\evil.example",
    "https://evil.example/o",
    "javascript:alert(1)",
    "/o\\..\\x",
    "/o\n/x",
    "/o\u007f/x",
    "/o\u0085/x",
    "/o\u2028/x",
    "/\u00a0/evil.example",
    // Rutas de este origen que, resueltos sus puntos, el navegador lee como otro sitio.
    "/.//evil.example",
    "/..//evil.example/x",
    "/o/..//evil.example",
    "/x/.././/evil.example",
    "/%2e%2e//evil.example",
    "/%2E//evil.example",
    // Lo que no es una página, también tras resolver los puntos.
    "/login",
    "/login?next=/o",
    "/login/",
    "/Login",
    // Ni las páginas de recuperación, con el enlace de otra cuenta en el fragmento o sin él.
    "/restablecer",
    "/restablecer#" + "a".repeat(43),
    "/o/../Restablecer?x=1#" + "a".repeat(43),
    "/recuperar",
    "/%72establecer#" + "a".repeat(43), // Next abre la misma página con una letra codificada
    "/%6Cogin?next=/o",
    "/o/../login",
    "/%2e/login?next=/o",
    "/api/v1/me/organizations/",
    "/o/../api/v1/auth/logout/",
    "/_next/static/x.js",
    ["/o", "/x"],
  ])("rechaza %j y usa el destino por defecto", (path) => {
    expect(safeNext(path)).toBe("/o");
  });

  it("devuelve la ruta ya resuelta, que es la que el navegador abriría", () => {
    expect(safeNext("/o/acme/../beta?x=1")).toBe("/o/beta?x=1");
    expect(safeNext("/o/./acme")).toBe("/o/acme");
  });

  it("loginPath codifica el destino", () => {
    expect(loginPath("/o/acme?x=1&y=2")).toBe("/login?next=%2Fo%2Facme%3Fx%3D1%26y%3D2");
  });
});
