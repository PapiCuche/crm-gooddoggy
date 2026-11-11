"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import {
  getOpportunitiesListQueryKey,
  useOpportunitiesAssign,
  useOpportunitiesUpdate,
} from "@/lib/api/client";
import type { OpportunityDetail } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { AMOUNT } from "./create-opportunity";

// Lo que dice el formulario si la API respondió esto: la versión cambió, la oportunidad se cerró…
// (por `code`, nunca por el texto de la respuesta). `null`: uno genérico.
function why(error: ApiError, t: (key: string) => string) {
  if (error.status === 409) return t(error.code === "OPPORTUNITY_CLOSED" ? "closed" : "stale");
  return null;
}

// Editar una oportunidad abierta (F5-07): título, importe y próxima acción con `PATCH` y la
// versión que se leyó, y el responsable con `assign`. Se ofrecen a quien tiene `opportunities.update`
// y `opportunities.assign` (comodidad: la API decide y un 403 se explica). Solo viaja lo que cambió.
// Un 409 (otra persona la cambió) o un 404 vuelven a leer el detalle.
export function OpportunityEdit({
  detail,
  owners,
  saved,
  stale,
  started,
}: {
  detail: OpportunityDetail;
  owners: readonly (readonly [string, string])[]; // a quién se puede asignar
  saved: (next: OpportunityDetail, text: string) => void;
  stale: (text?: string) => void; // la API dijo que lo mostrado ya no vale: se vuelve a leer y se dice
  started: () => void; // empieza otra edición: el aviso de la anterior ya no aplica
}) {
  const t = useTranslations("deals.detail.edit");
  const errors = useTranslations("errors.api");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const held = (code: string) => permissions.some((grant) => grant.code === code);
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [owner, setOwner] = useState("");
  const [asked, setAsked] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const refreshLists = () =>
    void client.invalidateQueries({ queryKey: getOpportunitiesListQueryKey(slug) });
  const done = (next: OpportunityDetail, text: string) => {
    saved(next, text);
    refreshLists();
  };
  const update = useOpportunitiesUpdate({
    mutation: {
      networkMode: "always",
      onSuccess: (next) => {
        setOpen(false);
        done(next, t("done"));
        setTimeout(() => trigger.current?.focus());
      },
      // Cerrada mientras se editaba: al volver a leer, el formulario ya no está; se dice aquí.
      onError: (error) =>
        [404, 409].includes(error.status) &&
        stale(error.code === "OPPORTUNITY_CLOSED" ? t("closed") : undefined),
    },
  });
  const assign = useOpportunitiesAssign({
    mutation: {
      networkMode: "always",
      onSuccess: (next) => done(next, t("assigned")),
      onError: (error) => [404, 409].includes(error.status) && stale(why(error, t) ?? t("gone")),
    },
  });
  if (detail.status !== "OPEN") return null;
  const choice = owners.find(([id]) => id === owner);
  const canUpdate = held("opportunities.update");
  const canAssign = held("opportunities.assign") && owners.length > 0;

  return (
    <div className="flex flex-col gap-4">
      {canUpdate ? (
        open ? (
          <FieldsForm
            title={t("title")}
            heading={<h2 className="font-medium">{t("title")}</h2>}
            className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
            fields={[
              {
                ...{
                  name: "title",
                  max: 200,
                  label: t("fields.title"),
                  defaultValue: detail.title,
                },
                ...{ invalid: t("invalid.title"), required: t("required.title") },
              },
              {
                ...{ name: "amount", max: 15, label: t("fields.amount"), inputMode: "decimal" },
                ...{ defaultValue: detail.amount, invalid: t("invalid.amount") },
                ...{ required: t("required.amount"), valid: (text) => AMOUNT.test(text) },
              },
              {
                ...{ name: "next_action", max: 200, label: t("fields.next") },
                ...{ defaultValue: detail.next_action ?? "", invalid: t("invalid.next") },
              },
            ]}
            write={update}
            send={(values) => {
              // Solo lo que cambió; una próxima acción vacía la borra.
              const data = {
                ...(values.title !== detail.title ? { title: values.title } : {}),
                ...(Number(values.amount) !== Number(detail.amount)
                  ? { amount: values.amount }
                  : {}),
                ...(values.next_action !== (detail.next_action ?? "")
                  ? { next_action: values.next_action || null }
                  : {}),
              };
              if (Object.keys(data).length === 0) {
                setOpen(false); // nada cambió: no se pide nada
                return;
              }
              update.mutate({
                orgSlug: slug,
                opportunityId: detail.id,
                data: { ...data, version: detail.version },
              });
            }}
            taken={(error) => {
              const text = why(error, t);
              return text ? { text } : null;
            }}
            denied={t("denied")}
            labels={{ submit: t("submit"), busy: t("busy"), cancel: t("cancel") }}
            onCancel={() => {
              setOpen(false);
              setTimeout(() => trigger.current?.focus());
            }}
          />
        ) : (
          <Button
            ref={trigger}
            variant="ghost"
            className="border-border min-h-11 self-start border sm:min-h-9"
            onClick={() => {
              started();
              update.reset();
              setOpen(true);
            }}
          >
            {t("open")}
          </Button>
        )
      ) : null}
      {canAssign ? (
        <div className="flex flex-col gap-1.5">
          <div className="flex max-w-xl gap-2">
            <select
              value={owner}
              aria-label={t("assignLabel")}
              className="bg-surface border-foreground/50 h-11 min-w-0 flex-1 rounded-[10px] border px-2 text-[16px]"
              onChange={(event) => setOwner(event.target.value)}
            >
              <option value="">{t("assignPlaceholder")}</option>
              {owners.map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </select>
            <Button
              variant="primary"
              className="min-h-11 sm:min-h-9"
              aria-disabled={assign.isPending}
              onClick={() => {
                if (assign.isPending) return;
                started();
                if (!choice) return setAsked(true);
                setAsked(false);
                assign.mutate({
                  ...{ orgSlug: slug, opportunityId: detail.id },
                  data: { assigned_user_id: choice[0], version: detail.version },
                });
              }}
            >
              {assign.isPending ? t("assigning") : t("assign")}
            </Button>
          </div>
          {asked && !choice ? (
            <p role="alert" className="text-danger text-sm">
              {t("assignChoose")}
            </p>
          ) : null}
          {assign.error && ![404, 409].includes(assign.error.status) ? (
            <p role="alert" className="text-danger text-sm">
              {assign.error.status === 400
                ? t("assignInvalid")
                : assign.error.status === 403
                  ? t("denied")
                  : errors(apiErrorKey(assign.error))}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
