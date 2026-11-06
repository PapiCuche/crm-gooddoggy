"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { ChevronDown } from "lucide-react";
import { useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Choice } from "@/components/catalog/product-create";
import { Button } from "@/components/ui/button";
import {
  conversationAssign,
  getConversationsListQueryKey,
  getConversationsRetrieveQueryKey,
} from "@/lib/api/client";
import type { Assignment, AssignRequest, Conversation, ConversationDetail } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import { useMembers } from "./members";
import { messagesKey } from "./composer";
import { Plain } from "./plain";

type Who = Pick<Conversation, "assignee_kind" | "assigned_user_id">;

// «Asignada a quién»: a ti, a un miembro (si el cliente conoce su nombre) o sin asignar.
export function Assignee({ who }: { who: Who }) {
  const t = useTranslations("inbox.assignee");
  const { user } = useTenant();
  const { name } = useMembers();
  const id = who.assigned_user_id;
  if (id === user.id) return <>{t("mine")}</>;
  if (id === null) {
    const kind = who.assignee_kind;
    return <>{t(kind === "UNASSIGNED" || kind === "TEAM" ? kind : "other")}</>;
  }
  const named = name(id);
  return named === null ? (
    <>{t("other")}</>
  ) : (
    <>
      {t("to")} <Plain>{named}</Plain>
    </>
  );
}

// Tomar, transferir y soltar (F6-05b): `POST …/conversations/{id}/assign/`. Se ofrece con
// `conversations.assign` (el mismo permiso para las tres, D-F6-13) y con el detalle ya leído, porque
// manda siempre su `assignment_version`: si otra persona cambió la asignación, la API responde 409,
// se explica y se vuelve a leer. Es comodidad, no seguridad: la API decide.
export function AssignmentActions({ detail }: { detail: ConversationDetail }) {
  const t = useTranslations("inbox.assign");
  const errors = useTranslations("errors");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const client = useQueryClient();
  const { name, targets } = useMembers();
  const line = useRef<HTMLParagraphElement>(null);
  const [target, setTarget] = useState("");
  const [open, setOpen] = useState(false); // en 390 px las acciones van tras «Acciones»
  const panel = useId();
  const toggle = useRef<HTMLButtonElement>(null);
  const [said, setSaid] = useState<{ error: boolean; text: string } | null>(null);
  const detailKey = getConversationsRetrieveQueryKey(slug, detail.id);
  const can = permissions.some((grant) => grant.code === "conversations.assign");
  const closed = detail.status === "CLOSED";
  const holder = detail.assigned_user_id;
  const options = targets.filter(([id]) => id !== holder);

  const refresh = () =>
    Promise.all([
      client.invalidateQueries({ queryKey: detailKey }),
      client.invalidateQueries({ queryKey: getConversationsListQueryKey(slug) }),
      client.invalidateQueries({ queryKey: messagesKey(slug, detail.id) }),
    ]);
  const assign = useMutation<Assignment, ApiError, AssignRequest>({
    mutationFn: (request) => conversationAssign(slug, detail.id, request),
    onSuccess: (saved, request) => {
      // El estado nuevo se ve al instante; la relectura lo confirma.
      client.setQueryData<ConversationDetail>(detailKey, (old) => old && { ...old, ...saved });
      setTarget("");
      setOpen(false);
      setSaid({
        error: false,
        text:
          request.action === "TRANSFER"
            ? t("done.TRANSFER", { name: name(request.user_id ?? "") ?? t("someone") })
            : t(`done.${request.action}`),
      });
      void refresh();
      line.current?.focus(); // el botón pulsado puede haber desaparecido
    },
    onError: (error) => {
      let text = errors(`api.${apiErrorKey(error)}`);
      if (error.code === "ASSIGNMENT_CONFLICT") text = t("conflict");
      else if (error.code === "VALIDATION_ERROR" && error.fields && "user_id" in error.fields) {
        text = t("badTarget");
      }
      setSaid({ error: true, text });
      // Lo que cambió bajo nuestros pies se vuelve a leer: el estado y la versión.
      if (error.status === 409 || error.status === 404) void refresh();
    },
  });
  const run = (request: Omit<AssignRequest, "assignment_version">) => {
    if (assign.isPending) return;
    setSaid(null);
    assign.mutate({ ...request, assignment_version: detail.assignment_version });
  };
  const busy = assign.isPending;

  const acts = can && !closed;
  return (
    <div
      className="relative flex flex-col gap-1 lg:gap-2"
      onKeyDown={(event) => {
        if (event.key !== "Escape" || !open) return;
        setOpen(false);
        toggle.current?.focus();
      }}
    >
      <div className="flex items-center justify-between gap-2">
        <p ref={line} tabIndex={-1} className="text-sm font-medium focus-visible:outline-none">
          <Assignee who={detail} />
        </p>
        {acts ? (
          <Button
            ref={toggle}
            variant="ghost"
            className="border-border h-10 border lg:hidden"
            aria-expanded={open}
            aria-controls={panel}
            onClick={() => setOpen(!open)}
          >
            {t("actions")}
            <ChevronDown aria-hidden className={open ? "rotate-180" : undefined} />
          </Button>
        ) : null}
      </div>
      {said ? (
        <p
          role={said.error ? "alert" : "status"}
          className={said.error ? "text-danger text-sm" : "text-sm"}
        >
          {said.text}
        </p>
      ) : null}
      {can && closed ? <p className="text-muted text-sm">{t("closed")}</p> : null}
      {acts ? (
        <div
          id={panel}
          className={cn(
            "flex flex-wrap items-end gap-2",
            // En 390 px las acciones se abren sobre los mensajes, sin empujar el diseño.
            open
              ? "max-lg:bg-surface max-lg:absolute max-lg:inset-x-0 max-lg:top-full max-lg:z-10 max-lg:max-h-56 max-lg:overflow-y-auto max-lg:rounded-lg max-lg:border max-lg:p-3 max-lg:shadow-md"
              : "max-lg:hidden",
          )}
        >
          {holder === null ? (
            <Button
              variant="accent"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => run({ action: "TAKE" })}
            >
              {t("take")}
            </Button>
          ) : (
            <Button
              variant="ghost"
              className="border-border min-h-11 border sm:min-h-9"
              aria-disabled={busy}
              onClick={() => run({ action: "RELEASE" })}
            >
              {t("release")}
            </Button>
          )}
          {options.length > 0 ? (
            <div className="flex min-w-0 flex-wrap items-end gap-2">
              <div className="min-w-0 basis-44">
                <Choice
                  label={t("transferTo")}
                  value={target}
                  busy={busy}
                  onChange={setTarget}
                  options={[["", t("choose")], ...options]}
                />
              </div>
              <Button
                variant="ghost"
                className="border-border min-h-11 border sm:min-h-9"
                disabled={target === ""}
                aria-disabled={busy}
                onClick={() => target && run({ action: "TRANSFER", user_id: target })}
              >
                {t("transfer")}
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
