import type { ReactNode } from "react";

import { InventoryNav } from "@/components/inventory/inventory-nav";

export default function InventoryLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <InventoryNav />
      {children}
    </>
  );
}
