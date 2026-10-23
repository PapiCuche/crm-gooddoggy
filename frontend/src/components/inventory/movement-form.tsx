"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type RefObject, useEffect, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import {
  getInventoryLevelsListQueryKey,
  getInventoryMovementsListQueryKey,
  getWarehousesListQueryKey,
  useInventoryMovementsCreate,
} from "@/lib/api/client";
import {
  MovementCreateMovementTypeEnum as Kinds,
  type Variant,
  type Warehouse,
} from "@/lib/api/model";

type Kind = keyof typeof Kinds;
const KINDS = Object.values(Kinds);
// Una clave por movimiento (`[A-Za-z0-9._:-]{8,100}` en la API): 32 cifras hexadecimales al
// azar. `getRandomValues` existe también fuera de HTTPS, a diferencia de `randomUUID`.
const freshKey = () =>
  Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
// Un entero sin signo, como lo pide la API: desde 1 (desde 0 en un ajuste) hasta un millón.
const counts = (kind: Kind) => (value: string) =>
  /^\d{1,7}$/.test(value) &&
  Number(value) <= 1_000_000 &&
  (kind === "ADJUSTMENT" || Number(value) > 0);

// Registrar una entrada, una salida o un ajuste (lo contado) de la celda elegida (F3-15), con
// `POST …/inventory/movements/` (F3-11). Cada movimiento lleva su `idempotency_key`, y la clave
// sigue hasta que la API da uno por registrado (o dice `IDEMPOTENCY_KEY_REUSED`): tras un fallo
// sin respuesta, el siguiente envío lleva la misma aunque se haya corregido algo. Si el primero sí
// se registró, la API lo devuelve (mismos datos) o responde `IDEMPOTENCY_KEY_REUSED` (otros
// datos), y nunca cuenta dos veces. Quién puede hacerlo lo decide la API (`inventory.adjust`).
export function MovementForm({
  slug,
  warehouse,
  variant,
  keyRef,
  onDone,
}: {
  slug: string;
  warehouse: Warehouse;
  variant: Variant;
  // La clave pendiente, de la pantalla: sobrevive a cerrar el formulario o cambiar de celda.
  keyRef: RefObject<string | null>;
  onDone: (text: string | null) => void; // `null`: se abre el formulario; el anuncio ya no aplica
}) {
  const t = useTranslations("movements.form");
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<Kind | null>(null); // `null`: cerrado
  // La celda tal como era al enviar: el usuario puede cambiarla mientras se registra.
  const sentTo = useRef({ variant: variant.name, warehouse: warehouse.name });
  const trigger = useRef<HTMLButtonElement>(null);
  const create = useInventoryMovementsCreate({
    mutation: {
      networkMode: "always", // sin red falla y se dice; no queda en cola para después
      onSuccess: async (movement) => {
        keyRef.current = null; // el siguiente, aunque sea igual, es otro movimiento
        // Sigue «Registrando…» hasta que vuelve el saldo; el historial se pedirá de nuevo.
        void queryClient.invalidateQueries({ queryKey: getInventoryMovementsListQueryKey(slug) });
        await queryClient.invalidateQueries({ queryKey: getInventoryLevelsListQueryKey(slug) });
        setKind(null);
        const on_hand = movement.on_hand_after;
        onDone(t(`done.${movement.movement_type as Kind}`, { on_hand, ...sentTo.current }));
      },
      onError: (error) => {
        // Un envío anterior sin respuesta sí se registró, con otros datos: el saldo y el historial
        // se vuelven a pedir, y el siguiente envío, si el usuario lo quiere, lleva clave nueva.
        if (error.code === "IDEMPOTENCY_KEY_REUSED") {
          keyRef.current = null;
          void queryClient.invalidateQueries({ queryKey: getInventoryMovementsListQueryKey(slug) });
          void queryClient.invalidateQueries({ queryKey: getInventoryLevelsListQueryKey(slug) });
        }
        // La variante o el almacén ya no existen: almacenes, productos y variantes, otra vez.
        if (error.fields?.variant_id || error.fields?.warehouse_id) {
          void queryClient.invalidateQueries({ queryKey: getWarehousesListQueryKey(slug) });
          void queryClient.invalidateQueries({
            predicate: ({ queryKey }) =>
              String(queryKey[0]).startsWith(`/api/v1/o/${slug}/products/`),
          });
        }
      },
    },
  });

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el foco vuelve a «Registrar movimiento», salvo que el usuario esté en otra parte.
    if (kind === null && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = kind !== null;
  }, [kind]);
  function show(next: Kind | null) {
    create.reset();
    if (next) onDone(null);
    setKind(next);
  }

  return (
    <div
      className="flex flex-col items-start gap-3"
      // Enter mantenido repite la pulsación: reabriría el formulario o reenviaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {kind ? (
        <FieldsForm
          title={t("title")}
          heading={<h2 className="font-medium">{t("title")}</h2>}
          className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
          fields={(["quantity", "reason"] as const).map((name) => ({
            name,
            max: name === "quantity" ? 7 : 255,
            label: t(name === "quantity" ? `quantity.${kind}` : "reason"),
            required: t(`${name}Required`),
            invalid: t(name === "quantity" ? `quantityInvalid.${kind}` : "reasonInvalid"),
            hint: t(name === "quantity" ? `quantityHint.${kind}` : "reasonHint"),
            inputMode: name === "quantity" ? ("numeric" as const) : undefined,
            valid: name === "quantity" ? counts(kind) : undefined,
          }))}
          note={
            <fieldset className="flex flex-col gap-1">
              <legend className="mb-1 text-sm font-medium">{t("kind")}</legend>
              {KINDS.map((code) => (
                <label key={code} className="flex min-h-11 items-center gap-2 text-sm sm:min-h-9">
                  <input
                    type="radio"
                    name="movement_type"
                    className="size-4 shrink-0"
                    checked={kind === code}
                    aria-disabled={create.isPending}
                    onChange={() => create.isPending || setKind(code)}
                  />
                  {t(`kinds.${code}`)}
                </label>
              ))}
            </fieldset>
          }
          write={create}
          send={({ quantity, reason }) => {
            const data = {
              variant_id: variant.id,
              warehouse_id: warehouse.id,
              movement_type: kind,
              quantity: Number(quantity),
              reason,
            };
            sentTo.current = { variant: variant.name, warehouse: warehouse.name };
            keyRef.current ??= freshKey();
            create.mutate({ orgSlug: slug, data: { ...data, idempotency_key: keyRef.current } });
          }}
          taken={(error) => {
            if (error.code === "INSUFFICIENT_STOCK")
              return { field: "quantity", text: t("insufficient") };
            if (error.code === "WAREHOUSE_INACTIVE") return { text: t("inactive") };
            if (error.code === "IDEMPOTENCY_KEY_REUSED") return { text: t("reused") };
            const gone = error.fields?.variant_id || error.fields?.warehouse_id;
            return gone ? { text: t("gone") } : null;
          }}
          denied={t("denied")}
          labels={{ submit: t(`submit.${kind}`), busy: t("busy"), cancel: t("cancel") }}
          onCancel={() => show(null)}
        />
      ) : (
        <Button
          ref={trigger}
          variant="accent"
          className="min-h-11 sm:min-h-9"
          onClick={() => show("RECEIPT")}
        >
          {t("open")}
        </Button>
      )}
    </div>
  );
}
