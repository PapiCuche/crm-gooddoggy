import type { ReactNode } from "react";

// Un enlace entre las pantallas de acceso: subrayado, para que no dependa solo del color.
export const LINK = "underline underline-offset-4 hover:no-underline";

// Tarjeta de las pantallas previas al workspace (login, selección de organización). Deriva de
// «Acceso demo» del Figma GOOD DOGGY (35:266): papel con borde de tinta sobre el lienzo.
export function AuthCard({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-10">
      <section className="border-foreground bg-surface flex w-full max-w-[460px] flex-col gap-5 rounded-[24px] border p-6 shadow-[0_1px_8px_rgb(32_32_32/0.12)] sm:p-8">
        <p className="text-[13px] tracking-wide uppercase">{eyebrow}</p>
        <h1 className="text-[32px] leading-[1.15] font-bold tracking-[0.02em]">{title}</h1>
        {children}
      </section>
    </main>
  );
}
