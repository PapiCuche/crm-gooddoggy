"use client";

import { type QueryKey, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import {
  brandsList,
  categoriesList,
  getBrandsListQueryKey,
  getCategoriesListQueryKey,
  useProductsCreate,
} from "@/lib/api/client";
import { type ProductTypeEnum } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

import { type CategoryNode, everyPage, flatten, grow } from "./categories-tree";

const FIELDS = [
  { name: "name", max: 200 },
  { name: "sku", max: 64 },
  { name: "slug", max: 63 },
  { name: "short_description", max: 255 },
] as const;
const TYPES: ProductTypeEnum[] = ["PHYSICAL", "SERVICE"];

// Un `select` del formulario, con sus opciones `[valor, texto]`. Mientras se envía no cambia:
// lo enviado es lo que se ve.
function Choice({
  label,
  value,
  hint,
  busy,
  onChange,
  options,
}: {
  label: string;
  value: string;
  hint?: string;
  busy: boolean;
  onChange: (value: string) => void;
  options: [string, string][];
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {/* 16 px en el control: por debajo, Safari en iOS amplía la página al enfocarlo. */}
      <select
        id={id}
        value={value}
        aria-disabled={busy}
        aria-describedby={hint ? `${id}-hint` : undefined}
        className="bg-surface border-foreground/50 focus-visible:border-foreground h-11 w-full rounded-[10px] border px-3 text-[16px]"
        onChange={(event) => busy || onChange(event.target.value)}
      >
        {options.map(([key, text]) => (
          <option key={key} value={key}>
            {text}
          </option>
        ))}
      </select>
      {hint ? (
        <p id={`${id}-hint`} className="text-muted text-sm">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

// La marca o la categoría que la API no acepta (ya no existe en la organización).
function refused(error: ApiError): "brand_id" | "category_id" | null {
  if (error.code !== "VALIDATION_ERROR" || !error.fields) return null;
  if (Object.hasOwn(error.fields, "brand_id")) return "brand_id";
  return Object.hasOwn(error.fields, "category_id") ? "category_id" : null;
}

// Crear un producto (F3-10) con `POST /api/v1/o/{slug}/products/` (F3-06): nace en borrador con
// su variante por defecto, la del SKU. El slug es opcional, como en marcas; marca y categoría
// salen de sus listas, que se piden enteras al abrir el formulario.
export function ProductCreate({
  slug,
  listKey,
  onAsk,
  onDone,
}: {
  slug: string;
  listKey: QueryKey;
  onAsk: () => void; // se abre el formulario: el anuncio anterior ya no aplica
  onDone: (text: string) => void;
}) {
  const t = useTranslations("products.create");
  const inside = useTranslations("categories.parent");
  const kind = useTranslations("products.type");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [type, setType] = useState<ProductTypeEnum>("PHYSICAL");
  const [brand, setBrand] = useState("");
  const [category, setCategory] = useState("");
  // El último envío iba sin slug: un 400 o un 409 del slug habla del que la API formó.
  const [formed, setFormed] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const brands = useQuery({
    queryKey: [...getBrandsListQueryKey(slug), "all"],
    queryFn: ({ signal }) => everyPage((params) => brandsList(slug, params, { signal })),
    enabled: open,
    gcTime: 0,
  });
  const categories = useQuery({
    queryKey: [...getCategoriesListQueryKey(slug), "all"],
    queryFn: async ({ signal }) =>
      flatten(grow(await everyPage((params) => categoriesList(slug, params, { signal })))),
    enabled: open,
    gcTime: 0,
  });
  // Lo elegido que ya no está en su lista (se volvió a pedir) no se envía.
  const brandId = brands.data?.some((row) => row.id === brand) ? brand : "";
  const categoryId = categories.data?.some((row) => row.id === category) ? category : "";
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const create = useProductsCreate({
    mutation: {
      networkMode: "always",
      onSuccess: async (product) => {
        // Sigue «Creando…» hasta que la lista vuelve. Si releerla falla, se dice.
        await queryClient.invalidateQueries({ queryKey: listKey });
        const stale = queryClient.getQueryState(listKey)?.status === "error";
        setOpen(false);
        onDone(t(stale ? "doneStale" : "done", { name: product.name }));
      },
      // Una marca o categoría que ya no existe: sus listas se vuelven a pedir.
      onError: (error) => {
        if (refused(error as ApiError)) {
          void brands.refetch();
          void categories.refetch();
        }
      },
    },
  });
  const busy = create.isPending;

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el foco vuelve a «Crear producto», salvo que el usuario ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function show(next: boolean) {
    create.reset();
    if (next) {
      setType("PHYSICAL");
      setBrand("");
      setCategory("");
      onAsk();
    }
    setOpen(next);
  }
  // Mientras llega una lista, o si no llegó, se dice bajo su `select`: sin elegir se crea igual.
  const listHint = (query: { isPending: boolean; isError: boolean }) =>
    query.isError ? t("optionsFailed") : query.isPending ? t("optionsLoading") : undefined;
  const named = (row: { id: string; name: string }): [string, string] => [row.id, row.name];
  const names = new Map(categories.data?.map(named));
  // La sangría no se oye: con la madre en el texto, dos «Accesorios» se distinguen.
  const placed = (row: CategoryNode): [string, string] => {
    const parent = row.parent_id ? names.get(row.parent_id) : undefined;
    const text = parent ? inside("inside", { name: row.name, parent }) : row.name;
    return [row.id, "   ".repeat(row.depth - 1) + text];
  };

  return (
    <div
      className="flex flex-col items-start gap-3"
      // Enter mantenido repite la pulsación: reabriría el formulario o reenviaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {open ? (
        <FieldsForm
          title={t("title")}
          heading={<h2 className="font-medium">{t("title")}</h2>}
          className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
          fields={FIELDS.map((field) => ({
            ...field,
            label: t(field.name),
            invalid: field.name === "slug" && formed ? t("slugNeeded") : t(`${field.name}Invalid`),
            required:
              field.name === "name" || field.name === "sku"
                ? t(`${field.name}Required`)
                : undefined,
            hint:
              field.name === "sku" || field.name === "slug" ? t(`${field.name}Hint`) : undefined,
            literal: field.name === "sku" || field.name === "slug",
          }))}
          note={
            <>
              <Choice
                label={t("type")}
                value={type}
                hint={t("typeHint")}
                busy={busy}
                onChange={(value) => setType(value as ProductTypeEnum)}
                options={TYPES.map((code) => [code, kind(code)])}
              />
              <Choice
                label={t("brand")}
                value={brandId}
                hint={listHint(brands)}
                busy={busy}
                onChange={setBrand}
                options={[["", t("noBrand")], ...(brands.data ?? []).map(named)]}
              />
              <Choice
                label={t("category")}
                value={categoryId}
                hint={listHint(categories)}
                busy={busy}
                onChange={setCategory}
                options={[["", t("noCategory")], ...(categories.data ?? []).map(placed)]}
              />
            </>
          }
          write={create}
          // Lo vacío y opcional no se envía: el slug lo forma la API con el nombre.
          send={({ name, sku, slug: chosen, short_description }) => {
            setFormed(!chosen);
            create.mutate({
              orgSlug: slug,
              data: {
                name,
                sku,
                product_type: type,
                ...(chosen ? { slug: chosen } : {}),
                ...(brandId ? { brand_id: brandId } : {}),
                ...(categoryId ? { category_id: categoryId } : {}),
                ...(short_description ? { short_description } : {}),
              },
            });
          }}
          taken={(error) => {
            if (error.code === "PRODUCT_SLUG_TAKEN") {
              return { field: "slug", text: t(formed ? "slugTakenFormed" : "slugTaken") };
            }
            if (error.code === "SKU_TAKEN") return { field: "sku", text: t("skuTaken") };
            const gone = refused(error);
            return gone ? { text: t(gone === "brand_id" ? "brandGone" : "categoryGone") } : null;
          }}
          denied={t("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: t("cancel") }}
          onCancel={() => show(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant="accent"
          className="min-h-11 sm:min-h-9"
          onClick={() => show(true)}
        >
          {t("open")}
        </Button>
      )}
    </div>
  );
}
