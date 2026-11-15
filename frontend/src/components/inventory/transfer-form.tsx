"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type RefObject, useEffect, useId, useRef, useState } from "react";

import { Choice } from "@/components/catalog/product-create";
import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import {
  getInventoryLevelsListQueryKey,
  getInventoryMovementsListQueryKey,
  getWarehousesListQueryKey,
  useInventoryTransfersCreate,
} from "@/lib/api/client";
import type { Variant, Warehouse } from "@/lib/api/model";

import { placed } from "./cell-picker";
import { freshKey } from "./movement-form";

// Transferir unidades de la celda elegida a otro almacén (F3-32), con `POST
// …/inventory/transfers/` (F3-31): una operación, dos movimientos del mismo `transfer_group_id`.
// Como en `MovementForm`, la `idempotency_key` sigue hasta que la API da la transferencia por
// hecha (o dice `IDEMPOTENCY_KEY_REUSED`): reintentar tras un fallo sin respuesta nunca mueve dos
// veces. Solo se mueve lo disponible (`on_hand - reserved`). Quién puede lo decide la API
// (`inventory.transfer`).
export function TransferForm({
  slug,
  warehouse,
  variant,
  available,
  warehouses,
  keyRef,
  onDone,
}: {
  slug: string;
  warehouse: Warehouse; // el origen
  variant: Variant;
  available: number; // lo disponible en el origen, tal como se ve en pantalla
  warehouses: readonly Warehouse[];
  keyRef: RefObject<string | null>; // la clave pendiente, de la pantalla
  onDone: (text: string | null) => void; // `null`: se abre el formulario
}) {
  const t = useTranslations("transfers");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [to, setTo] = useState("");
  const [noTarget, setNoTarget] = useState(false);
  // Lo que se envió, para anunciarlo aunque el usuario cambie la celda mientras se transfiere.
  const sentTo = useRef({ variant: variant.name, from: warehouse.name, to: "" });
  const trigger = useRef<HTMLButtonElement>(null);
  const why = useId();
  // Destino: un almacén activo y distinto del origen (la lista ya trae solo los que existen).
  const targets = warehouses.filter((row) => row.is_active && row.id !== warehouse.id);
  const target = targets.find((row) => row.id === to);
  const refresh = (key: readonly unknown[]) => queryClient.invalidateQueries({ queryKey: key });
  const create = useInventoryTransfersCreate({
    mutation: {
      networkMode: "always", // sin red falla y se dice; no queda en cola
      onSuccess: async ({ legs }) => {
        keyRef.current = null; // la siguiente, aunque sea igual, es otra transferencia
        void refresh(getInventoryMovementsListQueryKey(slug));
        // Sigue «Transfiriendo…» hasta que vuelven los saldos de los dos almacenes.
        await refresh(getInventoryLevelsListQueryKey(slug));
        setOpen(false);
        const [out, entry] = legs;
        onDone(
          t("done", {
            quantity: entry?.quantity ?? 0,
            from_on_hand: out?.on_hand_after ?? 0,
            to_on_hand: entry?.on_hand_after ?? 0,
            ...sentTo.current,
          }),
        );
      },
      onError: (error) => {
        if (error.code === "IDEMPOTENCY_KEY_REUSED") keyRef.current = null;
        // Lo que la respuesta dice que cambió se vuelve a pedir.
        if (["IDEMPOTENCY_KEY_REUSED", "INSUFFICIENT_STOCK", "STOCK_LIMIT"].includes(error.code)) {
          void refresh(getInventoryMovementsListQueryKey(slug));
          void refresh(getInventoryLevelsListQueryKey(slug));
        }
        const fields = error.fields ?? {};
        if (
          error.code === "WAREHOUSE_INACTIVE" ||
          fields.from_warehouse_id ||
          fields.to_warehouse_id
        )
          void refresh(getWarehousesListQueryKey(slug));
        if (fields.variant_id)
          void queryClient.invalidateQueries({
            predicate: ({ queryKey }) =>
              String(queryKey[0]).startsWith(`/api/v1/o/${slug}/products/`),
          });
      },
    },
  });

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el foco vuelve a «Transferir», salvo que el usuario esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);
  function show(next: boolean) {
    create.reset();
    setNoTarget(false);
    if (next) {
      onDone(null);
      setTo(targets.length === 1 ? targets[0]!.id : "");
    }
    setOpen(next);
  }

  if (!open) {
    const blocked = available < 1 || targets.length === 0; // se dice por qué, junto al botón
    return (
      <div className="flex flex-col items-start gap-1">
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-disabled={blocked}
          aria-describedby={blocked ? why : undefined}
          onClick={() => blocked || show(true)}
        >
          {t("open")}
        </Button>
        {blocked ? (
          <p id={why} className="text-muted text-sm">
            {t(available < 1 ? "nothing" : "noTargets")}
          </p>
        ) : null}
      </div>
    );
  }
  return (
    <div
      className="flex flex-col items-start"
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      <FieldsForm
        title={t("title")}
        heading={
          <h2 className="font-medium wrap-anywhere">
            {t("heading", { variant: variant.name, from: warehouse.name })}
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
          <Choice
            label={t("to")}
            value={target ? to : ""}
            error={noTarget ? t("toRequired") : undefined}
            busy={create.isPending}
            onChange={(id) => {
              setTo(id);
              setNoTarget(false);
            }}
            options={[
              ["", t("chooseTo")],
              ...targets.map((row): [string, string] => [row.id, t("option", placed(row))]),
            ]}
          />
        }
        write={create}
        check={() => {
          setNoTarget(!target);
          return !!target;
        }}
        send={({ quantity, reason }) => {
          sentTo.current = { variant: variant.name, from: warehouse.name, to: target!.name };
          keyRef.current ??= freshKey();
          const data = {
            variant_id: variant.id,
            from_warehouse_id: warehouse.id,
            to_warehouse_id: target!.id,
            quantity: Number(quantity),
            reason,
            idempotency_key: keyRef.current,
          };
          create.mutate({ orgSlug: slug, data });
        }}
        taken={(error) => {
          if (error.code === "INSUFFICIENT_STOCK")
            return { field: "quantity", text: t("insufficient") };
          if (error.code === "STOCK_LIMIT") return { field: "quantity", text: t("limit") };
          if (error.code === "WAREHOUSE_INACTIVE") return { text: t("inactive") };
          if (error.code === "IDEMPOTENCY_KEY_REUSED") return { text: t("reused") };
          const fields = error.fields ?? {};
          const gone = fields.variant_id || fields.from_warehouse_id || fields.to_warehouse_id;
          return gone ? { text: t("gone") } : null;
        }}
        denied={t("denied")}
        labels={{ submit: t("submit"), busy: t("busy"), cancel: t("cancel") }}
        onCancel={() => show(false)}
      />
    </div>
  );
}
