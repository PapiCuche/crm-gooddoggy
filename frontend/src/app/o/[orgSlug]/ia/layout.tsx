import type { ReactNode } from "react";

import { AiNav } from "@/components/ai/ai-nav";

export default function AiLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <AiNav />
      {children}
    </>
  );
}
