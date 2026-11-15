"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useFormatter, useTimeZone, useTranslations } from "next-intl";
import { type RefObject, useEffect, useId, useRef, useState } from "react";

import { Choice } from "@/components/catalog/product-create";
import { FieldsForm } from "@/components/forms/fields-form";
import { fieldsOf, instantAt } from "@/components/tasks/task-time";
import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { getWarehousesListQueryKey, useInventoryReservationsCreate } from "@/lib/api/client";
import type { Variant, Warehouse } from "@/lib/api/model";

import { refetchCell, SHORT } from "./cell-reservations";
import { freshKey } from "./movement-form";

const DAY = 86_400_000;
const QUICK = [1, 3, 7]; // días desde ahora
// La reserva enviada y aún sin respuesta: su clave y el `expires_at` exacto que llevó, elegido
// con `picked` (los días, o el día). La API solo reconoce un reintento con el mismo instante.
export type PendingReservation = { key: string; picked: string; at: string };

// Reservar unidades disponibles de la celda elegida (F3-36) con `POST …/inventory/reservations/`
// (F3-35): cantidad de 1 a lo disponible, vencimiento (a 1, 3 o 7 días, o al terminar un día
// elegido, en la zona de la aplicación; la API admite 30 días como mucho) y motivo. La clave
// sigue, como en `MovementForm`, hasta que la API da la reserva por hecha (o dice
// `IDEMPOTENCY_KEY_REUSED`); con el mismo vencimiento elegido, el reintento lleva también el mismo
// `expires_at`. Quién puede lo decide la API (`inventory.manage`).
export function ReservationForm({
  slug,
  warehouse,
  variant,
  available,
  keyRef,
  onDone,
}: {
  slug: string;
  warehouse: Warehouse;
  variant: Variant;
  available: number; // lo disponible, tal como se ve en pantalla
  keyRef: RefObject<PendingReservation | null>; // de la pantalla: sobrevive a cerrar
  onDone: (text: string | null) => void; // `null`: se abre el formulario
}) {
  const t = useTranslations("reservations");
  const format = useFormatter();
  const zone = useTimeZone() ?? "America/Lima";
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [expiry, setExpiry] = useState("3");
  const [day, setDay] = useState("");
  const [days, setDays] = useState({ first: "", last: "" }); // los que valen, al abrir
  const [asked, setAsked] = useState(false); // se intentó enviar
  // Fechas ISO del mismo largo: compararlas como texto es compararlas como días.
  const badDay = expiry === "day" && !(day.length === 10 && day >= days.first && day <= days.last);
  const sentTo = useRef({ variant: variant.name, warehouse: warehouse.name });
  const trigger = useRef<HTMLButtonElement>(null);
  const why = useId();
  const create = useInventoryReservationsCreate({
    mutation: {
      networkMode: "always", // sin red falla y se dice; no queda en cola
      onSuccess: async (made) => {
        keyRef.current = null; // la siguiente, aunque sea igual, es otra reserva
        const cell = await refetchCell(queryClient, slug, made); // sigue «Reservando…»
        setOpen(false);
        const at = format.dateTime(new Date(made.expires_at), SHORT);
        const done = t("done", { quantity: made.quantity, at, ...sentTo.current }); // al enviar
        onDone([done, cell && t("figures", cell)].filter(Boolean).join(" "));
      },
      onError: (error) => {
        if (error.code === "IDEMPOTENCY_KEY_REUSED") keyRef.current = null;
        if (["IDEMPOTENCY_KEY_REUSED", "INSUFFICIENT_STOCK"].includes(error.code))
          void refetchCell(queryClient, slug, {
            variant_id: variant.id,
            warehouse_id: warehouse.id,
          });
        if (error.code === "WAREHOUSE_INACTIVE" || error.fields?.warehouse_id)
          void queryClient.invalidateQueries({ queryKey: getWarehousesListQueryKey(slug) });
      },
    },
  });

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el foco vuelve a «Reservar», salvo que el usuario esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);
  function show(next: boolean) {
    create.reset();
    setAsked(false);
    if (next) {
      onDone(null);
      const now = Date.now();
      setDays({ first: fieldsOf(now, zone).date, last: fieldsOf(now + 29 * DAY, zone).date });
    }
    setOpen(next);
  }
  // El instante en que vence lo elegido: dentro de N días, o al terminar ese día en la zona.
  function expiresAt(picked: string) {
    if (!picked.includes("-")) return new Date(Date.now() + Number(picked) * DAY).toISOString();
    const [y, m, d] = picked.split("-").map(Number) as [number, number, number];
    return new Date(instantAt(zone, y, m, d + 1) - 60_000).toISOString();
  }

  if (!open) {
    return (
      <div className="flex flex-col items-start gap-1">
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-disabled={available < 1}
          aria-describedby={available < 1 ? why : undefined}
          onClick={() => available < 1 || show(true)}
        >
          {t("open")}
        </Button>
        {available < 1 ? (
          <p id={why} className="text-muted text-sm">
            {t("nothing")}
          </p>
        ) : null}
      </div>
    );
  }
  const onDay = expiry === "day";
  return (
    <div
      className="flex flex-col items-start"
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      <FieldsForm
        title={t("formTitle")}
        heading={
          <h2 className="font-medium wrap-anywhere">
            {t("heading", { variant: variant.name, warehouse: warehouse.name })}
          </h2>
        }
        className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
        fields={[
          {
            name: "quantity",
            max: 7,
            label: t("quantity"),
            required: t("quantityRequired"),
            invalid: t("quantityInvalid", { available }),
            hint: t("quantityHint", { available }),
            inputMode: "numeric",
            valid: (value) => /^\d{1,7}$/.test(value) && +value >= 1 && +value <= available,
          },
          {
            name: "reason",
            max: 255,
            label: t("reason"),
            required: t("reasonRequired"),
            invalid: t("reasonInvalid"),
            hint: t("reasonHint"),
          },
        ]}
        note={
          <>
            <Choice
              label={t("expiry")}
              value={expiry}
              hint={t("expiryHint")}
              busy={create.isPending}
              onChange={setExpiry}
              options={[
                ...QUICK.map((count): [string, string] => [String(count), t("in", { count })]),
                ["day", t("onDay")],
              ]}
            />
            {onDay ? (
              <TextField
                type="date"
                label={t("day")}
                min={days.first}
                max={days.last}
                value={day}
                readOnly={create.isPending}
                hint={t("dayHint")}
                error={asked && badDay ? t("dayInvalid", { last: days.last }) : undefined}
                onChange={(event) => setDay(event.target.value)}
              />
            ) : null}
          </>
        }
        write={create}
        check={() => {
          setAsked(true);
          return !badDay;
        }}
        send={({ quantity, reason }) => {
          sentTo.current = { variant: variant.name, warehouse: warehouse.name };
          const picked = onDay ? day : expiry;
          const before = keyRef.current;
          const at = before?.picked === picked ? before.at : expiresAt(picked);
          keyRef.current = { key: before?.key ?? freshKey(), picked, at };
          const data = {
            ...{ variant_id: variant.id, warehouse_id: warehouse.id },
            ...{ quantity: Number(quantity), reason, expires_at: at },
            idempotency_key: keyRef.current.key,
          };
          create.mutate({ orgSlug: slug, data });
        }}
        taken={(error) => {
          // Por `code`; un 400 de `expires_at`, `variant_id` o `warehouse_id`, con su propio texto.
          const { expires_at, variant_id, warehouse_id } = error.fields ?? {};
          const code = expires_at ? "LATE" : variant_id || warehouse_id ? "GONE" : error.code;
          if (!t.has(`codes.${code}`)) return null;
          const field = code === "INSUFFICIENT_STOCK" ? ("quantity" as const) : undefined;
          return { field, text: t(`codes.${code}`) };
        }}
        denied={t("denied")}
        labels={{ submit: t("submit"), busy: t("busy"), cancel: t("cancel") }}
        onCancel={() => show(false)}
      />
    </div>
  );
}
