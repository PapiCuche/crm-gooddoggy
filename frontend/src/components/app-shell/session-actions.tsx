"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import { useAuthLogout } from "@/lib/api/client";
import { apiErrorKey } from "@/lib/api-errors";

const ACTION =
  "hover:bg-surface-raised active:bg-surface-raised flex min-h-11 w-full items-center rounded-md px-3 py-1.5 text-left text-sm transition-colors aria-disabled:opacity-60 lg:min-h-9";

// Cambiar de organización y cerrar sesión. El cierre lo hace la API (borra la sesión y la
// cookie); aquí solo se olvida lo que la pantalla tenía en memoria.
export function SessionActions() {
  const t = useTranslations();
  const router = useRouter();
  const queryClient = useQueryClient();
  function leave() {
    queryClient.clear();
    router.replace("/login");
  }
  // En el hook, no en `mutate()`: así terminan aunque el shell se desmonte con la petición en vuelo.
  // `networkMode`: sin red el cierre falla y se dice; no queda en cola para cuando vuelva.
  const logout = useAuthLogout({
    mutation: {
      networkMode: "always",
      onSuccess: leave,
      onError: (error) => error.status === 401 && leave(),
    },
  });

  // Un 401 al cerrar significa que la sesión ya no existía: el resultado es el mismo.
  const failed = logout.isError && logout.error.status !== 401;
  const busy = !logout.isIdle && !failed; // hasta que cambia la página, no solo hasta la respuesta
  return (
    <div className="flex flex-col gap-1">
      {/* Antes de las acciones: al aparecer no las mueve de donde se acaban de pulsar. */}
      {failed ? (
        <p role="alert" className="text-danger px-3 text-sm">
          {t(`errors.api.${apiErrorKey(logout.error)}`)}
        </p>
      ) : null}
      <Link href="/o?elegir" className={ACTION}>
        {t("shell.switchOrganization")}
      </Link>
      {/* `aria-disabled` y no `disabled`: el botón conserva el foco mientras se cierra. */}
      <button
        type="button"
        className={ACTION}
        aria-disabled={busy}
        onClick={() => busy || logout.mutate()}
      >
        {busy ? t("shell.loggingOut") : t("shell.logout")}
      </button>
    </div>
  );
}
