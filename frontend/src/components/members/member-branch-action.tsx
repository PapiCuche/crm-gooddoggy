"use client";

import { type InfiniteData, type QueryKey, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  branchesList,
  getBranchesListQueryKey,
  type membersList,
  useMembersSetBranch,
} from "@/lib/api/client";
import type { Branch, Member } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { ApiError } from "@/lib/http";

type Pages = InfiniteData<Awaited<ReturnType<typeof membersList>>>;
type Option = Pick<Branch, "id" | "code" | "name"> & { is_active?: boolean };

// Todas las sucursales de la organización: el selector las enseña juntas, así que sigue el cursor.
async function allBranches(slug: string, signal: AbortSignal): Promise<Branch[]> {
  const found: Branch[] = [];
  const seen = new Set<string>();
  let cursor: string | undefined;
  do {
    const page = await branchesList(
      slug,
      { limit: 200, ...(cursor ? { cursor } : {}) },
      { signal },
    );
    found.push(...page.results);
    cursor = page.next ?? undefined;
    // Un cursor repetido no avanza: es un fallo de la API, no una lista sin fin.
    if (cursor && seen.has(cursor)) throw new ApiError(500, "INTERNAL_ERROR");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return found;
}

// Asignar, cambiar o quitar la sucursal de un miembro (F2-72). Quién puede hacerlo lo decide la
// API (ADR-017): la pantalla ofrece la acción, envía lo elegido una vez y explica la respuesta.
export function MemberBranchAction({
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
  const t = useTranslations("members.branchAction");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const field = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  // La sucursal que el miembro tenía al abrir el panel (`null`: cerrado). Si la lista cambia
  // debajo, el selector no cambia bajo el dedo: se compara y se parte de lo que el usuario leyó.
  const [from, setFrom] = useState<{ branch: Member["default_branch"] } | null>(null);
  const [choice, setChoice] = useState(""); // el id elegido; vacío: sin sucursal
  const [done, setDone] = useState<{ branch: Member["default_branch"] } | null>(null);
  // Una escritura se envía una vez: el estado de la mutación llega a la pantalla una tarea
  // después de la pulsación, y dos pulsaciones seguidas no deben ser dos peticiones.
  const sending = useRef(false);
  const initial = from?.branch?.id ?? "";
  const branchesKey = [...getBranchesListQueryKey(slug), "all"];
  const branches = useQuery<Branch[], ApiError>({
    queryKey: branchesKey,
    queryFn: ({ signal }) => allBranches(slug, signal),
    enabled: from !== null, // nada se pide hasta abrir el panel
    staleTime: 0, // y cada apertura vuelve a preguntar
    refetchOnWindowFocus: false, // no por su cuenta: «Reintentar» se desmontaría con el foco
    refetchOnReconnect: false,
  });
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const change = useMembersSetBranch({
    mutation: {
      networkMode: "always",
      onSuccess: async (result, { membershipId }) => {
        // Como al suspender: una lectura en vuelo traería la sucursal de antes y pisaría la fila.
        const read = queryClient.getQueryState(listKey);
        const rereading = !!read && read.fetchStatus !== "idle" && !read.fetchMeta?.fetchMore;
        await queryClient.cancelQueries({ queryKey: listKey });
        if (result.id !== membershipId) {
          return void queryClient.invalidateQueries({ queryKey: listKey });
        }
        queryClient.setQueryData<Pages>(listKey, (data) =>
          data
            ? {
                ...data,
                pages: data.pages.map((page) => ({
                  ...page,
                  results: page.results.map((row) =>
                    row.id === membershipId
                      ? { ...row, default_branch: result.default_branch }
                      : row,
                  ),
                })),
              }
            : data,
        );
        setDone({ branch: result.default_branch }); // lo que respondió la API, no lo que se pidió
        setFrom(null);
        if (rereading) void queryClient.invalidateQueries({ queryKey: listKey });
      },
      onError: (error) => {
        if (error.status !== 404) return;
        // La membresía ya no está al alcance: lo explica la lista, que se vuelve a pedir.
        const active = document.activeElement;
        onStale(t("stale", { name }), active === document.body || !!root.current?.contains(active));
        setFrom(null);
      },
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const failed = change.isError && change.error.status !== 401 && change.error.status !== 404;
  const busy = change.isPending || (change.isError && change.error.status === 401);
  const open = from !== null;
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta:
  // una pulsación entre las dos cosas reenviaría lo mismo. En un efecto de layout: uno pasivo
  // de un render anterior podría llegar después de la pulsación. Sin sesión (401) no se suelta.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });

  const opened = useRef(false);
  useEffect(() => {
    // El control pulsado desaparece al abrir y al cerrar: el foco pasa al que lo sustituye,
    // salvo que el usuario ya esté en otra parte. Al abrir, a «Cancelar», que no cambia nada.
    if (document.activeElement === document.body) {
      if (open) cancel.current?.focus();
      else if (opened.current) trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  const retryButton = useRef<HTMLButtonElement>(null);
  const [retrying, setRetrying] = useState(false);
  async function retry() {
    setRetrying(true);
    const result = await branches.refetch();
    setRetrying(false);
    // El botón se va al llegar las sucursales: el foco, a «Cancelar», si seguía aquí o en ninguna parte.
    const active = document.activeElement;
    const still = active === document.body || active === retryButton.current;
    if (result.isSuccess && still) cancel.current?.focus();
  }

  function save() {
    if (sending.current) return;
    if (choice === initial) return setFrom(null); // nada que cambiar: no se envía
    sending.current = true;
    change.mutate({ orgSlug: slug, membershipId: member.id, data: { branch_id: choice || null } });
  }

  // La sucursal del miembro figura siempre, aunque la lectura de sucursales aún no la traiga.
  const rows: Option[] = branches.data ?? [];
  const options: Option[] =
    from?.branch && !rows.some((row) => row.id === initial) ? [...rows, from.branch] : rows;
  return (
    <div
      ref={root}
      className="flex flex-col items-start gap-2 sm:col-span-3 sm:items-end"
      // Al guardar, el panel deja su sitio a «Sucursal»: una tecla mantenida no debe reabrirlo.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {from ? (
        <div
          role="group"
          aria-label={t("panel", { name })}
          className="flex w-full max-w-md flex-col gap-2"
        >
          <label htmlFor={field} className="text-sm font-medium wrap-anywhere">
            {t("panel", { name })}
          </label>
          {branches.data ? (
            <>
              {/* 16 px en el control: por debajo, Safari en iOS amplía la página al enfocarlo. */}
              <select
                id={field}
                value={choice}
                aria-disabled={busy}
                className="bg-surface border-foreground/50 focus-visible:border-foreground h-11 w-full rounded-[10px] border px-3 text-[16px]"
                onChange={(event) => {
                  if (sending.current) return; // mientras se envía, lo elegido no cambia
                  change.reset();
                  setChoice(event.target.value);
                }}
              >
                <option value="">{t("none")}</option>
                {options.map((row) => (
                  <option key={row.id} value={row.id}>
                    {t(row.is_active === false ? "optionInactive" : "option", {
                      name: row.name,
                      code: row.code,
                    })}
                  </option>
                ))}
              </select>
              {options.length === 0 ? <p className="text-muted text-sm">{t("empty")}</p> : null}
            </>
          ) : (branches.isError && branches.error.status !== 401) || retrying ? (
            <div className="flex flex-col items-start gap-2">
              {branches.isError ? (
                <p role="alert" className="text-danger text-sm">
                  {errors(apiErrorKey(branches.error))}
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
                      ? "INTERNAL_ERROR" // las sucursales salen de la API: es un fallo nuestro
                      : apiErrorKey(change.error),
                  )}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-2 sm:justify-end">
            <Button
              ref={cancel}
              variant="ghost"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => sending.current || setFrom(null)}
            >
              {t("cancel")}
            </Button>
            {branches.data ? (
              // `aria-disabled` y no `disabled`: conserva el foco mientras se envía.
              <Button
                variant="primary"
                className="min-h-11 sm:min-h-9"
                aria-disabled={busy}
                onClick={save}
              >
                {t(busy ? "saving" : "save")}
              </Button>
            ) : null}
          </div>
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
            setFrom({ branch: member.default_branch });
            setChoice(member.default_branch?.id ?? "");
            onAsk();
          }}
        >
          {t("trigger")}
        </Button>
      )}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className="sr-only">
        {done
          ? done.branch
            ? t("done", { name, branch: done.branch.name, code: done.branch.code })
            : t("cleared", { name })
          : ""}
      </p>
    </div>
  );
}
