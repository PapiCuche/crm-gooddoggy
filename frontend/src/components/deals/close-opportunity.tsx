"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { type KeyboardEvent, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { everyPage } from "@/components/catalog/categories-tree";
import { FieldsForm } from "@/components/forms/fields-form";
import { useReads } from "@/components/inventory/cell-picker";
import { moneyText } from "@/components/pricing/margins";
import { Button } from "@/components/ui/button";
import {
  getLostReasonsListQueryKey,
  lostReasonsList,
  opportunitiesLose,
  opportunitiesWin,
} from "@/lib/api/client";
import type { LostReason, Opportunity } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

import { AMOUNT } from "./create-opportunity";
import type { Mover } from "./move-opportunity";

type Panel = "win" | "lose" | null;
const NOTE_MAX = 1000;

// Ganar y perder desde una tarjeta abierta (F5-05d): cada botón abre un formulario bajo ella.
// Ganar pide el importe final (`POST …/win/`, mayor que 0) y perder un motivo de `GET …/lost-reasons/`
// (que solo se pide al abrir ese formulario) con una nota, obligatoria si el motivo la exige
// (`POST …/lose/`). Se ofrecen a todos: sin `opportunities.mark_won` o `mark_lost` la API responde
// 403 y se dice. Un 404 o un 409 (la oportunidad cambió, se cerró, le faltan datos) lo dice el
// tablero y cierra el formulario; un 400, un 403 o un fallo se dicen en el formulario, que
// conserva lo escrito. Mientras la tarjeta tiene una orden en curso sus botones esperan.
// Escape cierra (no mientras se envía, ni durante una composición).
export function CloseControls({ card, mover }: { card: Opportunity; mover: Mover }) {
  const t = useTranslations("deals.close");
  const [panel, setPanel] = useState<Panel>(null);
  const openers = useRef<Record<string, HTMLButtonElement | null>>({});
  const busy = mover.pending.has(card.id);
  if (card.status !== "OPEN") return null;

  function close() {
    const opener = panel && openers.current[panel];
    setPanel(null);
    opener?.focus(); // el botón sigue en pantalla: el foco vuelve a él
  }
  function onKeyDown(event: KeyboardEvent) {
    if (event.key === "Escape" && panel && !busy && !event.nativeEvent.isComposing) {
      event.stopPropagation();
      close();
    }
  }
  return (
    <div className="flex flex-col gap-2" onKeyDown={onKeyDown}>
      <div className="flex gap-2">
        {(["win", "lose"] as const).map((one) => (
          <Button
            key={one}
            ref={(button) => {
              openers.current[one] = button;
            }}
            variant="ghost"
            className="border-border min-h-11 border aria-disabled:opacity-50 sm:min-h-9"
            aria-label={t(`${one}Label`, { title: card.title })}
            aria-expanded={panel === one}
            aria-disabled={busy}
            onClick={() => busy || setPanel(panel === one ? null : one)}
          >
            {t(one)}
          </Button>
        ))}
      </div>
      {panel === "win" ? <WinForm card={card} mover={mover} onClose={close} /> : null}
      {panel === "lose" ? <LoseForm card={card} mover={mover} onClose={close} /> : null}
    </div>
  );
}

// Una orden de cambio de etapa como escritura del formulario: lo que `act` devuelve como error
// (400, 403…) lo lanza, para que `FieldsForm` lo diga junto a su campo.
function useClose<Input>(
  mover: Mover,
  card: Opportunity,
  run: (input: Input) => Parameters<Mover["act"]>[1],
  onClose: () => void,
) {
  const write = useMutation<void, ApiError, Input>({
    networkMode: "always",
    mutationFn: async (input) => {
      const { error } = await mover.act(card, run(input));
      if (error) throw error;
    },
    onSuccess: onClose,
  });
  return write;
}

type FormProps = { card: Opportunity; mover: Mover; onClose: () => void };
const FORM = "border-border bg-surface flex w-full flex-col gap-3 rounded-lg border p-3";

