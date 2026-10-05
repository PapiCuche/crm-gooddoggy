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
  getRolesListQueryKey,
  type membersList,
  membersRolesAssign,
  membersRolesRemove,
  rolesList,
} from "@/lib/api/client";
import type { Member, Role } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { ApiError } from "@/lib/http";

type Pages = InfiniteData<Awaited<ReturnType<typeof membersList>>>;
type Change = { role: Role; assign: boolean };

// Todos los roles de la organización: el panel los enseña juntos, así que sigue el cursor.
async function allRoles(slug: string, signal: AbortSignal): Promise<Role[]> {
  const found: Role[] = [];
  const seen = new Set<string>();
  let cursor: string | undefined;
  do {
    const page = await rolesList(slug, { limit: 200, ...(cursor ? { cursor } : {}) }, { signal });
    found.push(...page.results);
    cursor = page.next ?? undefined;
    // Un cursor repetido no avanza: es un fallo de la API, no una lista sin fin.
    if (cursor && seen.has(cursor)) throw new ApiError(500, "INTERNAL_ERROR");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return found;
}

// Asignar y quitar roles a un miembro (F2-27). Las reglas contra la escalada las aplica la API
// (ADR-003 §5): la pantalla ofrece la acción, envía un cambio cada vez y explica la respuesta.
export function MemberRolesAction({
  slug,
  member,
  name,
  listKey,
  onAsk,
  onStale,
}: {
  slug: string;
  member: Member;
  name: string;
  listKey: QueryKey;
  onAsk: () => void; // se abre el panel: el aviso anterior de la lista ya no aplica
  onStale: (notice: string, here: boolean) => void; // `here`: el foco seguía en esta acción
}) {
  const t = useTranslations("members.roleAction");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  // Los roles que el miembro tiene, fijados al abrir el panel (`null`: cerrado). Solo cambian
  // con las respuestas propias: si la lista cambia debajo, ningún botón pasa de «Asignar» a
  // «Quitar» bajo el dedo del usuario.
  const [held, setHeld] = useState<ReadonlySet<string> | null>(null);
  const [done, setDone] = useState<{ role: string; assign: boolean } | null>(null);
  // Una escritura cada vez, y enviada una sola vez (el estado de la mutación llega a la
  // pantalla una tarea después de la pulsación).
  const sending = useRef(false);
  const rolesKey = [...getRolesListQueryKey(slug), "all"];
  const roles = useQuery<Role[], ApiError>({
    queryKey: rolesKey,
    queryFn: ({ signal }) => allRoles(slug, signal),
    enabled: held !== null, // nada se pide hasta abrir el panel
    staleTime: 0, // y cada apertura vuelve a preguntar
    refetchOnWindowFocus: false, // no por su cuenta: «Reintentar» se desmontaría con el foco
    refetchOnReconnect: false,
  });
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const change = useMutation<void, ApiError, Change>({
    networkMode: "always",
    mutationFn: ({ role, assign }) =>
      (assign ? membersRolesAssign : membersRolesRemove)(slug, member.id, role.id),
    onSuccess: async (_result, { role, assign }) => {
      // Como al suspender: una lectura en vuelo traería los roles de antes y pisaría la fila.
      const read = queryClient.getQueryState(listKey);
      const rereading = !!read && read.fetchStatus !== "idle" && !read.fetchMeta?.fetchMore;
      await queryClient.cancelQueries({ queryKey: listKey });
      const own = (row: Member) => {
        const rest = row.roles.filter((mine) => mine.id !== role.id);
        const mine = assign ? [...rest, { id: role.id, code: role.code, name: role.name }] : rest;
        // Como el directorio: por nombre y, a igualdad, por código. Es una aproximación a la
        // intercalación de la base; la siguiente lectura trae el orden exacto.
        mine.sort((a, b) => a.name.localeCompare(b.name, "es") || (a.code < b.code ? -1 : 1));
        return { ...row, roles: mine };
      };
      queryClient.setQueryData<Pages>(listKey, (data) =>
        data
          ? {
              ...data,
              pages: data.pages.map((page) => ({
                ...page,
                results: page.results.map((row) => (row.id === member.id ? own(row) : row)),
              })),
            }
          : data,
      );
      setHeld((before) => {
        const after = new Set(before);
        if (assign) after.add(role.id);
        else after.delete(role.id);
        return after;
      });
      setDone({ role: role.name, assign });
      if (rereading) void queryClient.invalidateQueries({ queryKey: listKey });
    },
    onError: (error) => {
      // El miembro o el rol ya no existen, o ya no tiene ese rol: lo explica la lista.
      if (error.status !== 404) return;
      const active = document.activeElement;
      onStale(t("stale", { name }), active === document.body || !!root.current?.contains(active));
      void queryClient.invalidateQueries({ queryKey: rolesKey }); // el rol pudo borrarse
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
    const result = await roles.refetch();
    setRetrying(false);
    // El botón se va al llegar los roles: el foco, a «Cerrar», si seguía aquí o en ninguna parte.
    const active = document.activeElement;
    const still = active === document.body || active === retryButton.current;
    if (result.isSuccess && still) close.current?.focus();
  }

  function send(role: Role, assign: boolean) {
    if (sending.current) return;
    sending.current = true;
    setDone(null);
    change.mutate({ role, assign });
  }

  return (
    <div ref={root} className="flex flex-col items-start gap-2 sm:col-span-3 sm:items-end">
      {held ? (
        <div
          role="group"
          aria-label={t("panel", { name })}
          className="flex w-full max-w-md flex-col gap-2"
        >
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm font-medium">{t("panel", { name })}</p>
            <Button
              ref={close}
              variant="ghost"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => sending.current || setHeld(null)}
            >
              {t("close")}
            </Button>
          </div>
          {roles.data ? (
            <ul className="flex flex-col gap-1">
              {roles.data.map((role) => {
                const assign = !held.has(role.id);
                const mine = busy && change.variables?.role.id === role.id;
                return (
                  <li key={role.id} className="flex items-center justify-between gap-3">
                    <span className="min-w-0 text-sm wrap-anywhere">{role.name}</span>
                    {/* `aria-disabled` y no `disabled`: conserva el foco mientras se envía. */}
                    <Button
                      variant="ghost"
                      className="border-border min-h-11 shrink-0 border sm:min-h-9"
                      aria-label={t(assign ? "assignLabel" : "removeLabel", {
                        role: role.name,
                        name,
                      })}
                      aria-disabled={busy}
                      aria-busy={mine}
                      // El botón pasa a la acción contraria al terminar: el segundo clic de un
                      // doble clic, o una tecla mantenida, no deben deshacer lo recién hecho.
                      onKeyDown={(event) =>
                        event.repeat && event.key === "Enter" && event.preventDefault()
                      }
                      onClick={(event) => event.detail > 1 || send(role, assign)}
                    >
                      {t(`${assign ? "assign" : "remove"}${mine ? "Busy" : ""}`)}
                    </Button>
                  </li>
                );
              })}
              {roles.data.length === 0 ? (
                <li className="text-muted text-sm">{t("empty")}</li>
              ) : null}
            </ul>
          ) : (roles.isError && roles.error.status !== 401) || retrying ? (
            <div className="flex flex-col items-start gap-2">
              {roles.isError ? (
                <p role="alert" className="text-danger text-sm">
                  {errors(apiErrorKey(roles.error))}
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
                : errors(apiErrorKey(change.error))}
            </p>
          ) : null}
        </div>
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t("triggerLabel", { name })}
          onClick={() => {
            change.reset();
            setDone(null);
            setHeld(new Set(member.roles.map((role) => role.id)));
            onAsk();
          }}
        >
          {t("trigger")}
        </Button>
      )}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className="sr-only">
        {done ? t(done.assign ? "assignDone" : "removeDone", { role: done.role, name }) : ""}
      </p>
    </div>
  );
}
