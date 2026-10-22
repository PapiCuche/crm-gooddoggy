"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { Choice } from "@/components/catalog/product-create";
import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import {
  getBranchesListQueryKey,
  useWarehousesCreate,
  useWarehousesUpdate,
} from "@/lib/api/client";
import { type Branch, type Warehouse, WarehouseTypeEnum } from "@/lib/api/model";
import { cn } from "@/lib/utils";

export type Branches = Branch[] | "loading" | "failed";
// Lo que se elige fuera de los campos de texto, fijado al abrir: si la lista cambia debajo, el
// formulario sigue enseñando lo que el usuario abrió.
type Picked = Pick<Warehouse, "type" | "is_sellable" | "is_active">;
const FRESH: Picked = { type: "WAREHOUSE", is_sellable: true, is_active: true };
// Los campos de texto: el código solo al crear.
const NAME: ("code" | "name")[] = ["name"];
const CODE_AND_NAME: ("code" | "name")[] = ["code", "name"];

// Crear un almacén (sin `warehouse`) o cambiar uno (F3-13), con `POST …/warehouses/` y
// `PATCH …/warehouses/{id}/` (F3-08). El código y la sucursal se eligen al crear y no cambian:
// la API responde 400 si un `PATCH` los trae, así que editar no los envía. El camino de envío
// es el de `FieldsForm`. Quién puede hacerlo lo decide la API (`warehouses.manage`).
export function WarehouseForm(props: {
  slug: string;
  listKey: QueryKey;
  branches: Branches;
  warehouse?: Warehouse;
  onAsk: () => void; // se abre el formulario: los avisos anteriores ya no aplican
  onDone: (text: string) => void;
  onStale: (name: string, here: boolean) => void; // `here`: el foco seguía en esta fila
}) {
  const { slug, listKey, branches, warehouse } = props;
  const t = useTranslations("warehouses.form");
  const kind = useTranslations("warehouses.type");
  const queryClient = useQueryClient();
  const [picked, setPicked] = useState<Picked | null>(null); // `null`: cerrado
  const [branch, setBranch] = useState(""); // al crear
  const [branchMissing, setBranchMissing] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);

  async function saved(row: Warehouse) {
    // Sigue «Guardando…» hasta que la lista vuelve. Si releerla falla, se dice.
    await queryClient.invalidateQueries({ queryKey: listKey });
    const stale = queryClient.getQueryState(listKey)?.status === "error";
    setPicked(null);
    props.onDone(t(stale ? "doneStale" : "done", { name: row.name, created: String(!warehouse) }));
  }
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const create = useWarehousesCreate({
    mutation: {
      networkMode: "always",
      onSuccess: saved,
      // La sucursal ya no existe: la lista se vuelve a pedir y lo elegido deja de valer.
      onError: ({ fields }) =>
        fields?.branch_id &&
        queryClient.invalidateQueries({ queryKey: getBranchesListQueryKey(slug) }),
    },
  });
  const update = useWarehousesUpdate({
    mutation: {
      networkMode: "always",
      onSuccess: saved,
      onError: (error) => {
        // El almacén ya no existe: lo explica la lista, que se vuelve a pedir.
        if (error.status !== 404 || !warehouse) return;
        const active = document.activeElement;
        const here = active === document.body || !!root.current?.closest("li")?.contains(active);
        setPicked(null);
        props.onStale(warehouse.name, here);
      },
    },
  });
  const write = warehouse ? update : create;
  const busy = write.isPending;
  // Lo elegido que ya no está en la lista de sucursales (se volvió a pedir) no se envía.
  const known = Array.isArray(branches) ? branches : [];
  const branchId = known.some((row) => row.id === branch) ? branch : "";

  const opened = useRef(false);
  const open = picked !== null;
  useEffect(() => {
    // Al cerrar, el foco vuelve al botón que abrió, salvo que el usuario ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function show(next: boolean) {
    write.reset();
    setBranchMissing(false);
    if (next) props.onAsk();
    const { type, is_sellable, is_active } = warehouse ?? FRESH;
    setBranch("");
    setPicked(next ? { type, is_sellable, is_active } : null);
  }
  const pick = (change: Partial<Picked>) => setPicked((now) => now && { ...now, ...change });
  const title = warehouse ? t("editLabel", { name: warehouse.name }) : t("title");
  const toggle = (name: "is_sellable" | "is_active") => (
    <label className="flex min-h-11 items-center gap-2 text-sm sm:min-h-9">
      <input
        type="checkbox"
        className="size-4 shrink-0"
        checked={picked?.[name] ?? false}
        aria-disabled={busy}
        onChange={() => busy || pick({ [name]: !picked?.[name] })}
      />
      {t(name)}
    </label>
  );

  return (
    <div
      ref={root}
      className={cn("flex flex-col items-start gap-3", open && "w-full")}
      // Enter mantenido repite la pulsación: reabriría el formulario o reenviaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {picked ? (
        <FieldsForm
          title={title}
          heading={<h2 className="font-medium wrap-anywhere">{title}</h2>}
          className={cn(
            "flex w-full max-w-xl flex-col gap-4",
            !warehouse && "border-border bg-surface rounded-lg border p-4",
          )}
          fields={(warehouse ? NAME : CODE_AND_NAME).map((name) => ({
            name,
            max: name === "code" ? 20 : 100,
            label: t(name),
            required: t(`${name}Required`),
            invalid: t(`${name}Invalid`),
            hint: name === "code" ? t("codeHint") : undefined,
            literal: name === "code",
            defaultValue: name === "name" ? warehouse?.name : undefined,
          }))}
          note={
            <>
              {warehouse ? null : (
                <Choice
                  label={t("branch")}
                  value={branchId}
                  busy={busy}
                  error={branchMissing ? t("branchRequired") : undefined}
                  hint={typeof branches === "string" ? t(`branches_${branches}`) : undefined}
                  onChange={(branch_id) => {
                    setBranchMissing(false);
                    setBranch(branch_id);
                  }}
                  options={[
                    ["", t("chooseBranch")],
                    ...known.map(({ id, name, code, is_active }): [string, string] => [
                      id,
                      t("branchOption", { name, code, active: String(is_active) }),
                    ]),
                  ]}
                />
              )}
              <Choice
                label={t("type")}
                value={picked.type}
                busy={busy}
                onChange={(type) => pick({ type: type as WarehouseTypeEnum })}
                options={Object.values(WarehouseTypeEnum).map((code) => [code, kind(code)])}
              />
              {toggle("is_sellable")}
              {warehouse ? toggle("is_active") : null}
            </>
          }
          write={write}
          check={() => {
            setBranchMissing(!warehouse && !branchId);
            return !!warehouse || !!branchId;
          }}
          send={({ name, code }) => {
            if (!warehouse) {
              const data = { code, name, ...picked, branch_id: branchId };
              return create.mutate({ orgSlug: slug, data });
            }
            // Solo lo que cambió respecto a la fila: no pisa lo que otro cambió en lo demás.
            const data = Object.fromEntries(
              Object.entries({ name, ...picked }).filter(
                ([key, value]) => value !== warehouse[key as "name"],
              ),
            );
            update.mutate({ orgSlug: slug, warehouseId: warehouse.id, data });
          }}
          taken={(error) => {
            if (error.code === "WAREHOUSE_CODE_TAKEN")
              return { field: "code", text: t("codeTaken") };
            return error.fields?.branch_id ? { text: t("branchGone") } : null;
          }}
          denied={t("denied")}
          labels={{ submit: t(warehouse ? "save" : "open"), busy: t("busy"), cancel: t("cancel") }}
          onCancel={() => show(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant={warehouse ? "ghost" : "accent"}
          className={cn("min-h-11 sm:min-h-9", warehouse && "border-border border")}
          aria-label={warehouse ? t("editLabel", { name: warehouse.name }) : undefined}
          onClick={() => show(true)}
        >
          {t(warehouse ? "edit" : "open")}
        </Button>
      )}
    </div>
  );
}
