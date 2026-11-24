import "./globals.css";

import { GeistMono } from "geist/font/mono";
import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { NextIntlClientProvider } from "next-intl";
import { getTranslations } from "next-intl/server";
import { connection } from "next/server";

import { LOCALE } from "@/i18n/request";

import { dmSans } from "./fonts";
import { Providers } from "./providers";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("app");
  return { title: t("name") };
}

// En Android el teclado encoge el viewport (y `dvh`): el Inbox cabe con el teclado abierto.
export const viewport: Viewport = { interactiveWidget: "resizes-content" };
export default async function RootLayout({ children }: { children: ReactNode }) {
  // La CSP usa un nonce por petición (src/proxy.ts): ninguna página se prerenderiza en el build.
  await connection();
  return (
    <html lang={LOCALE} className={`${dmSans.variable} ${GeistMono.variable}`}>
      <body>
        <NextIntlClientProvider>
          <Providers>{children}</Providers>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
