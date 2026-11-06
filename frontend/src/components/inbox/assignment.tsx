"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

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

  return (
    <div className="flex flex-col gap-2">
      <p ref={line} tabIndex={-1} className="text-sm font-medium focus-visible:outline-none">
        <Assignee who={detail} />
      </p>
      {said ? (
        <p
          role={said.error ? "alert" : "status"}
          className={said.error ? "text-danger text-sm" : "text-sm"}
        >
          {said.text}
        </p>
      ) : null}
      {can && closed ? <p className="text-muted text-sm">{t("closed")}</p> : null}
      {can && !closed ? (
        <div className="flex flex-wrap items-end gap-2">
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
