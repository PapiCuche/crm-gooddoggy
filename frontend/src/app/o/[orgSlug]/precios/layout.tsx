import type { ReactNode } from "react";

import { PricingNav } from "@/components/pricing/pricing-nav";

export default function PricingLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <PricingNav />
      {children}
    </>
  );
}