function WinForm({ card, mover, onClose }: FormProps) {
  const t = useTranslations("deals.close");
  const format = useFormatter();
  const slug = useTenant().organization.slug;
  const write = useClose<string>(
    mover,
    card,
    (amount) => ({
      call: () => opportunitiesWin(slug, card.id, { amount, version: card.version }),
      done: ({ amount: won, currency }) =>
        t("won", { title: card.title, amount: moneyText(format, won, currency) }),
      form: true,
    }),
    onClose,
  );
  return (
    <FieldsForm<"amount">
      title={t("winTitle")}
      heading={<h3 className="font-medium">{t("winTitle")}</h3>}
      className={FORM}
      fields={[
        {
          ...{ name: "amount", max: 15, inputMode: "decimal", defaultValue: card.amount },
          ...{ label: t("amount"), hint: t("amountHint"), required: t("amountRequired") },
          invalid: t("amountInvalid"),
          valid: (value) => AMOUNT.test(value) && Number(value) > 0,
        },
      ]}
      write={write}
      send={({ amount }) => write.mutate(amount)}
      denied={t("denied")}
      labels={{ submit: t("winSubmit"), busy: t("busy"), cancel: t("cancel") }}
      onCancel={onClose}
    />
  );
}

function LoseForm({ card, mover, onClose }: FormProps) {
  const t = useTranslations("deals.close");
  const slug = useTenant().organization.slug;
  const { failed, retry } = useReads();
  const legend = useId();
  const [reasonId, setReasonId] = useState("");
  const [asked, setAsked] = useState(false); // intentó enviar sin motivo
  // Los motivos activos, solo al abrir este formulario; nada queda entre visitas.
  const reasons = useQuery({
    queryKey: [...getLostReasonsListQueryKey(slug), "all"],
    queryFn: async ({ signal }) =>
      (await everyPage((params) => lostReasonsList(slug, params, { signal }))).filter(
        (one: LostReason) => one.is_active,
      ),
    gcTime: 0,
  });
  const chosen = reasons.data?.find(({ id }) => id === reasonId);
  const write = useClose<{ note: string }>(
    mover,
    card,
    ({ note }) => ({
      call: () =>
        opportunitiesLose(slug, card.id, {
          ...{ lost_reason_id: reasonId, version: card.version },
          ...(note ? { note } : {}),
        }),
      done: () => t("lost", { title: card.title, reason: chosen?.name ?? "" }),
      form: true,
    }),
    onClose,
  );
  const missing = asked && !chosen && !!reasons.data?.length;
  const problem = failed(reasons, t("reason.denied"));
  return (
    <FieldsForm<"note">
      title={t("loseTitle")}
      heading={<h3 className="font-medium">{t("loseTitle")}</h3>}
      className={FORM}
      fields={[
        {
          ...{
            name: "note",
            max: NOTE_MAX,
            label: t(chosen?.requires_note ? "note" : "noteOptional"),
          },
          required: chosen?.requires_note ? t("noteRequired") : undefined,
          invalid: t(chosen?.requires_note ? "noteRequired" : "noteInvalid"),
        },
      ]}
      note={
        <fieldset
          className="flex flex-col gap-1"
          aria-labelledby={legend}
          aria-describedby={missing ? `${legend}-error` : undefined}
        >
          <legend id={legend} className="text-sm font-medium">
            {t("reason.label")}
          </legend>
          {problem ? (
            <p role="alert" className="text-danger text-sm">
              {problem}
            </p>
          ) : null}
          {reasons.isPending ? (
            <p role="status" className="text-muted text-sm">
              {t("reason.loading")}
            </p>
          ) : null}
          {reasons.data?.length === 0 ? (
            <p className="text-muted text-sm">{t("reason.none")}</p>
          ) : null}
          {reasons.data?.map(({ id, name, requires_note: note }) => (
            <label key={id} className="flex min-h-11 items-center gap-2 sm:min-h-9">
              <input
                type="radio"
                name="reason"
                checked={reasonId === id}
                onChange={() => setReasonId(id)}
              />
              <span className="wrap-anywhere">
                {name}
                {note ? ` (${t("reason.needsNote")})` : ""}
              </span>
            </label>
          ))}
          {retry(reasons)}
          {missing ? (
            <p id={`${legend}-error`} role="alert" className="text-danger text-sm">
              {t("reason.required")}
            </p>
          ) : null}
        </fieldset>
      }
      check={() => {
        setAsked(true);
        return !!chosen;
      }}
      write={write}
      send={({ note }) => write.mutate({ note })}
      // El motivo que la API ya no acepta (se desactivó): se vuelve a leer la lista.
      taken={(error) => {
        const bad = error.code === "VALIDATION_ERROR" ? (error.fields ?? {}) : {};
        return Object.hasOwn(bad, "lost_reason_id") ? { text: t("reason.invalid") } : null;
      }}
      denied={t("denied")}
      labels={{ submit: t("loseSubmit"), busy: t("busy"), cancel: t("cancel") }}
      onCancel={onClose}
    />
  );
}
