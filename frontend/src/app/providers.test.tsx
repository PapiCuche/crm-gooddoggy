import { useQueryClient } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/http";

import { Providers, sessionEnded, shouldRetry } from "./providers";

describe("política de reintentos", () => {
  it("no repite una respuesta 4xx y reintenta una vez lo demás", () => {
    for (const status of [400, 401, 403, 404, 429, 499]) {
      expect(shouldRetry(0, new ApiError(status, "X"))).toBe(false);
    }
    for (const error of [new ApiError(500, "X"), new ApiError(0, "NETWORK_ERROR"), new Error()]) {
      expect(shouldRetry(0, error)).toBe(true);
      expect(shouldRetry(1, error)).toBe(false);
    }
  });

  it("el cliente de consultas la usa y nunca repite una escritura", () => {
    let options: ReturnType<ReturnType<typeof useQueryClient>["getDefaultOptions"]> = {};
    function Probe() {
      options = useQueryClient().getDefaultOptions();
      return null;
    }
    render(
      <Providers>
        <Probe />
      </Providers>,
    );
    expect(options.queries?.retry).toBe(shouldRetry);
    expect(options.mutations?.retry).toBe(false);
  });
});

describe("sesión terminada", () => {
  const at = (pathname: string, search = "") => ({ pathname, search });

  it("un 401 lleva al login con vuelta a donde estaba", () => {
    const expired = new ApiError(401, "NOT_AUTHENTICATED");
    expect(sessionEnded(expired, at("/o/acme/clientes", "?vista=2"))).toBe(
      "/login?next=%2Fo%2Facme%2Fclientes%3Fvista%3D2",
    );
    expect(sessionEnded(expired, at("/login", "?next=%2Fo"))).toBeNull(); // ya está ahí
    // La página de una invitación entra en la misma página: salir de ella perdería el enlace.
    expect(sessionEnded(expired, at("/invitacion"))).toBeNull();
    expect(sessionEnded(expired, at("/invitacion/"))).toBeNull(); // Next la sirve también
    for (const path of ["/o/acme/invitaciones", "/invitacionx", "/o/invitacion"]) {
      expect(sessionEnded(expired, at(path))).toBe(`/login?next=${encodeURIComponent(path)}`);
    }
  });

  it("ningún otro error saca de la pantalla", () => {
    for (const error of [
      new ApiError(403, "PERMISSION_DENIED"),
      new ApiError(0, "NETWORK_ERROR"),
    ]) {
      expect(sessionEnded(error, at("/o"))).toBeNull();
    }
    expect(sessionEnded(new Error("x"), at("/o"))).toBeNull();
  });
});
