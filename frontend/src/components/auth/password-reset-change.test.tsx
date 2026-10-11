import { onlineManager } from "@tanstack/react-query";
import { act, configure, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockApi, renderApp } from "@/test-utils";

import { PasswordResetChange } from "./password-reset-change";

const CHANGE = "POST /api/v1/auth/password-reset/confirm/";
const LINK = "a".repeat(20) + "-_Z" + "9".repeat(20); // la forma de un secreto: 43 caracteres
const OTHER = "b".repeat(43);
const TYPED = "  una frase larga con espacios  ";
const CHANGED = "Contraseña cambiada.";
const INVALID = "Este enlace no vale.";
const changed = { status: 204 };
const refusedLink = {
  status: 400,
  body: { code: "INVALID_RESET_LINK", message: "texto de la API" },
};
const weak = (...codes: string[]) => ({
  status: 400,
  body: {
    code: "VALIDATION_ERROR",
    fields: { password: codes.map((code) => ({ code, message: "texto de la API" })) },
  },
});
const field = () => screen.getByLabelText("Contraseña nueva");
const button = () => screen.getByRole("button");
const dom = () => new XMLSerializer().serializeToString(document.documentElement);
const sent = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls
    .filter(([url]) => String(url).endsWith("/confirm/"))
    .map(([, init]) => JSON.parse(String(init?.body)) as unknown);

function open(fragment: string, ui = <PasswordResetChange />) {
  window.history.replaceState({ deNext: true }, "", `/restablecer?x=1${fragment}`);
  return renderApp(ui);
}
function type(value: string) {
  fireEvent.change(field(), { target: { value } });
  fireEvent.click(button());
}
// Deja que la respuesta se lea sin dar tiempo a pintarla (dentro de `act`): solo microtareas.
async function arrive(view: ReturnType<typeof renderApp>) {
  const status = () => view.client.getMutationCache().getAll().at(-1)?.state.status;
  expect(status()).toBe("pending");
  for (let tick = 0; tick < 100 && status() === "pending"; tick += 1) await Promise.resolve();
  expect(status()).not.toBe("pending");
}

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  vi.restoreAllMocks();
  configure({ reactStrictMode: false });
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
  window.history.replaceState(null, "", "/");
});

