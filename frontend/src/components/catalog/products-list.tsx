"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList } from "@/components/lists/cursor-list";
import { getProductsListQueryKey, productsList } from "@/lib/api/client";
import type { Product } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { ProductCreate } from "./product-create";

// Productos del catálogo (F3-10): lo que devuelve `GET /api/v1/o/{slug}/products/` (F3-06),
// página a página y por orden de creación. La lista, sus estados y su foco son los de
// `CursorList`. Quién puede verlos o crearlos lo decide la API (`products.view`,
// `products.manage`).
export function ProductsList() {
  const t = useTranslations("products");
  const { organization, permissions } = useTenant();
  // Comodidad: crear se ofrece a quien la API dijo que tiene `products.manage`.
  const canManage = permissions.some((grant) => grant.code === "products.manage");
  const listKey = [...getProductsListQueryKey(organization.slug), "pages"];
  const [done, setDone] = useState<string | null>(null);

  return (
    <CursorList<Product>
      section="products"
      organization={organization.name}
      listKey={listKey}
      fetchPage={(cursor, signal) =>
        productsList(organization.slug, cursor ? { cursor } : undefined, { signal })
      }
      notice={
        canManage ? (
          <>
            <ProductCreate
              slug={organization.slug}
              listKey={listKey}
              onAsk={() => setDone(null)}
              onDone={setDone}
            />
            {/* Montado desde el principio: un lector de pantalla lo anuncia cuando cambia. */}
            <p role="status" className={done ? "text-sm wrap-anywhere" : "sr-only"}>
              {done ?? ""}
            </p>
          </>
        ) : null
      }
      empty={<p className="text-muted">{t("empty")}</p>}
      rowClassName="grid gap-x-4 gap-y-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start"
    >
      {(product) => (
        <>
          <p className="flex min-w-0 flex-col leading-snug">
            <span className="font-medium wrap-anywhere">{product.name}</span>
            <span className="text-muted font-mono text-[13px] wrap-anywhere">{product.slug}</span>
            {product.brand ? (
              <span className="text-muted text-sm wrap-anywhere">
                {t("brand", { name: product.brand.name })}
              </span>
            ) : null}
            {product.category ? (
              <span className="text-muted text-sm wrap-anywhere">
                {t("category", { name: product.category.name })}
              </span>
            ) : null}
          </p>
          <p className="flex flex-wrap gap-x-3 text-sm sm:flex-col sm:items-end">
            <span
              className={cn(
                "font-medium",
                product.status === "ACTIVE" ? "text-success" : "text-muted",
              )}
            >
              {t(`status.${product.status}`)}
            </span>
            <span className="text-muted">{t(`type.${product.product_type}`)}</span>
          </p>
        </>
      )}
    </CursorList>
  );
}
