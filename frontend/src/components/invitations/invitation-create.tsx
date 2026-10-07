"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { useInvitationsCreate } from "@/lib/api/client";
import type { Role } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

const MAX_ROLES = 20; // el límite de la API

// Invitar a una persona (F2-84), con `POST /api/v1/o/{slug}/invitations/` (F2-80): su correo y
// los roles que tendrá al aceptar. La invitación queda registrada; el correo todavía no se
// envía (D-F2-14), y el formulario lo dice. Qué roles puede dar cada quien lo decide la API: la
// pantalla ofrece todos y explica la respuesta. El camino de envío es el de `FieldsForm`.
export function InvitationCreate({
  slug,
  listKey,
  roles,
  rolesFailed,
  onAsk,
  onDone,
}: {
  slug: string;
  listKey: QueryKey;
  roles: readonly Role[] | undefined; // el directorio que ya lee la lista; sin él no hay qué elegir
  rolesFailed: boolean;
  onAsk: () => void; // se abre el formulario: el directorio de roles se vuelve a pedir
  onDone: () => void; // quedó registrada: lo que «Revocar» dijo de ese correo ya no aplica
}) {
  const t = useTranslations("invitations.create");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  // Los roles elegidos, por identificador. No llegan al DOM: las casillas no llevan `value`.
  const [chosen, setChosen] = useState<readonly string[]>([]);
  const [refused, setRefused] = useState<"rolesRequired" | "rolesTooMany" | null>(null);
  // Lo elegido que sigue en el directorio. Un rol borrado con el formulario abierto deja de
  // verse: tampoco cuenta ni se envía.
  const picked = chosen.filter((id) => roles?.some((role) => role.id === id));
  const trigger = useRef<HTMLButtonElement>(null);
  const firstRole = useRef<HTMLInputElement>(null);
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const invite = useInvitationsCreate({
    mutation: {
      networkMode: "always",
      onSuccess: (made) => {
        setDone(made.email); // el correo que guardó la API, no el que se escribió
        setOpen(false);
        onDone();
        // La invitación nueva va al principio de la lista, que puede tener más páginas: se
        // vuelve a pedir lo que hay en pantalla en vez de añadir una fila a mano.
        void queryClient.invalidateQueries({ queryKey: listKey });
      },
    },
  });

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el control pulsado desaparece: el foco vuelve a «Invitar», salvo que el
    // usuario ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function show(next: boolean) {
    invite.reset();
    if (next) {
      setDone(null);
      setChosen([]);
      setRefused(null);
      onAsk();
    }
    setOpen(next);
  }
  function toggle(role: string) {
    if (invite.isPending) return; // lo enviado no cambia mientras se envía
    setRefused(null);
    setChosen((before) =>
      before.includes(role) ? before.filter((one) => one !== role) : [...before, role],
    );
  }
  function ready(): boolean {
    const problem =
      picked.length === 0 ? "rolesRequired" : picked.length > MAX_ROLES ? "rolesTooMany" : null;
    setRefused(problem);
    if (problem) firstRole.current?.focus();
    return problem === null;
  }
  // Por `code`, nunca por el texto de la respuesta. Lo del correo, junto al correo.
  function explained(error: ApiError) {
    if (error.code === "ALREADY_MEMBER") return { field: "email" as const, text: t("member") };
    if (error.code === "INVITATION_PENDING") return { field: "email" as const, text: t("pending") };
    if (error.code === "INVITATION_LIMIT") return { text: t("limit") };
    if (error.code === "RATE_LIMITED") return { text: t("daily") };
    const fields = error.code === "VALIDATION_ERROR" ? (error.fields ?? {}) : {};
    return Object.hasOwn(fields, "role_ids") ? { text: t("rolesInvalid") } : null;
  }

  return (
    <div
      className="flex flex-col items-start gap-3"
      // Enter mantenido repite la pulsación: tras invitar reabriría el formulario (y borraría el
      // aviso), y tras un error reenviaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {open ? (
        <FieldsForm
          title={t("title")}
          heading={<h2 className="font-medium">{t("title")}</h2>}
          className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
          fields={[
            {
              name: "email",
              max: 254,
              label: t("email"),
              invalid: t("emailInvalid"),
              required: t("emailRequired"),
            },
          ]}
          write={invite}
          check={ready}
          send={({ email }) => invite.mutate({ orgSlug: slug, data: { email, role_ids: picked } })}
          taken={explained}
          denied={t("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: t("cancel") }}
          note={
            <>
              <fieldset className="flex min-w-0 flex-col gap-2">
                <legend className="mb-2 text-sm font-medium">{t("roles")}</legend>
                {roles ? (
                  roles.map((role, place) => (
                    <label key={role.id} className="flex min-h-11 items-center gap-2 sm:min-h-9">
                      <input
                        ref={place === 0 ? firstRole : undefined}
                        type="checkbox"
                        className="size-4 shrink-0"
                        checked={chosen.includes(role.id)}
                        onChange={() => toggle(role.id)}
                      />
                      <span className="min-w-0 wrap-anywhere">{role.name}</span>
                    </label>
                  ))
                ) : (
                  <p role={rolesFailed ? "alert" : "status"} className="text-muted text-sm">
                    {t(rolesFailed ? "rolesFailed" : "rolesLoading")}
                  </p>
                )}
                {refused ? (
                  <p role="alert" className="text-danger text-sm">
                    {t(refused)}
                  </p>
                ) : null}
              </fieldset>
              <p className="text-muted text-sm">{t("notSent")}</p>
            </>
          }
          onCancel={() => show(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant="accent"
          className="min-h-11 sm:min-h-9"
          onClick={() => show(true)}
        >
          {t("open")}
        </Button>
      )}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className={done ? "text-sm wrap-anywhere" : "sr-only"}>
        {done ? t("done", { email: done }) : ""}
      </p>
    </div>
  );
}
