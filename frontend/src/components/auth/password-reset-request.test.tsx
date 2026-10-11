import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockApi, renderApp } from "@/test-utils";

import { PasswordResetRequest } from "./password-reset-request";

const ASK = "POST /api/v1/auth/password-reset/";
const SENT = "Revisa tu correo.";
const admitted = { status: 204 };
const field = () => screen.getByLabelText("Correo");
const button = () => screen.getByRole("button");
const dom = () => new XMLSerializer().serializeToString(document.body);
const asked = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.filter(([url]) => String(url).endsWith("/password-reset/"));

function ask(email: string) {
  fireEvent.change(field(), { target: { value: email } });
  fireEvent.click(button());
}
// Deja que la respuesta se lea sin dar tiempo a pintarla (dentro de `act`): solo microtareas.
async function arrive(view: ReturnType<typeof renderApp>) {
  const waiting = () => view.client.getMutationCache().getAll().at(-1)?.state.status === "pending";
  for (let tick = 0; tick < 100 && waiting(); tick += 1) await Promise.resolve();
  expect(waiting()).toBe(false);
}

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("PasswordResetRequest", () => {
  it("envía el correo escrito y enseña un único mensaje que no lo repite", async () => {
    const api = mockApi({ [ASK]: admitted });
    renderApp(<PasswordResetRequest />);
    expect(screen.getByText(/Si existe, te enviaremos un enlace/)).toBeVisible();
    ask("  Persona-Unica@Acme.PE ");
    const title = await screen.findByRole("heading", { level: 2, name: SENT });
    expect(title).toHaveFocus(); // el formulario ya no está: el foco va a lo que lo sustituye
    expect(screen.getByText(/Si hay una cuenta con ese correo/)).toHaveTextContent(
      "Vale 60 minutos y solo sirve el último que pidas.",
    );
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument(); // ni se puede volver a enviar
    expect(dom().toLowerCase()).not.toContain("persona-unica"); // no dice de quién es la cuenta
    const [, init] = asked(api)[0] ?? [];
    expect(JSON.parse(String(init?.body))).toEqual({ email: "Persona-Unica@Acme.PE" });
    expect(new Headers(init?.headers).get("X-CSRFToken")).toBe("token-de-prueba");
    expect(asked(api)).toHaveLength(1);
    expect([localStorage.length, sessionStorage.length]).toEqual([0, 0]);
  });

  it("sin correo no llama a la API, lo pide en el campo y retira el aviso al escribir", () => {
    const api = mockApi({ [ASK]: admitted });
    renderApp(<PasswordResetRequest />);
    ask("   ");
    expect(field()).toHaveAccessibleDescription("Escribe tu correo.");
    expect(field()).toHaveFocus(); // no se queda en el botón
    fireEvent.input(field(), { target: { value: "ana@acme.pe" } });
    expect(field()).toBeValid(); // corregido: el aviso se retira sin esperar a otro envío
    expect(field()).not.toHaveAccessibleDescription();
    expect(api).not.toHaveBeenCalled();
  });

  it("lo que la API no acepta como correo se explica en el campo y deja corregir", async () => {
    let reply: { status: number; body?: unknown } = {
      status: 400,
      body: { code: "VALIDATION_ERROR", fields: { email: ["Introduzca una dirección válida."] } },
    };
    const api = mockApi({ [ASK]: () => reply });
    renderApp(<PasswordResetRequest />);
    ask("sin-arroba");
    await waitFor(() => expect(field()).toHaveAccessibleDescription("Escribe un correo válido."));
    expect(field()).toHaveFocus();
    expect(field()).toHaveValue("sin-arroba"); // no borra lo escrito
    expect(screen.getAllByRole("alert")).toHaveLength(1); // solo el del campo
    expect(dom()).not.toContain("Introduzca"); // por `code`, no por el texto de la respuesta
    fireEvent.input(field(), { target: { value: "ana@acme.pe" } });
    expect(field()).toBeValid();
    reply = admitted;
    fireEvent.click(button());
    expect(await screen.findByText(SENT)).toBeVisible();
    expect(asked(api)).toHaveLength(2);
  });

  it("un 400 que no es del correo se dice como fallo del servidor, no en el campo", async () => {
    mockApi({ [ASK]: { status: 400, body: { code: "VALIDATION_ERROR", fields: { otro: [] } } } });
    renderApp(<PasswordResetRequest />);
    ask("ana@acme.pe");
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal");
    expect(field()).toBeValid();
  });

  it("un `code` que esta ruta no contesta se dice como fallo del servidor, no como «no hay cuenta»", async () => {
    let code = "NOT_FOUND";
    mockApi({ [ASK]: () => ({ status: 404, body: { code } }) });
    renderApp(<PasswordResetRequest />);
    ask("ana@acme.pe");
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal");
    for (code of ["NOT_AUTHENTICATED", "PERMISSION_DENIED", "OTRO_CODIGO"]) {
      fireEvent.click(button()); // al reintentar, el aviso anterior se retira
      expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal");
    }
  });

  it("el límite de peticiones dice cuánto esperar; otros errores, el mensaje de su `code`", async () => {
    let reply: Parameters<typeof mockApi>[0][string] = {
      status: 429,
      body: { code: "RATE_LIMITED" },
      headers: { "Retry-After": "3541" },
    };
    mockApi({ [ASK]: () => reply as { status: number } });
    renderApp(<PasswordResetRequest />);
    ask("ana@acme.pe");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Demasiados intentos. Podrás volver a probar en 60 minutos.", // la espera, hacia arriba
    );
    expect(screen.queryByText(SENT)).not.toBeInTheDocument();
    expect(button()).toHaveAttribute("aria-disabled", "false"); // puede reintentar
    reply = { status: 429, body: { code: "RATE_LIMITED" } }; // sin cabecera: el mensaje general
    fireEvent.click(button());
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Espera un momento"));
    // La espera solo es del límite: otro error que traiga `Retry-After` no la enseña.
    reply = { status: 500, body: { code: "INTERNAL_ERROR" }, headers: { "Retry-After": "120" } };
    fireEvent.click(button());
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Algo salió mal"));
    reply = { status: 403, body: { code: "CSRF_FAILED" } };
    fireEvent.click(button());
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("La página caducó"));
  });

  it("pulsar varias veces envía una sola petición y el botón no pierde el foco", async () => {
    let answer: (value: Response) => void = () => {};
    const api = mockApi({});
    api.mockImplementation(() => new Promise<Response>((resolve) => (answer = resolve)));
    renderApp(<PasswordResetRequest />);
    button().focus();
    ask("ana@acme.pe");
    fireEvent.input(field(), { target: { value: "otra@acme.pe" } }); // escribir no lo libera
    fireEvent.click(button());
    expect(fireEvent.submit(document.forms[0] as HTMLFormElement)).toBe(false); // nunca nativo
    expect(button()).toHaveTextContent("Enviando…");
    expect(button()).toHaveAttribute("aria-disabled", "true");
    expect(button()).toBeEnabled(); // `aria-disabled`: conserva el foco
    expect(button()).toHaveFocus();
    expect(document.forms[0]).toHaveAttribute("aria-busy", "true");
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    answer(new Response(null, { status: 500 }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal");
    expect(api).toHaveBeenCalledTimes(1);
    expect(button()).toHaveFocus(); // y sigue ahí para reintentar
  });

  it("entre la llegada de la respuesta y la pantalla que la enseña, otra pulsación no envía", async () => {
    let answer: (value: Response) => void = () => {};
    const api = mockApi({});
    api.mockImplementation(() => new Promise<Response>((resolve) => (answer = resolve)));
    const view = renderApp(<PasswordResetRequest />);
    ask("ana@acme.pe");
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    await act(async () => {
      answer(new Response(null, { status: 500 }));
      await arrive(view);
      expect(button()).toHaveTextContent("Enviando…"); // la respuesta llegó; la pantalla aún no
      fireEvent.click(button());
      fireEvent.submit(document.forms[0] as HTMLFormElement);
    });
    expect(screen.getByRole("alert")).toHaveTextContent("Algo salió mal");
    expect(api).toHaveBeenCalledTimes(1); // lo decide el paso que se ve, no la llegada
  });

  it("un servidor que no contesta no deja el formulario ocupado, y su respuesta tardía no cuenta", async () => {
    const answers: ((value: Response) => void)[] = [];
    const api = mockApi({});
    api.mockImplementation(() => new Promise<Response>((resolve) => answers.push(resolve)));
    renderApp(<PasswordResetRequest />);
    vi.useFakeTimers();
    try {
      ask("ana@acme.pe");
      await act(() => vi.advanceTimersByTimeAsync(29_000));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // aún espera
      await act(() => vi.advanceTimersByTimeAsync(1_500));
      expect(screen.getByRole("alert")).toHaveTextContent("tarda demasiado");
      expect(button()).toHaveTextContent("Enviar enlace"); // puede reintentar
      await act(async () => {
        answers[0]!(new Response(null, { status: 204 })); // la que se dejó de esperar
        await vi.advanceTimersByTimeAsync(10);
      });
      expect(screen.queryByText(SENT)).not.toBeInTheDocument();
      fireEvent.click(button());
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // el aviso anterior se retira
      await act(() => vi.advanceTimersByTimeAsync(10));
      await act(async () => {
        answers[1]!(new Response(null, { status: 204 }));
        await vi.advanceTimersByTimeAsync(10);
      });
      expect(screen.getByText(SENT)).toBeVisible();
      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // enviado: ya no hay espera
    } finally {
      vi.useRealTimers();
    }
    expect(api).toHaveBeenCalledTimes(2);
  });

  it("una respuesta que llega justo antes del plazo cuenta, aunque el plazo venza antes de pintarla", async () => {
    let answer: (value: Response) => void = () => {};
    mockApi({}).mockImplementation(() => new Promise<Response>((resolve) => (answer = resolve)));
    const view = renderApp(<PasswordResetRequest />);
    vi.useFakeTimers();
    try {
      ask("ana@acme.pe");
      await act(() => vi.advanceTimersByTimeAsync(29_999));
      await act(async () => {
        answer(new Response(null, { status: 204 }));
        await arrive(view);
        vi.advanceTimersByTime(1); // el plazo vence con la respuesta leída y sin pintar
      });
      expect(screen.getByText(SENT)).toBeVisible(); // la API la admitió: no pide reintentar
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("sin red lo dice enseguida y no deja la petición en cola", async () => {
    renderApp(<PasswordResetRequest />);
    onlineManager.setOnline(false); // el navegador avisó de que no hay red
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("sin red")));
    ask("ana@acme.pe");
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay conexión");
    expect(fetch).toHaveBeenCalledTimes(1); // salió: no espera a que vuelva la red
  });

  it("el campo es de correo, no admite más que el contrato y nunca va a la URL", () => {
    mockApi({});
    renderApp(<PasswordResetRequest />);
    expect(document.forms[0]).toHaveAttribute("method", "post"); // sin JavaScript, nada a la URL
    expect(field()).toHaveAttribute("type", "email");
    expect(field()).toHaveAttribute("name", "email");
    expect(field()).toHaveAttribute("autocomplete", "email");
    expect(field()).toHaveAttribute("maxlength", "254");
    expect(document.forms[0]).toHaveAttribute("novalidate"); // qué es un correo lo decide la API
  });
});
