import type { ReactNode } from "react";

import { CatalogNav } from "@/components/catalog/catalog-nav";

export default function CatalogLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <CatalogNav />
      {children}
    </>
  );
}
