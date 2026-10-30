"use client";

import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { isStepUpCancelled, useStepUp } from "@/components/security/use-step-up";
import { Button } from "@/components/ui/button";
import { costCreate } from "@/lib/api/client";
import type { PriceVariant } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

import { validAmount } from "./price-form";

// Devuelve el foco a la fila tras repintar: el botón puede ser otro nodo que el que abrió.
export const focusAfterPaint = (selector: string) =>
  requestAnimationFrame(() => document.querySelector<HTMLElement>(selector)?.focus());

type Props = {
  slug: string;
  variant: PriceVariant;
  current: string | null; // el costo vigente, si ya se leyó
  onSaved: () => void; // los márgenes se piden otra vez
};

// «Registrar costo» de una variante (F3-47, `product_cost.manage`): importe y motivo en un
// `<dialog>` modal nativo, con `POST …/pricing/variants/{id}/costs/`. La ruta pide un segundo
// factor reciente: `withStepUp` abre el diálogo del código y repite la petición una vez. El
// importe viaja como lo escribió la persona, en texto.
export function CostForm({ slug, variant, current, onSaved }: Props) {
  const t = useTranslations("prices.costs.form");
  const format = useFormatter();
  const { withStepUp, dialog: stepUp } = useStepUp();
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<{ pending: boolean; error: ApiError | null }>({
    ...{ pending: false, error: null },
  });
  const [saved, setSaved] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const id = useId();
  const name = `${variant.product_name} ${variant.name} (${variant.sku})`;
  const money = (amount: string) =>
    format.number(Number(amount), { style: "currency", currency: "PEN" });
  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal();
  }, [open]);

  function send(values: { amount: string; reason: string }) {
    setState({ pending: true, error: null });
    withStepUp(() => costCreate(slug, variant.id, values)).then(
      (cost) => {
        setState({ pending: false, error: null });
        setSaved(t("done", { sku: variant.sku, amount: money(cost.amount) }));
        dialog.current?.close(); // `close` devuelve el foco a la fila
        onSaved();
      },
      // Cancelar el código no es un fallo: el formulario sigue como estaba.
      (error: ApiError) =>
        setState({ pending: false, error: isStepUpCancelled(error) ? null : error }),
    );
  }

  return (
    <>
      <Button
        variant="ghost"
        className="border-border min-h-11 border sm:min-h-9"
        aria-haspopup="dialog"
        data-cost-form={variant.id}
        aria-label={t("openLabel", { name })}
        onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
        onClick={() => {
          setState({ pending: false, error: null });
          setSaved("");
          setOpen(true);
        }}
      >
        {t("open")}
      </Button>
      <p role="status" className={saved ? "text-sm wrap-anywhere" : "sr-only"}>
        {saved}
      </p>
      {open ? (
        <dialog
          ref={dialog}
          aria-labelledby={id}
          className="step-up-dialog bg-surface text-foreground border-border m-auto w-[calc(100%-2rem)] max-w-md rounded-[14px] border p-0 backdrop:bg-black/40"
          // Escape no cierra mientras se envía (ni mientras se pide el código).
          onCancel={(event) => state.pending && event.preventDefault()}
          onClose={() => {
            setOpen(false);
            focusAfterPaint(`[data-cost-form="${variant.id}"]`);
          }}
          onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
        >
          <FieldsForm<"amount" | "reason">
            title={t("title", { name })}
            heading={
              <div className="flex flex-col gap-1">
                <h2 id={id} className="text-lg font-semibold">
                  {t("heading")}
                </h2>
                <p className="text-muted text-sm wrap-anywhere">{name}</p>
              </div>
            }
            className="flex flex-col gap-4 p-4 sm:p-5"
            fields={[
              {
                ...{ name: "amount", max: 15, label: t("amount"), inputMode: "decimal" },
                ...{ required: t("amountRequired"), invalid: t("amountInvalid") },
                hint: current ? t("amountHint", { amount: money(current) }) : t("amountHintFirst"),
                valid: validAmount,
              },
              {
                ...{ name: "reason", max: 500, label: t("reason"), hint: t("reasonHint") },
                ...{ required: t("reasonRequired"), invalid: t("reasonInvalid") },
                valid: (text) => text.length >= 3,
              },
            ]}
            write={{
              ...{ isPending: state.pending, isError: !!state.error, error: state.error },
              reset: () => state.error && setState({ pending: false, error: null }),
            }}
            send={send}
            taken={(error) => {
              const field = error.code === "COST_UNCHANGED" ? "amount" : undefined;
              const text = {
                ...{ COST_UNCHANGED: t("unchanged"), COST_CONFLICT: t("conflict") },
                ...{ COST_TOO_SOON: t("tooSoon"), COST_REJECTED: t("rejected") },
                STEP_UP_REQUIRED: t("stepUp"),
              }[error.code as "COST_UNCHANGED"];
              return text ? { field, text } : null;
            }}
            denied={t("denied")}
            labels={{ submit: t("save"), busy: t("busy"), cancel: t("cancel") }}
            onCancel={() => dialog.current?.close()}
          />
        </dialog>
      ) : null}
      {stepUp}
    </>
  );
}
