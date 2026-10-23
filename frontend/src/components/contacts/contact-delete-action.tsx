"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useContactsDelete } from "@/lib/api/client";
import type { Contact } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { cn } from "@/lib/utils";

// Borrar un cliente (F4-05) con `DELETE …/contacts/{id}/` (F4-03), tras confirmar, como borrar
// una marca. El borrado es lógico. La fila sale cuando vuelve la lista; como desaparece, el
// resultado lo anuncia la lista (`onDeleted`). Un 404 (fuera de su alcance o ya borrado) también.
export function ContactDeleteAction({
  slug,
  contact,
  listKey,
  onAsk,
  onStale,
  onDeleted,
}: {
  slug: string;
  contact: Contact;
  listKey: QueryKey;
  onAsk: () => void; // se abre la confirmación: el aviso anterior de la lista ya no aplica
  onStale: (name: string, here: boolean) => void; // `here`: el foco seguía en esta fila
  onDeleted: (text: string, here: boolean) => void;
}) {
  const t = useTranslations("contacts.delete");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const question = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  // El nombre que el usuario leyó al abrir (`null`: cerrado), aunque la fila cambie debajo.
  const [asking, setAsking] = useState<string | null>(null);
  // Una escritura se envía una vez (el estado de la mutación llega una tarea después).
  const sending = useRef(false);
  const here = () => {
    const active = document.activeElement;
    return active === document.body || !!root.current?.closest("li")?.contains(active);
  };
  // `networkMode`: sin red falla y se dice; un borrado no queda en cola para después.
  const remove = useContactsDelete({
    mutation: {
      networkMode: "always",
      onSuccess: async () => {
        // Sigue «Borrando…» hasta que la lista vuelve sin la fila. Si releerla falla, se dice.
        await queryClient.invalidateQueries({ queryKey: listKey });
        const stale = queryClient.getQueryState(listKey)?.status === "error";
        setAsking(null); // la confirmación se cierra también si la lista no se pudo releer
        onDeleted(
          t(stale ? "doneStale" : "done", { name: asking ?? contact.display_name }),
          here(),
        );
      },
      onError: (error) => {
        if (error.status !== 404) return;
        const focused = here();
        setAsking(null);
        onStale(asking ?? contact.display_name, focused);
      },
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error. Un 404 cierra.
  const failed = remove.isError && remove.error.status !== 401 && remove.error.status !== 404;
  const busy = remove.isPending || (remove.isError && remove.error.status === 401);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });

  const open = asking !== null;
  const opened = useRef(false);
  useEffect(() => {
    // Al abrir, el foco va a «Cancelar», que no cambia nada; al cerrar, vuelve a «Borrar».
    if (open) cancel.current?.focus();
    else if (opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  return (
    <div
      ref={root}
      className={cn("flex flex-col items-start gap-2", open && "w-full")}
      // Enter mantenido repite la pulsación: abriría y confirmaría de un solo gesto.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {asking !== null ? (
        <div
          role="group"
          aria-label={t("label", { name: asking })}
          aria-describedby={question}
          className="flex w-full max-w-xl flex-col items-start gap-2"
        >
          <p id={question} className="text-sm wrap-anywhere">
            {t("ask", { name: asking })}
          </p>
          {failed ? (
            <p role="alert" className="text-danger text-sm">
              {remove.error.code === "PERMISSION_DENIED"
                ? t("denied")
                : errors(apiErrorKey(remove.error))}
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
                remove.mutate({ orgSlug: slug, contactId: contact.id });
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
          aria-label={t("label", { name: contact.display_name })}
          onClick={() => {
            remove.reset();
            setAsking(contact.display_name);
            onAsk();
          }}
        >
          {t("trigger")}
        </Button>
      )}
    </div>
  );
}
