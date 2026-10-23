import { SectionNav } from "@/components/catalog/catalog-nav";

// Las páginas del inventario (F3-13, F3-14, F3-17, F3-22). Se ven con `inventory.view`, el de la entrada.
const PAGES = [
  { key: "stock", path: "/inventario/existencias" },
  { key: "movements", path: "/inventario/movimientos" },
  { key: "warehouses", path: "/inventario/almacenes" },
  { key: "import", path: "/inventario/importar" }, // la página explica el 403 sin `inventory.adjust`
] as const;

export const InventoryNav = () => <SectionNav section="inventory" pages={PAGES} />;
