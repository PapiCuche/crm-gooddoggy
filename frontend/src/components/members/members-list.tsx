"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { MemberStatusAction } from "@/components/members/member-status-action";
import { Button } from "@/components/ui/button";
import { getMembersListQueryKey, membersList } from "@/lib/api/client";
import type { Member } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

function fullName(user: Member["user"]): string {
  return `${user.first_name} ${user.last_name}`.trim();
}

// Directorio de miembros (F2-17): lo que devuelve `GET /api/v1/o/{slug}/members/`, página a
// página. Quién puede verlo lo decide la API: aquí un 403 solo se explica.
export function MembersList() {
  const t = useTranslations();
  const format = useFormatter();
  const { organization, membership_id: own, permissions } = useTenant();
  // Comodidad, no autorización: sin el permiso la API responde 403 a la acción (ADR-017).
  const manages = permissions.some((grant) => grant.code === "users.manage");
  const listKey = [...getMembersListQueryKey(organization.slug), "pages"];
  const members = useInfiniteQuery<Awaited<ReturnType<typeof membersList>>, ApiError>({
    queryKey: listKey,
    queryFn: ({ pageParam, signal }) =>
      membersList(organization.slug, pageParam ? { cursor: pageParam as string } : undefined, {
        signal,
      }),
    initialPageParam: null,
    getNextPageParam: (last) => last.next,
    gcTime: 0, // como la guardia: al salir no queda en memoria y cada entrada pregunta a la API
  });
  const heading = useRef<HTMLHeadingElement>(null);
  const fresh = useRef<HTMLLIElement>(null); // la primera fila de la última página cargada
  const moreButton = useRef<HTMLButtonElement>(null);
  const retryButton = useRef<HTMLButtonElement>(null);
  // El foco solo se mueve si sigue donde el usuario pulsó (o en ninguna parte).
  const still = (control: HTMLElement | null) =>
    document.activeElement === document.body || document.activeElement === control;
  const [retrying, setRetrying] = useState(false);
  // Como la guardia: una negativa cierra la lista aunque ya estuviera en pantalla, y sigue
  // cerrada hasta que la API responde bien (un fallo pasajero posterior no la reabre).
  const status = members.error?.status;
  const [denied, setDenied] = useState(false);
  if (status === 403 && !denied) setDenied(true);
  if (denied && members.isSuccess) setDenied(false);
  // «Cargar más»: ocupado desde la pulsación hasta la respuesta, también si espera a la red.
  const [more, setMore] = useState<"idle" | "busy" | "arrived">("idle");
  const pages = members.data?.pages ?? [];
  const rows = pages.flatMap((page) => page.results);
  const firstOfLast = pages.length > 1 ? pages.at(-1)?.results[0]?.id : undefined;

  useEffect(() => {
    // «Cargar más» puede desaparecer: el foco sigue en lo que llegó. Solo tras pulsarlo (no al
    // reabrir la pantalla con páginas en caché) y si el foco no se fue ya a otra parte.
    if (more === "arrived" && still(moreButton.current)) fresh.current?.focus();
  }, [more]);

  async function loadMore() {
    setMore("busy");
    const result = await members.fetchNextPage();
    // Llegó si hay una página más; un fallo o una petición cancelada no lo son.
    setMore((result.data?.pages.length ?? 0) > pages.length ? "arrived" : "idle");
    const closed = result.error?.status === 403; // la lista se cierra: el foco, al título
    if (closed && still(moreButton.current)) heading.current?.focus();
  }

  async function retry() {
    setRetrying(true);
    const result = await members.refetch();
    setRetrying(false);
    // La tarjeta se va (llegó la lista, o la API niega): el foco, al título.
    const gone = result.isSuccess || result.error?.status === 403;
    if (gone && still(retryButton.current)) heading.current?.focus();
  }

  let body;
  if (denied) {
    body = <p role="alert">{t("members.denied")}</p>;
  } else if (members.data) {
    body = (
      <>
        <ul aria-label={t("members.title")} className="flex flex-col gap-2">
          {rows.map((member) => (
            <li
              key={member.id}
              ref={member.id === firstOfLast ? fresh : undefined}
              tabIndex={member.id === firstOfLast ? -1 : undefined}
              className="border-border bg-surface grid gap-x-4 gap-y-2 rounded-lg border p-4 focus-visible:outline-none sm:grid-cols-[minmax(0,2fr)_minmax(0,2fr)_auto] sm:items-center"
            >
              <p className="flex min-w-0 flex-col leading-snug">
                <span className="font-medium wrap-anywhere">
                  {fullName(member.user) || member.user.email}
                </span>
                {fullName(member.user) ? (
                  <span className="text-muted text-sm wrap-anywhere">{member.user.email}</span>
                ) : null}
              </p>
              <ul aria-label={t("members.roles")} className="flex flex-wrap gap-1.5">
                {member.roles.map((role) => (
                  <li key={role.code} className="bg-background rounded-md px-2 py-0.5 text-sm">
                    {role.name}
                  </li>
                ))}
                {member.roles.length === 0 ? (
                  <li className="text-muted text-sm">{t("members.noRole")}</li>
                ) : null}
              </ul>
              <p className="flex flex-col text-sm sm:items-end">
                <span
                  className={cn(
                    "font-medium",
                    member.status === "ACTIVE" ? "text-success" : "text-muted",
                  )}
                >
                  {t(`members.status.${member.status}`)}
                </span>
                <span className="text-muted">
                  {t("members.joined", {
                    date: format.dateTime(new Date(member.joined_at), { dateStyle: "medium" }),
                  })}
                </span>
              </p>
              {manages &&
              member.id !== own &&
              (member.status === "ACTIVE" || member.status === "SUSPENDED") ? (
                <MemberStatusAction
                  slug={organization.slug}
                  member={member}
                  name={fullName(member.user) || member.user.email}
                  organization={organization.name}
                  listKey={listKey}
                />
              ) : null}
            </li>
          ))}
        </ul>
        <p role="status" className="sr-only">
          {t("members.count", { count: rows.length })}
        </p>
        {members.hasNextPage ? (
          <div className="flex items-start gap-3">
            <Button
              ref={moreButton}
              variant="accent"
              size="lg"
              aria-disabled={more === "busy"}
              onClick={() => more === "busy" || void loadMore()}
            >
              {more === "busy" ? t("members.loadingMore") : t("members.more")}
            </Button>
            {/* Al lado del botón: no lo mueve de donde se pulsó ni queda fuera de la vista. Se
                quita al reintentar: si vuelve a fallar se monta de nuevo y se anuncia otra vez. */}
            {members.isFetchNextPageError && status !== 401 && more !== "busy" ? (
              <p role="alert" className="text-danger min-w-0 self-center text-sm">
                {t(`errors.api.${apiErrorKey(members.error)}`)}
              </p>
            ) : null}
          </div>
        ) : null}
      </>
    );
  } else if ((members.isError && status !== 401) || retrying) {
    // Sin sesión (401), `Providers` ya lleva al login: queda el aviso de carga, sin error.
    body = (
      <div className="flex flex-col items-start gap-3">
        {members.isError ? (
          <p role="alert" className="text-danger">
            {t(`errors.api.${apiErrorKey(members.error)}`)}
          </p>
        ) : null}
        <Button
          ref={retryButton}
          variant="primary"
          size="lg"
          aria-disabled={retrying}
          onClick={() => retrying || void retry()}
        >
          {retrying ? t("members.loading") : t("errors.retry")}
        </Button>
      </div>
    );
  } else {
    body = (
      <p role="status" className="text-muted">
        {t("members.loading")}
      </p>
    );
  }

  return (
    <section className="flex max-w-4xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <p className="text-[13px] tracking-wide uppercase">{t("members.eyebrow")}</p>
        <h1
          ref={heading}
          tabIndex={-1}
          className="text-[28px] leading-tight font-bold tracking-[0.02em] focus-visible:outline-none"
        >
          {t("members.title")}
        </h1>
        <p className="text-muted">{t("members.intro", { organization: organization.name })}</p>
      </div>
      {body}
    </section>
  );
}
