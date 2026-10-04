"use client";

import { notFound } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { AuthCard } from "@/components/auth/auth-card";
import { Button } from "@/components/ui/button";
import { useMeContext } from "@/lib/api/client";
import { apiErrorKey } from "@/lib/api-errors";

import { AppShell } from "./app-shell";
import { SessionActions } from "./session-actions";
import { TenantProvider } from "./tenant-context";

// Guardia de `/o/[orgSlug]`: nada del workspace se pinta hasta que la API dice quién es el
// usuario en esa organización. La URL solo selecciona; quien autoriza es la API.
export function TenantGate({ orgSlug, children }: { orgSlug: string; children: ReactNode }) {
  const t = useTranslations();
  const denies = (code?: number) => code === 401 || code === 403 || code === 404;
  // Una negativa lo deja cerrado hasta que la API vuelve a responder bien: un fallo pasajero
  // posterior no lo reabre con el contexto de antes de la negativa.
  const [closed, setClosed] = useState(false);
  // `gcTime: 0`: al salir de la organización no queda su respuesta en memoria. Cada entrada
  // espera a la API en vez de decidir con lo que respondió en una visita anterior.
  const context = useMeContext(orgSlug, {
    query: {
      gcTime: 0,
      // Al volver a la pestaña solo pregunta con el workspace abierto. La tarjeta de error no se
      // desmonta sola (perdería el foco y sus avisos): ahí pregunta «Reintentar».
      refetchOnWindowFocus: ({ state }) => !closed && !!state.data && !denies(state.error?.status),
    },
  });
  const again = useRef<HTMLButtonElement>(null);
  const [retries, setRetries] = useState(0);
  useEffect(() => {
    // Reintentar desmonta su botón: al terminar, el foco va al botón nuevo o al workspace.
    if (retries) (again.current ?? document.getElementById("workspace"))?.focus();
  }, [retries]);
  const status = context.error?.status;
  // Con el workspace abierto solo lo cierra una respuesta que niega el acceso. Un fallo pasajero
  // al refrescar (red, servidor) deja lo último que dijo la API y lo que haya en pantalla.
  const denied = denies(status);
  if (denied && !closed) setClosed(true);
  if (closed && context.isSuccess) setClosed(false);
  if (context.data && !denied && !closed) {
    return (
      <TenantProvider value={context.data}>
        <AppShell>{children}</AppShell>
      </TenantProvider>
    );
  }
  // Organización inexistente o sin membresía: el mismo 404 que cualquier dirección que no existe.
  if (status === 404) notFound();
  if (context.isPending || context.isFetching || status === 401) {
    // Cargando o reintentando. Sin sesión, `Providers` ya lleva al login: no se muestra un error.
    return (
      <p role="status" className="text-muted flex min-h-dvh items-center justify-center">
        {t("shell.loading")}
      </p>
    );
  }
  return (
    <AuthCard eyebrow={t("shell.unavailableEyebrow")} title={t("shell.unavailableTitle")}>
      <p role="alert">{t(`errors.api.${apiErrorKey(context.error)}`)}</p>
      <Button
        ref={again}
        variant="primary"
        size="lg"
        onClick={() => void context.refetch().then(() => setRetries(retries + 1))}
      >
        {t("errors.retry")}
      </Button>
      <SessionActions />
    </AuthCard>
  );
}
