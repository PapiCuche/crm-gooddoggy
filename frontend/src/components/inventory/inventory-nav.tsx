import { SectionNav } from "@/components/catalog/catalog-nav";

// Las páginas del inventario (F3-13). Todas leen con `inventory.view`, el de la entrada.
const PAGES = [{ key: "warehouses", path: "/inventario/almacenes" }] as const;

export const InventoryNav = () => <SectionNav section="inventory" pages={PAGES} />;
