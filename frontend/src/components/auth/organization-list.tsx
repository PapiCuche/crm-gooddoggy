"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { SessionActions } from "@/components/app-shell/session-actions";
import { Button } from "@/components/ui/button";
import { useMeOrganizations } from "@/lib/api/client";
import { apiErrorKey } from "@/lib/api-errors";
import { ApiError } from "@/lib/http";

// Organizaciones del usuario (E01-05). La lista es la que devuelve la API: la URL solo
// selecciona, y entrar en una organización lo decide la API en cada petición.
export function OrganizationList({ choose }: { choose: boolean }) {
  const t = useTranslations();
  const router = useRouter();
  const organizations = useMeOrganizations();
  const region = useRef<HTMLDivElement>(null);
  const again = useRef<HTMLButtonElement>(null);
  const [retries, setRetries] = useState(0);
  // Sin sesión, `Providers` ya lleva al login: aquí solo se evita mostrar un error mientras.
  const expired = organizations.error instanceof ApiError && organizations.error.status === 401;
  const only = !choose && organizations.data?.length === 1 ? organizations.data[0] : undefined;

  useEffect(() => {
    if (only) router.replace(`/o/${only.slug}`); // una sola: no hay nada que elegir
  }, [only, router]);

  useEffect(() => {
    // El botón se desmonta mientras se reintenta: al terminar, el foco va al botón nuevo o,
    // si ya hay respuesta, a lo que llegó. Sin esto se quedaría en ninguna parte.
    if (retries) (again.current ?? region.current)?.focus();
  }, [retries]);

  let announced = ""; // lo que oye un lector de pantalla cuando cambia el estado
  let content: ReactNode;
  const waiting = organizations.isPending || expired || !!only;
  if (waiting) {
    announced = t("organizations.loading");
    content = <p className="text-muted">{announced}</p>;
  } else if (organizations.isError) {
    content = (
      <>
        <p role="alert" className="text-danger">
          {t(`errors.api.${apiErrorKey(organizations.error)}`)}
        </p>
        <Button
          ref={again}
          variant="primary"
          size="lg"
          onClick={() => void organizations.refetch().then(() => setRetries(retries + 1))}
        >
          {t("errors.retry")}
        </Button>
      </>
    );
  } else if (organizations.data.length === 0) {
    announced = t("organizations.emptyTitle");
    content = (
      <>
        <p className="font-medium">{announced}</p>
        <p className="text-muted">{t("organizations.emptyBody")}</p>
      </>
    );
  } else {
    announced = t("organizations.count", { count: organizations.data.length });
    content = (
      <>
        <p className="text-muted">{t("organizations.intro")}</p>
        <ul className="flex flex-col gap-2">
          {organizations.data.map((organization) => (
            <li key={organization.id}>
              <Link
                href={`/o/${organization.slug}`}
                className="border-foreground/50 hover:border-foreground hover:bg-surface-raised flex items-center justify-between gap-3 rounded-[10px] border px-4 py-3 transition-[background-color,border-color,transform] duration-150 ease-(--ease-out) active:scale-[0.98]"
              >
                <span className="flex min-w-0 flex-col">
                  <span className="truncate font-medium">{organization.name}</span>
                  <span className="text-muted truncate font-mono text-sm">{organization.slug}</span>
                </span>
                <span aria-hidden>→</span>
              </Link>
            </li>
          ))}
        </ul>
      </>
    );
  }
  return (
    <div ref={region} tabIndex={-1} className="flex flex-col gap-5 focus-visible:outline-none">
      <p role="status" className="sr-only">
        {announced}
      </p>
      {content}
      {/* Quien no puede entrar a ninguna organización, o la está eligiendo, también puede salir. */}
      {waiting ? null : <SessionActions switcher={false} />}
    </div>
  );
}
