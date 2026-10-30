import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/http";
import { mockApi, renderApp } from "@/test-utils";

import { isStepUpCancelled, useStepUp } from "./use-step-up";

const STEP_UP = "POST /api/v1/auth/mfa/step-up/";
const OK = { status: 200, body: { step_up_expires_in: 900 } };
const needed = () => new ApiError(403, "STEP_UP_REQUIRED");
type Reply = { status: number; body?: unknown; headers?: Record<string, string> };

let run: (() => Promise<string>) & ReturnType<typeof vi.fn>;
let outcome: { value?: string; error?: unknown; done: boolean };

function Probe() {
  const { withStepUp, dialog } = useStepUp();
  return (
    <>
      <button
        onClick={() => {
          outcome = { done: false };
          withStepUp(run).then(
            (value) => (outcome = { value, done: true }),
            (error: unknown) => (outcome = { error, done: true }),
          );
        }}
      >
        Guardar
      </button>
      {dialog}
    </>
  );
}

const posts = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.filter(
    ([url, init]) => String(url).endsWith("/step-up/") && init?.method === "POST",
  );

async function ask(reply: Reply | (() => Reply) = OK) {
  const api = mockApi({ [STEP_UP]: reply });
  renderApp(<Probe />);
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
  await screen.findByRole("dialog");
  return api;
}
function type(code: string) {
  fireEvent.change(screen.getByLabelText(/^Código/), { target: { value: code } });
  fireEvent.click(screen.getByRole("button", { name: "Verificar" }));
}

beforeAll(() => {
  // jsdom no implementa el `<dialog>` modal: se refleja `open` y el evento `close`.
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.open = false;
    this.dispatchEvent(new Event("close"));
  };
});
beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
  run = vi.fn().mockRejectedValueOnce(needed()).mockResolvedValue("hecho") as typeof run;
});
afterEach(() => vi.unstubAllGlobals());

