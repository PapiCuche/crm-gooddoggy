"use client";

import { type InfiniteData, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { type rolesList, useRolesDelete } from "@/lib/api/client";
import type { Role } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { cn } from "@/lib/utils";

type Pages = InfiniteData<Awaited<ReturnType<typeof rolesList>>>;

// Borrar un rol (F2-40), con `DELETE …/roles/{id}/` (F2-38). No se puede deshacer: pide
// confirmación en la tarjeta, como «Suspender». Quién puede hacerlo lo decide la API (cubrir el
// rol, que no tenga miembros, que no sea de plantilla).
export function RoleDeleteAction({
  slug,
  role,
  listKey,
  onAsk,
  onGone,
}: {
  slug: string;
  role: Role;
  listKey: QueryKey;
  onAsk: () => void; // se abre la confirmación: el aviso anterior de la lista ya no aplica
  // La tarjeta desaparece: lo anuncia la lista. `stale`: ya no existía (hay que volver a pedirla).
  // `here`: el foco seguía en la tarjeta, o en ninguna parte.
  onGone: (notice: string, how: { stale: boolean; here: boolean }) => void;
}) {
  const t = useTranslations("roles.delete");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const question = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  // Lo que se confirma queda fijado al abrir (`null`: cerrado): la pregunta nombra el rol que el
  // usuario leyó, aunque la lista cambie debajo.
  const [asking, setAsking] = useState<string | null>(null);
  // Una escritura se envía una vez (el estado de la mutación llega a la pantalla una tarea
  // después de la pulsación).
  const sending = useRef(false);
  const here = () => {
    const active = document.activeElement;
    return active === document.body || !!root.current?.closest("li")?.contains(active);
  };
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const remove = useRolesDelete({
    mutation: {
      networkMode: "always",
      onSuccess: async () => {
        // Una lectura en vuelo traería el rol de vuelta a la lista: se cancela. Si era la lista
        // entera (no «Cargar más»), se vuelve a pedir después.
        const read = queryClient.getQueryState(listKey);
        const rereading = !!read && read.fetchStatus !== "idle" && !read.fetchMeta?.fetchMore;
        const focused = here(); // antes de quitar la tarjeta: después el foco ya no está en ella
        await queryClient.cancelQueries({ queryKey: listKey });
        queryClient.setQueryData<Pages>(listKey, (data) =>
          data
            ? {
                ...data,
                pages: data.pages.map((page) => ({
                  ...page,
                  results: page.results.filter((row) => row.id !== role.id),
                })),
              }
            : data,
        );
        onGone(t("done", { role: asking ?? role.name }), { stale: false, here: focused });
        if (rereading) void queryClient.invalidateQueries({ queryKey: listKey });
      },
      onError: (error) => {
        // El rol ya no existe: lo explica la lista, que se vuelve a pedir.
        if (error.status !== 404) return;
        onGone(t("stale", { role: asking ?? role.name }), { stale: true, here: here() });
        setAsking(null);
      },
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const gone = remove.isError && (remove.error.status === 401 || remove.error.status === 404);
  const failed = remove.isError && !gone;
  // Tras el éxito la tarjeta se va: hasta entonces sigue ocupada, sin otro envío.
  const busy =
    remove.isPending || remove.isSuccess || (remove.isError && remove.error.status === 401);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });

  const open = asking !== null;
  const opened = useRef(false);
  useEffect(() => {
    // El control pulsado desaparece al abrir y al cancelar: el foco pasa al que lo sustituye,
    // salvo que el usuario ya esté en otra parte. Al abrir, a «Cancelar», que no cambia nada.
    if (document.activeElement === document.body) {
      if (open) cancel.current?.focus();
      else if (opened.current) trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  return (
    <div
      ref={root}
      // Abierta ocupa su fila: las acciones vecinas de la tarjeta pasan a otra línea.
      className={cn("flex flex-col items-start gap-2", open && "w-full")}
      // Enter mantenido repite la pulsación: abriría y confirmaría de un solo gesto.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {asking ? (
        <div
          role="group"
          aria-label={t("label", { role: asking })}
          aria-describedby={question}
          className="flex w-full max-w-xl flex-col items-start gap-2"
        >
          <p id={question} className="text-sm wrap-anywhere">
            {t("ask", { role: asking })}
          </p>
          {failed ? (
            <p role="alert" className="text-danger text-sm">
              {remove.error.code === "PERMISSION_DENIED"
                ? t("denied")
                : errors(
                    remove.error.code === "VALIDATION_ERROR"
                      ? "INTERNAL_ERROR" // no hay campos que revisar: es un fallo nuestro
                      : apiErrorKey(remove.error),
                  )}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button
              ref={cancel}
              variant="ghost"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => sending.current || setAsking(null)}
            >
              {t("cancel")}
            </Button>
            {/* `aria-disabled` y no `disabled`: conserva el foco mientras se envía. */}
            <Button
              variant="primary"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => {
                if (sending.current) return;
                sending.current = true;
                remove.mutate({ orgSlug: slug, roleId: role.id });
              }}
            >
              {t(busy ? "busy" : "confirm")}
            </Button>
          </div>
        </div>
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t("label", { role: role.name })}
          onClick={() => {
            remove.reset();
            setAsking(role.name);
            onAsk();
          }}
        >
          {t("trigger")}
        </Button>
      )}
    </div>
  );
}
