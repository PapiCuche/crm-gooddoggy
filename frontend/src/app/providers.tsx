"use client";

import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

import { ApiError } from "@/lib/http";
import { loginPath } from "@/lib/next-path";

// Un 4xx es una respuesta, no un fallo pasajero: repetirlo no cambia nada (y un 401 o un 429
// repetidos empeoran las cosas). Solo se reintenta, una vez, lo que no llegó o falló en el servidor.
export function shouldRetry(failures: number, error: unknown): boolean {
  const answered = error instanceof ApiError && error.status >= 400 && error.status < 500;
  return !answered && failures < 1;
}

// Único punto que trata una sesión que no existe o caducó (ADR-014): cualquier lectura o
// escritura que responda 401 lleva al login, con vuelta a donde estaba. En el propio login no
// hay a dónde ir. El cierre de sesión trata su propio 401 (`meta.ownSessionEnd`).
export function sessionEnded(error: unknown, location: Pick<Location, "pathname" | "search">) {
  if (!(error instanceof ApiError) || error.status !== 401) return null;
  if (location.pathname === "/login") return null;
  return loginPath(location.pathname + location.search);
}

export function Providers({ children }: { children: ReactNode }) {
  // Un QueryClient por sesión del navegador (no compartido entre peticiones del servidor).
  const [client] = useState(() => {
    const toLogin = (error: unknown) => {
      const login = sessionEnded(error, window.location);
      if (login) window.location.assign(login); // navegación completa: no queda estado
    };
    return new QueryClient({
      queryCache: new QueryCache({ onError: toLogin }),
      mutationCache: new MutationCache({
        onError: (error, _variables, _context, mutation) =>
          mutation.meta?.ownSessionEnd || toLogin(error),
      }),
      defaultOptions: {
        queries: { staleTime: 30_000, retry: shouldRetry },
        mutations: { retry: false }, // una escritura nunca se repite sola
      },
    });
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