describe("useStepUp", () => {
  it("una acción que no pide step-up pasa sin diálogo", async () => {
    run = vi.fn().mockResolvedValue("hecho") as typeof run;
    mockApi({});
    renderApp(<Probe />);
    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
    await waitFor(() => expect(outcome.value).toBe("hecho"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("otro error no abre el diálogo y sale tal cual", async () => {
    const other = new ApiError(403, "PERMISSION_DENIED");
    run = vi.fn().mockRejectedValue(other) as typeof run;
    renderApp(<Probe />);
    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
    await waitFor(() => expect(outcome.error).toBe(other));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("pide el código, lo envía y reintenta una sola vez", async () => {
    const api = await ask();
    const input = screen.getByLabelText("Código de la aplicación");
    expect(screen.getByRole("dialog", { name: "Confirma que eres tú" })).toBeVisible();
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute("inputmode", "numeric");
    expect(input).toHaveAttribute("autocomplete", "one-time-code");
    type("123 456");
    await waitFor(() => expect(outcome.value).toBe("hecho"));
    expect(JSON.parse(String(posts(api)[0]?.[1]?.body))).toEqual({ code: "123456" });
    expect(run).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("un segundo STEP_UP_REQUIRED tras verificar no vuelve a preguntar", async () => {
    const again = needed();
    run = vi.fn().mockRejectedValue(again) as typeof run;
    const api = await ask();
    type("123456");
    await waitFor(() => expect(outcome.error).toBe(again));
    expect(run).toHaveBeenCalledTimes(2);
    expect(posts(api)).toHaveLength(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("un código incorrecto se dice en el diálogo, que sigue abierto", async () => {
    let reply: Reply = {
      status: 400,
      body: { code: "VALIDATION_ERROR", fields: { code: [{ code: "invalid", message: "x" }] } },
    };
    const api = await ask(() => reply);
    type("000000");
    await waitFor(() =>
      expect(screen.getByLabelText("Código de la aplicación")).toHaveAccessibleDescription(
        /Ese código no vale/,
      ),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/Ese código no vale/);
    expect(run).toHaveBeenCalledTimes(1);
    expect(outcome.done).toBe(false);
    reply = OK; // y se puede volver a intentar
    type("123456");
    await waitFor(() => expect(outcome.value).toBe("hecho"));
    expect(posts(api)).toHaveLength(2);
  });

  it("sin escribir nada no envía", async () => {
    const api = await ask();
    fireEvent.click(screen.getByRole("button", { name: "Verificar" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Escribe el código.");
    expect(posts(api)).toHaveLength(0);
  });

  it("el límite de intentos se dice con el tiempo que falta", async () => {
    await ask({ status: 429, body: { code: "RATE_LIMITED" }, headers: { "Retry-After": "600" } });
    type("123456");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Demasiados intentos. Podrás volver a probar en 10 minutos.",
    );
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("cancelar rechaza con un error reconocible y no reintenta", async () => {
    const api = await ask();
    fireEvent.click(screen.getByRole("button", { name: "Cancelar" }));
    await waitFor(() => expect(isStepUpCancelled(outcome.error)).toBe(true));
    expect(run).toHaveBeenCalledTimes(1);
    expect(posts(api)).toHaveLength(0);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Escape cancela", async () => {
    await ask();
    act(() => {
      screen.getByRole("dialog").dispatchEvent(new Event("close")); // lo que hace Escape
    });
    await waitFor(() => expect(isStepUpCancelled(outcome.error)).toBe(true));
  });

  it("con un código de respaldo: otro campo, sin espacios ni guiones", async () => {
    const api = await ask();
    fireEvent.click(screen.getByRole("button", { name: "Usar un código de respaldo" }));
    const input = screen.getByLabelText("Código de respaldo");
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute("inputmode", "text");
    type("aaaa-bbbb cccc");
    await waitFor(() => expect(outcome.value).toBe("hecho"));
    expect(JSON.parse(String(posts(api)[0]?.[1]?.body))).toEqual({ code: "aaaabbbbcccc" });
  });

  it("volver al código de la aplicación borra lo escrito", async () => {
    await ask();
    fireEvent.click(screen.getByRole("button", { name: "Usar un código de respaldo" }));
    fireEvent.change(screen.getByLabelText("Código de respaldo"), { target: { value: "ABC" } });
    fireEvent.click(screen.getByRole("button", { name: "Usar el código de la aplicación" }));
    expect(screen.getByLabelText("Código de la aplicación")).toHaveValue("");
  });

  it("sin segundo factor avisa y lleva a activarlo", async () => {
    await ask({ status: 409, body: { code: "MFA_NOT_ENABLED" } });
    type("123456");
    const link = await screen.findByRole("link", { name: "Ir a la seguridad de la cuenta" });
    expect(link).toHaveAttribute("href", "/cuenta/seguridad");
    expect(screen.getByRole("status")).toHaveTextContent(/aún no la tiene/);
    expect(screen.queryByLabelText(/^Código/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Cerrar" }));
    await waitFor(() => expect(isStepUpCancelled(outcome.error)).toBe(true));
  });

  it("dos acciones a la vez comparten un solo diálogo", async () => {
    mockApi({ [STEP_UP]: OK });
    const second = vi.fn().mockRejectedValueOnce(needed()).mockResolvedValue("otra");
    function Two() {
      const { withStepUp, dialog } = useStepUp();
      return (
        <>
          <button onClick={() => void withStepUp(run)}>A</button>
          <button onClick={() => void withStepUp(second)}>B</button>
          {dialog}
        </>
      );
    }
    renderApp(<Two />);
    fireEvent.click(screen.getByRole("button", { name: "A" }));
    fireEvent.click(screen.getByRole("button", { name: "B" }));
    await screen.findByRole("dialog");
    await waitFor(() => expect(second).toHaveBeenCalledTimes(1));
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    type("123456");
    await waitFor(() => expect(second).toHaveBeenCalledTimes(2));
    expect(run).toHaveBeenCalledTimes(2);
  });
});
