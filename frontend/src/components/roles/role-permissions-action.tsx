"use client";

import {
  type InfiniteData,
  type QueryKey,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  getPermissionsListQueryKey,
  permissionsList,
  type rolesList,
  rolesPermissionsGrant,
  rolesPermissionsRevoke,
} from "@/lib/api/client";
import type { Permission, Role } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

type Pages = InfiniteData<Awaited<ReturnType<typeof rolesList>>>;
type Change = { permission: Permission; grant: boolean };

// Conceder y retirar permisos a un rol (F2-36). Las reglas contra la escalada las aplica la API
// (ADR-003 §5): la pantalla ofrece la acción, envía un cambio cada vez y explica la respuesta.
export function RolePermissionsAction({
  slug,
  role,
  listKey,
  label,
  scope,
  onAsk,
  onStale,
}: {
  slug: string;
  role: Role;
  listKey: QueryKey;
  label: (code: string) => string; // el nombre del permiso para mostrar
  scope: (code: string) => string; // y el de un alcance
  onAsk: () => void; // se abre el panel: el aviso anterior de la lista ya no aplica
  onStale: (notice: string, here: boolean) => void; // `here`: el foco seguía en esta acción
}) {
  const t = useTranslations("roles.grants");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  // Las concesiones del rol, fijadas al abrir el panel (`null`: cerrado). Solo cambian con las
  // respuestas propias: si la lista cambia debajo, ningún botón pasa de «Conceder» a «Retirar»
  // bajo el dedo del usuario.
  const [held, setHeld] = useState<ReadonlyMap<string, string | null> | null>(null);
  const [done, setDone] = useState<{ permission: string; grant: boolean } | null>(null);
  // Una escritura cada vez, y enviada una sola vez (el estado de la mutación llega a la
  // pantalla una tarea después de la pulsación).
  const sending = useRef(false);
  const catalog = useQuery<Permission[], ApiError>({
    queryKey: getPermissionsListQueryKey(slug),
    queryFn: async ({ signal }) => (await permissionsList(slug, { signal })).results,
    enabled: held !== null, // nada se pide hasta abrir el panel
    staleTime: 0, // y cada apertura vuelve a preguntar
    refetchOnWindowFocus: false, // no por su cuenta: «Reintentar» se desmontaría con el foco
    refetchOnReconnect: false,
  });
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const change = useMutation<void, ApiError, Change>({
    networkMode: "always",
    mutationFn: ({ permission, grant }) =>
      grant
        ? rolesPermissionsGrant(slug, role.id, permission.code, {})
        : rolesPermissionsRevoke(slug, role.id, permission.code),
    onSuccess: async (_result, { permission, grant }) => {
      // Una lectura en vuelo traería las concesiones de antes y pisaría la tarjeta: se cancela.
      // Si era la lista entera (no «Cargar más»), se vuelve a pedir después.
      const read = queryClient.getQueryState(listKey);
      const rereading = !!read && read.fetchStatus !== "idle" && !read.fetchMeta?.fetchMore;
      await queryClient.cancelQueries({ queryKey: listKey });
      const own = (row: Role) => {
        const rest = row.permissions.filter((mine) => mine.code !== permission.code);
        const mine = grant ? [...rest, { code: permission.code, scope: null }] : rest;
        mine.sort((a, b) => (a.code < b.code ? -1 : 1)); // por código, como el directorio
        return { ...row, permissions: mine };
      };
      queryClient.setQueryData<Pages>(listKey, (data) =>
        data
          ? {
              ...data,
              pages: data.pages.map((page) => ({
                ...page,
                results: page.results.map((row) => (row.id === role.id ? own(row) : row)),
              })),
            }
          : data,
      );
      setHeld((before) => {
        const after = new Map(before);
        if (grant) after.set(permission.code, null);
        else after.delete(permission.code);
        return after;
      });
      setDone({ permission: label(permission.code), grant });
      if (rereading) void queryClient.invalidateQueries({ queryKey: listKey });
    },
    onError: (error) => {
      // El rol ya no existe, o ya no tiene esa concesión: lo explica la lista.
      if (error.status !== 404) return;
      const active = document.activeElement;
      const here = active === document.body || !!root.current?.contains(active);
      onStale(t("stale", { role: role.name }), here);
      setHeld(null);
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const failed = change.isError && change.error.status !== 401 && change.error.status !== 404;
  const busy = change.isPending || (change.isError && change.error.status === 401);
  const open = held !== null;
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta:
  // una pulsación entre las dos cosas reenviaría lo mismo. Sin sesión (401) no se suelta.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });

  const opened = useRef(false);
  useEffect(() => {
    // El control pulsado desaparece al abrir y al cerrar: el foco pasa al que lo sustituye,
    // salvo que el usuario ya esté en otra parte. Al abrir, a «Cerrar», que no cambia nada.
    if (document.activeElement === document.body) {
      if (open) close.current?.focus();
      else if (opened.current) trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  const retryButton = useRef<HTMLButtonElement>(null);
  const [retrying, setRetrying] = useState(false);
  async function retry() {
    setRetrying(true);
    const result = await catalog.refetch();
    setRetrying(false);
    // El botón se va al llegar el catálogo: el foco, a «Cerrar», si seguía aquí o en ninguna parte.
    const active = document.activeElement;
    const still = active === document.body || active === retryButton.current;
    if (result.isSuccess && still) close.current?.focus();
  }

  function send(permission: Permission, grant: boolean) {
    if (sending.current) return;
    sending.current = true;
    setDone(null);
    change.mutate({ permission, grant });
  }

  return (
    <div ref={root} className="flex flex-col items-start gap-2">
      {held ? (
        <div
          role="group"
          aria-label={t("panel", { role: role.name })}
          className="flex w-full max-w-xl flex-col gap-2"
        >
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm font-medium wrap-anywhere">{t("panel", { role: role.name })}</p>
            <Button
              ref={close}
              variant="ghost"
              className="min-h-11 shrink-0 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => sending.current || setHeld(null)}
            >
              {t("close")}
            </Button>
          </div>
          {/* Un cambio llega a todos los miembros del rol, no solo a quien lo edita. */}
          <p className="text-muted text-sm">{t("reach", { count: role.members })}</p>
          {catalog.data ? (
            <ul className="flex flex-col gap-1">
              {catalog.data.map((permission) => {
                const grant = !held.has(permission.code);
                const mine = busy && change.variables?.permission.code === permission.code;
                const name = label(permission.code);
                const granted = held.get(permission.code);
                return (
                  <li key={permission.code} className="flex items-center justify-between gap-3">
                    <span className="flex min-w-0 flex-wrap items-baseline gap-x-2 text-sm">
                      <span className="wrap-anywhere">{name}</span>
                      {permission.is_sensitive ? (
                        <span className="text-muted">{t("sensitive")}</span>
                      ) : null}
                    </span>
                    {permission.supports_scope ? (
                      // El alcance aún no se elige aquí: se enseña lo que hay, sin botón.
                      <span className="text-muted shrink-0 text-sm">
                        {granted ? scope(granted) : t("scoped")}
                      </span>
                    ) : (
                      // `aria-disabled` y no `disabled`: conserva el foco mientras se envía.
                      <Button
                        variant="ghost"
                        className="border-border min-h-11 shrink-0 border sm:min-h-9"
                        aria-label={t(grant ? "grantLabel" : "revokeLabel", {
                          permission: name,
                          role: role.name,
                        })}
                        aria-disabled={busy}
                        aria-busy={mine}
                        // El botón pasa a la acción contraria al terminar: el segundo clic de un
                        // doble clic, o una tecla mantenida, no deben deshacer lo recién hecho.
                        onKeyDown={(event) =>
                          event.repeat && event.key === "Enter" && event.preventDefault()
                        }
                        onClick={(event) => event.detail > 1 || send(permission, grant)}
                      >
                        {t(`${grant ? "grant" : "revoke"}${mine ? "Busy" : ""}`)}
                      </Button>
                    )}
                  </li>
                );
              })}
            </ul>
          ) : (catalog.isError && catalog.error.status !== 401) || retrying ? (
            <div className="flex flex-col items-start gap-2">
              {catalog.isError ? (
                <p role="alert" className="text-danger text-sm">
                  {errors(apiErrorKey(catalog.error))}
                </p>
              ) : null}
              {/* No se desmonta mientras reintenta: conserva el foco. */}
              <Button
                ref={retryButton}
                variant="primary"
                className="min-h-11 sm:min-h-9"
                aria-disabled={retrying}
                onClick={() => retrying || void retry()}
              >
                {retrying ? t("loading") : t("retry")}
              </Button>
            </div>
          ) : (
            <p role="status" className="text-muted text-sm">
              {t("loading")}
            </p>
          )}
          {failed ? (
            <p role="alert" className="text-danger text-sm">
              {change.error.code === "PERMISSION_DENIED"
                ? t("denied")
                : errors(
                    change.error.code === "VALIDATION_ERROR"
                      ? "INTERNAL_ERROR" // no hay campos que revisar: es un fallo nuestro
                      : apiErrorKey(change.error),
                  )}
            </p>
          ) : null}
        </div>
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t("triggerLabel", { role: role.name })}
          onClick={() => {
            change.reset();
            setDone(null);
            setHeld(new Map(role.permissions.map((grant) => [grant.code, grant.scope])));
            onAsk();
          }}
        >
          {t("trigger")}
        </Button>
      )}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className="sr-only">
        {done
          ? t(done.grant ? "grantDone" : "revokeDone", {
              permission: done.permission,
              role: role.name,
            })
          : ""}
      </p>
    </div>
  );
}