describe("PasswordResetChange", () => {
  it("saca el secreto de la barra de direcciones al abrir y no lo deja en ningún sitio", async () => {
    const api = mockApi({});
    const entries = window.history.length;
    const strip = vi.spyOn(window.history, "replaceState");
    open(`#${LINK}`);
    expect(window.location.href).toBe(`${window.location.origin}/restablecer?x=1`);
    expect(window.history.length).toBe(entries); // sustituye su entrada: no queda otra con él
    // Ya, con el estado que hubiera; y después, sin él, que es como se entera el router de Next.
    expect(strip.mock.calls.slice(1)).toEqual([[{ deNext: true }, "", "/restablecer?x=1"]]);
    await Promise.resolve();
    expect(strip.mock.calls.slice(2)).toEqual([[null, "", "/restablecer?x=1"]]);
    expect(field()).toHaveFocus();
    expect(dom()).not.toContain(LINK);
    expect([localStorage.length, sessionStorage.length, api.mock.calls.length]).toEqual([0, 0, 0]);
  });

  it("hasta sacar el secreto de la barra solo dice que abre el enlace: es su primer render", () => {
    window.history.replaceState(null, "", `/restablecer#${LINK}`);
    const strip = vi.spyOn(window.history, "replaceState");
    strip.mockImplementationOnce(() =>
      expect(document.body.textContent).toBe("Abriendo el enlace…"),
    );
    renderApp(<PasswordResetChange />);
    expect(strip).toHaveBeenCalledTimes(1);
  });

  it("cambia la contraseña con el secreto y lo escrito tal cual, y enlaza al acceso", async () => {
    const api = mockApi({ [CHANGE]: changed });
    const view = open(`#${LINK}`);
    type(TYPED);
    const title = await screen.findByRole("heading", { level: 2, name: CHANGED });
    expect(title).toHaveFocus(); // el formulario ya no está: el foco va a lo que lo sustituye
    expect(screen.getByText(/Se cerraron las sesiones que tenías abiertas/)).toBeVisible();
    expect(screen.getByRole("link", { name: "Entrar" })).toHaveAttribute("href", "/login");
    expect(screen.queryByLabelText("Contraseña nueva")).not.toBeInTheDocument();
    expect(sent(api)).toEqual([{ token: LINK, password: TYPED }]); // sin recortar: cuentan
    const [, init] = api.mock.calls.at(-1) ?? [];
    expect(String(api.mock.calls.at(-1)?.[0])).not.toContain(LINK); // en el cuerpo, no en la URL
    expect(new Headers(init?.headers).get("X-CSRFToken")).toBe("token-de-prueba");
    expect(dom()).not.toContain(LINK);
    expect(dom()).not.toContain(TYPED.trim());
    // La caché de escrituras no retiene el secreto ni la contraseña.
    await waitFor(() => expect(view.client.getMutationCache().getAll()).toHaveLength(0));
    expect([localStorage.length, sessionStorage.length]).toEqual([0, 0]);
  });

  it.each([
    ["", "sin fragmento"],
    ["#", "vacío"],
    [`#${LINK.slice(1)}`, "corto"],
    [`#${LINK}a`, "largo"],
    [`#${LINK.slice(1)}%`, "con otro carácter"],
    [`#token=${LINK}`, "con otra forma"],
  ])(
    "un enlace que no tiene la forma de uno (%s: %s) no llama a la API y ofrece pedir otro",
    (hash) => {
      const api = mockApi({});
      open(hash);
      expect(screen.getByRole("heading", { level: 2, name: INVALID })).toHaveFocus();
      expect(screen.getByRole("link", { name: "Pedir otro enlace" })).toHaveAttribute(
        "href",
        "/recuperar",
      );
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
      expect(window.location.hash).toBe(""); // tampoco eso se queda en la barra de direcciones
      expect(api).not.toHaveBeenCalled();
    },
  );

  it("un enlace que la API no acepta se explica sin decir por qué y deja de ofrecerse", async () => {
    const api = mockApi({ [CHANGE]: refusedLink });
    const view = open(`#${LINK}`);
    type(TYPED);
    expect(await screen.findByRole("heading", { level: 2, name: INVALID })).toHaveFocus();
    expect(
      screen.getByText(/ya se haya usado, que haya caducado o que hayas pedido otro/),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "Pedir otro enlace" })).toHaveAttribute(
      "href",
      "/recuperar",
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(dom()).not.toContain("texto de la API"); // por `code`
    expect(sent(api)).toHaveLength(1);
    await waitFor(() => expect(view.client.getMutationCache().getAll()).toHaveLength(0)); // ni así
  });

  it("lo que los validadores rechazan se dice junto al campo, por su código, y el enlace sigue sirviendo", async () => {
    let reply: { status: number; body?: unknown } = weak(
      "password_too_short",
      "password_too_common",
    );
    const api = mockApi({ [CHANGE]: () => reply });
    open(`#${LINK}`);
    button().focus();
    type("corta");
    await waitFor(() =>
      expect(field()).toHaveAccessibleDescription(
        "Es demasiado corta: usa al menos 12 caracteres. Es una contraseña demasiado común.",
      ),
    );
    expect(field()).toHaveFocus();
    expect(field()).toHaveValue("corta"); // no borra lo escrito
    expect(screen.getAllByRole("alert")).toHaveLength(1); // solo el del campo
    expect(dom()).not.toContain("texto de la API");
    reply = weak("password_entirely_numeric", "password_too_similar", "password_too_similar");
    button().focus();
    fireEvent.click(button());
    await waitFor(() =>
      expect(field()).toHaveAccessibleDescription(
        "No puede tener solo números. Se parece demasiado a los datos de tu cuenta.",
      ),
    );
    expect(field()).toHaveFocus(); // cada rechazo devuelve el foco al campo
    reply = weak("constructor", "otro_codigo"); // ni un nombre heredado ni uno desconocido
    fireEvent.click(button());
    await waitFor(() =>
      expect(field()).toHaveAccessibleDescription("Esa contraseña no sirve. Elige otra."),
    );
    fireEvent.input(field(), { target: { value: TYPED } });
    expect(field()).toHaveAccessibleDescription("Al menos 12 caracteres. Los espacios cuentan.");
    reply = changed;
    fireEvent.click(button());
    expect(await screen.findByText(CHANGED)).toBeVisible();
    expect(sent(api).map((body) => (body as { token: string }).token)).toEqual(Array(4).fill(LINK));
  });

  it("sin contraseña no llama a la API; solo espacios sí es una contraseña", async () => {
    const api = mockApi({ [CHANGE]: weak("password_too_short") });
    open(`#${LINK}`);
    button().focus();
    fireEvent.click(button());
    expect(field()).toHaveAccessibleDescription("Escribe una contraseña nueva.");
    expect(field()).toHaveFocus();
    expect(api).not.toHaveBeenCalled();
    type("   ");
    await waitFor(() => expect(sent(api)).toEqual([{ token: LINK, password: "   " }]));
  });

  it("el límite dice cuánto esperar; lo que la ruta no contesta, un fallo del servidor", async () => {
    let reply: Parameters<typeof mockApi>[0][string] = {
      status: 429,
      body: { code: "RATE_LIMITED" },
      headers: { "Retry-After": "841" },
    };
    mockApi({ [CHANGE]: () => reply as { status: number } });
    open(`#${LINK}`);
    type(TYPED);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Podrás volver a probar en 15 minutos.",
    );
    expect(field()).toBeValid();
    const said = async (next: typeof reply, text: string) => {
      reply = next;
      fireEvent.click(button());
      await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(text));
    };
    await said({ status: 429, body: { code: "RATE_LIMITED" } }, "Espera un momento");
    await said({ status: 403, body: { code: "CSRF_FAILED" } }, "Algo salió mal"); // no «Recárgala»
    await said({ status: 404, body: { code: "NOT_FOUND" } }, "Algo salió mal");
    await said(
      { status: 400, body: { code: "VALIDATION_ERROR", fields: { token: [] } } },
      "Algo salió mal",
    );
    await said(
      { status: 500, body: { code: "X" }, headers: { "Retry-After": "60" } },
      "Algo salió mal",
    );
    expect(screen.queryByText(INVALID)).not.toBeInTheDocument(); // el enlace sigue ofrecido
  });

  it("pulsar varias veces envía una sola petición, también entre la respuesta y su pantalla", async () => {
    let answer: (value: Response) => void = () => {};
    const api = mockApi({});
    api.mockImplementation(() => new Promise<Response>((resolve) => (answer = resolve)));
    const view = open(`#${LINK}`);
    button().focus();
    type(TYPED);
    fireEvent.input(field(), { target: { value: "otra cosa escrita" } }); // escribir no lo libera
    fireEvent.click(button());
    expect(fireEvent.submit(document.forms[0] as HTMLFormElement)).toBe(false); // nunca nativo
    expect(button()).toHaveTextContent("Cambiando…");
    expect(button()).toHaveAttribute("aria-disabled", "true");
    expect(button()).toBeEnabled(); // `aria-disabled`: conserva el foco
    expect(button()).toHaveFocus();
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    await act(async () => {
      answer(new Response(null, { status: 500 }));
      await arrive(view);
      expect(button()).toHaveTextContent("Cambiando…"); // la respuesta llegó; la pantalla aún no
      fireEvent.click(button());
    });
    expect(screen.getByRole("alert")).toHaveTextContent("Algo salió mal");
    expect(api).toHaveBeenCalledTimes(1);
  });

  it("sin respuesta a tiempo deja reintentar, y la respuesta tardía no cambia la pantalla", async () => {
    const answers: ((value: Response) => void)[] = [];
    const api = mockApi({});
    api.mockImplementation(() => new Promise<Response>((resolve) => answers.push(resolve)));
    open(`#${LINK}`);
    vi.useFakeTimers();
    try {
      type(TYPED);
      await act(() => vi.advanceTimersByTimeAsync(29_000));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // aún espera
      await act(() => vi.advanceTimersByTimeAsync(1_500));
      expect(screen.getByRole("alert")).toHaveTextContent("tarda demasiado");
      await act(async () => {
        answers[0]!(new Response(null, { status: 204 })); // la que se dejó de esperar
        await vi.advanceTimersByTimeAsync(10);
      });
      expect(screen.queryByText(CHANGED)).not.toBeInTheDocument(); // su respuesta no se vio
      fireEvent.click(button());
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // el aviso anterior se retira
      await act(() => vi.advanceTimersByTimeAsync(10));
      await act(async () => {
        answers[1]!(new Response(JSON.stringify(refusedLink.body), { status: 400 }));
        await vi.advanceTimersByTimeAsync(10);
      });
      expect(screen.getByText(INVALID)).toBeVisible(); // lo que diga la API al reintentar
    } finally {
      vi.useRealTimers();
    }
    expect(api).toHaveBeenCalledTimes(2);
  });

  it("una respuesta que llega justo antes del plazo cuenta, aunque el plazo venza antes de pintarla", async () => {
    let answer: (value: Response) => void = () => {};
    mockApi({}).mockImplementation(() => new Promise<Response>((resolve) => (answer = resolve)));
    const view = open(`#${LINK}`);
    vi.useFakeTimers();
    try {
      type(TYPED);
      await act(() => vi.advanceTimersByTimeAsync(29_999));
      await act(async () => {
        answer(new Response(null, { status: 204 }));
        await arrive(view);
        vi.advanceTimersByTime(1); // el plazo vence con la respuesta leída y sin pintar
      });
      expect(screen.getByText(CHANGED)).toBeVisible(); // cambió: no pide reintentar
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("sin red lo dice enseguida y no deja la petición en cola", async () => {
    open(`#${LINK}`);
    onlineManager.setOnline(false); // el navegador avisó de que no hay red
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("sin red")));
    type(TYPED);
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay conexión");
    expect(fetch).toHaveBeenCalledTimes(1); // salió: no espera a que vuelva la red
  });

  it("otro enlace abierto en la misma pestaña sustituye al anterior sin quedarse en la barra", async () => {
    let answer: (value: Response) => void = () => {};
    const api = mockApi({});
    api.mockImplementation(() => new Promise<Response>((resolve) => (answer = resolve)));
    open(`#${LINK}`);
    type(TYPED);
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    act(() => {
      window.location.hash = OTHER; // solo cambia el fragmento: la página no se recarga
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(window.location.hash).toBe("");
    expect(button()).toHaveTextContent("Cambiar contraseña"); // un formulario nuevo
    expect(field()).toHaveValue(""); // sin lo que se escribió para el otro enlace
    await act(async () => {
      answer(new Response(JSON.stringify(refusedLink.body), { status: 400 })); // la del anterior
      await new Promise((done) => setTimeout(done, 10));
    });
    expect(screen.queryByText(INVALID)).not.toBeInTheDocument(); // ya no cambia esta pantalla
    mockApi({ [CHANGE]: changed });
    type(TYPED);
    expect(await screen.findByText(CHANGED)).toBeVisible();
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls.at(-1)?.[1]?.body))).toMatchObject({
      token: OTHER,
    });
    act(() => {
      window.dispatchEvent(new HashChangeEvent("hashchange")); // sin fragmento: nada que abrir
    });
    expect(screen.getByText(CHANGED)).toBeVisible();
  });

  it("lee el enlace una sola vez aunque React repita sus efectos", () => {
    mockApi({});
    configure({ reactStrictMode: true }); // en la raíz: anidado en `renderApp`, React no los repite
    open(`#${LINK}`);
    expect(field()).toBeVisible(); // la segunda pasada ya no encuentra el fragmento
    expect(screen.queryByText(INVALID)).not.toBeInTheDocument();
  });

  it("el campo sirve a un gestor de contraseñas y la contraseña nunca va a la URL", () => {
    mockApi({});
    open(`#${LINK}`);
    expect(document.forms[0]).toHaveAttribute("method", "post"); // sin JavaScript, nada a la URL
    expect(field()).toHaveAttribute("type", "password");
    expect(field()).toHaveAttribute("name", "password");
    expect(field()).toHaveAttribute("autocomplete", "new-password");
    expect(field()).toHaveAttribute("maxlength", "1024");
    expect(field()).toHaveAccessibleDescription("Al menos 12 caracteres. Los espacios cuentan.");
    expect(document.querySelectorAll("input")).toHaveLength(1); // ningún campo oculto con el secreto
  });
});
