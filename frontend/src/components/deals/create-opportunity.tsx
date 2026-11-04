"use client";

import { type InfiniteData, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { getOpportunitiesListQueryKey, useOpportunitiesCreate } from "@/lib/api/client";
import type {
  OpportunityCreateRequest,
  OpportunityDetail,
  PaginatedOpportunityList,
} from "@/lib/api/model";

import { type Customer, CustomerPicker } from "./customer-picker";
import type { Mover } from "./move-opportunity";

// Los límites son los del contrato (`OpportunityCreateRequest`). El importe es texto estricto:
// hasta 12 cifras enteras y 2 decimales, sin signo, y puede ser 0.
const FIELDS = [
  { name: "title", max: 200, required: true },
  { name: "amount", max: 15, inputMode: "decimal", hinted: true },
  { name: "next_action", max: 200 },
] as const;
type Name = (typeof FIELDS)[number]["name"];
export const AMOUNT = /^\d{1,12}(\.\d{1,2})?$/;
const idle = () => new Promise((resolve) => setTimeout(resolve, 50));

// Crear una oportunidad desde el tablero (F5-05c) con `POST …/opportunities/` (F5-04d), en el
// pipeline elegido: la API la deja en su primera etapa abierta y a quien crea como responsable.
// Se ofrece a todos: sin `opportunities.create` o `contacts.view` la API responde 403 y se
// dice. Al crear, espera a que acaben las órdenes de mover en curso, vuelve a pedir las listas y
// mira si la nueva está en ellas: si está, el foco va a su tarjeta; si no (un filtro o una página
// sin cargar), lo dice en lugar de callar. Escape cierra sin enviar; lo escrito se conserva si
// la API rechaza. Desde la ficha del cliente (F5-06) `customer` ya viene elegido, sin buscador,
// y no hay órdenes de mover (`mover`) que esperar.
export function OpportunityCreate({
  slug,
  pipelineId,
  ready,
  mover,
  customer,
}: {
  slug: string;
  pipelineId: string;
  ready: boolean; // el pipeline tiene una etapa abierta y activa donde dejarla
  mover?: Mover;
  customer?: Customer;
}) {
  const t = useTranslations("deals.create");
  const client = useQueryClient();
  const lists = { queryKey: getOpportunitiesListQueryKey(slug) };
  const [open, setOpen] = useState(false);
  const [said, setSaid] = useState("");
  const [picked, setPicked] = useState<Customer | null>(null);
  const who = customer ?? picked;
  const [asked, setAsked] = useState(false); // intentó enviar sin cliente
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);

  async function arrived(created: OpportunityDetail) {
    // Se relee la lista cuando no hay órdenes de mover en curso; si una empieza durante la lectura,
    // cancela esa lectura (su resultado no vale) y se espera a que acabe para leer otra vez.
    for (let tries = 0; tries < 3; tries++) {
      while (mover && mover.sending.current.size > 0) await idle();
      await client.invalidateQueries(lists);
      if (!mover || mover.sending.current.size === 0) break;
    }
    const failed = client
      .getQueryCache()
      .findAll(lists)
      .some((q) => q.state.status === "error");
    const shown = client
      .getQueriesData<InfiniteData<PaginatedOpportunityList>>(lists)
      .some(([, data]) =>
        data?.pages.some((page) => page.results.some((r) => r.id === created.id)),
      );
    const values = { title: created.title, stage: created.stage.name };
    setSaid(t(failed ? "stale" : shown ? "done" : "hidden", values));
    setOpen(false);
    // La tarjeta nueva aparece con la lista: el foco va a ella cuando ya está pintada, salvo que
    // el usuario ya esté en otra parte (el botón lo recibe al cerrar, y es de quien lo puso).
    if (shown && !failed) {
      setTimeout(() => {
        const { activeElement } = document;
        if (activeElement === document.body || activeElement === trigger.current) {
          document.getElementById(`opportunity-${created.id}`)?.focus();
        }
      }, 0);
    }
  }
  const create = useOpportunitiesCreate({
    mutation: {
      networkMode: "always",
      onSuccess: arrived,
      // Un cliente que la API rechaza ya no se muestra como elegido.
      onError: (error) => {
        if (error.code === "VALIDATION_ERROR" && Object.hasOwn(error.fields ?? {}, "contact_id")) {
          setPicked(null);
        }
      },
    },
  });
  const hint = useId();
  const busy = create.isPending;

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el foco vuelve a «Nueva oportunidad», salvo que el usuario ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function show(next: boolean) {
    create.reset();
    if (next) {
      setPicked(null);
      setAsked(false);
      setSaid("");
    }
    setOpen(next);
  }

  return (
    <div
      className="flex flex-col items-start gap-3"
      // Escape cierra sin enviar (no mientras se envía); Enter mantenido no repite el envío.
      onKeyDown={(event) => {
        // Escape en la búsqueda de cliente es de la búsqueda, y durante una composición (IME)
        // cancela la composición: ninguno cierra el formulario.
        const inSearch = event.target instanceof HTMLInputElement && event.target.type === "search";
        if (
          event.key === "Escape" &&
          open &&
          !busy &&
          !inSearch &&
          !event.nativeEvent.isComposing
        ) {
          show(false);
        }
        if (event.repeat && event.key === "Enter") event.preventDefault();
      }}
    >
      {open ? (
        <FieldsForm<Name>
          title={t("heading")}
          heading={<h2 className="font-medium">{t("heading")}</h2>}
          className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
          fields={FIELDS.map((field) => ({
            ...field,
            label: t(field.name),
            invalid: t(`${field.name}Invalid`),
            required: "required" in field ? t("titleRequired") : undefined,
            hint: "hinted" in field ? t(`${field.name}Hint`) : undefined,
            valid: field.name === "amount" ? (value: string) => AMOUNT.test(value) : undefined,
          }))}
          note={
            customer ? undefined : (
              <CustomerPicker
                slug={slug}
                picked={picked}
                onPick={setPicked}
                searchRef={search}
                error={asked && !picked ? t("customer.required") : undefined}
              />
            )
          }
          check={() => {
            setAsked(true);
            if (!who) search.current?.focus(); // sin permiso para buscar no hay a dónde ir
            return !!who;
          }}
          write={create}
          // Solo lo escrito; el responsable y la etapa los pone la API.
          send={(values) => {
            const data: OpportunityCreateRequest = {
              contact_id: who!.id,
              title: values.title,
              pipeline_id: pipelineId,
            };
            if (values.amount) data.amount = values.amount;
            if (values.next_action) data.next_action = values.next_action;
            create.mutate({ orgSlug: slug, data });
          }}
          // Los campos que el formulario no tiene como texto se dicen aparte; un 409 también.
          taken={(error) => {
            const bad = error.code === "VALIDATION_ERROR" ? (error.fields ?? {}) : {};
            if (error.status === 409) return { text: t("conflict") };
            if (Object.hasOwn(bad, "contact_id")) return { text: t("contactInvalid") };
            if (Object.hasOwn(bad, "pipeline_id")) return { text: t("pipelineInvalid") };
            return null;
          }}
          denied={t("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: t("cancel") }}
          onCancel={() => show(false)}
        />
      ) : (
        <>
          <Button
            ref={trigger}
            variant="accent"
            className="min-h-11 aria-disabled:cursor-not-allowed aria-disabled:opacity-50 sm:min-h-9"
            aria-disabled={!ready}
            aria-describedby={ready ? undefined : `${hint}-stage`}
            onClick={() => ready && show(true)}
          >
            {t("open")}
          </Button>
          {ready ? null : (
            <p id={`${hint}-stage`} className="text-muted text-sm">
              {t("noStage")}
            </p>
          )}
        </>
      )}
      {/* Montado desde el principio: un lector de pantalla lo anuncia cuando cambia. */}
      <p role="status" className={said ? "text-sm wrap-anywhere" : "sr-only"}>
        {said}
      </p>
    </div>
  );
}
