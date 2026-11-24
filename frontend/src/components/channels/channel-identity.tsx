import {
  Camera,
  CircleHelp,
  FlaskConical,
  type LucideIcon,
  MessageCircle,
  MessagesSquare,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { Plain } from "@/components/inbox/plain";
import type { PlatformEnum } from "@/lib/api/model";
import { cn } from "@/lib/utils";

// La identidad de cada red (UX-INBOX-01): la única fuente de su icono, su nombre y su color. El
// color es una capa que se suma al nombre, nunca la única señal: va solo en el icono y en el punto
// de la fila, jamás en un texto, una marca de entrega o un aviso. Los tonos son los tokens
// `--channel-*` de `globals.css`, todos ≥3:1 sobre los fondos claros (`channel-identity.test.tsx`).
// `social`: Instagram y Messenger, cuyos motivos de fallo nombran su red (`failureSocial`, F7b-06f).
// La Sandbox se rotula «Pruebas» y una red que esta versión no conoce, «Otra red»: las dos neutras.
export type Tone = "whatsapp" | "instagram" | "messenger" | "neutral";
type Identity = { icon: LucideIcon; labelKey: string; tone: Tone; social: boolean };
export const CHANNELS = {
  WHATSAPP: { icon: MessageCircle, labelKey: "WHATSAPP", tone: "whatsapp", social: false },
  INSTAGRAM: { icon: Camera, labelKey: "INSTAGRAM", tone: "instagram", social: true },
  MESSENGER: { icon: MessagesSquare, labelKey: "MESSENGER", tone: "messenger", social: true },
  SANDBOX: { icon: FlaskConical, labelKey: "TEST", tone: "neutral", social: false },
  UNKNOWN: { icon: CircleHelp, labelKey: "OTHER", tone: "neutral", social: false },
} as const satisfies Record<PlatformEnum | "UNKNOWN", Identity>;

// Clases enteras (Tailwind solo genera las que lee tal cual en el código).
export const TONES = {
  whatsapp: { icon: "text-channel-whatsapp", dot: "bg-channel-whatsapp" },
  instagram: { icon: "text-channel-instagram", dot: "bg-channel-instagram" },
  messenger: { icon: "text-channel-messenger", dot: "bg-channel-messenger" },
  neutral: { icon: "text-channel-neutral", dot: "bg-channel-neutral" },
} as const satisfies Record<Tone, { icon: string; dot: string }>;

// La plataforma llega de la API como texto: solo una clave propia del mapa (`Object.hasOwn`, ni
// `constructor` ni otra del prototipo) da su identidad; cualquier otra es «Otra red».
export function channelOf(platform: string): Identity {
  return Object.hasOwn(CHANNELS, platform)
    ? CHANNELS[platform as keyof typeof CHANNELS]
    : CHANNELS.UNKNOWN;
}

// El nombre visible de la red, de `channels.platform` (la misma clave que «Canales»).
export function useChannelName() {
  const t = useTranslations("channels.platform");
  return (platform: string) => t(channelOf(platform).labelKey);
}

// Icono con el color de la red y su nombre en texto (la tinta de alrededor, nunca el color).
export function ChannelMark({ platform }: { platform: string }) {
  const name = useChannelName();
  const { icon: Icon, tone } = channelOf(platform);
  return (
    <span className="text-foreground inline-flex shrink-0 items-center gap-1 font-medium">
      <Icon aria-hidden className={cn("size-3.5 shrink-0", TONES[tone].icon)} />
      {name(platform)}
    </span>
  );
}

// La red y, aparte, la cuenta que recibió la conversación (puede haber varias por red): un «·»
// que el lector de pantalla no lee las separa a la vista, y una coma oculta y un espacio fuera de
// ella las separan al oído («WhatsApp, Tienda Lima»). La cuenta es texto ajeno: va por `Plain`.
export function ChannelSource({ platform, account }: { platform: string; account: string }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      <ChannelMark platform={platform} />
      <span aria-hidden className="text-muted">
        ·
      </span>
      <span className="sr-only">,</span> <Plain className="text-muted min-w-0">{account}</Plain>
    </span>
  );
}

// El punto de la fila, junto al nombre del cliente: decorativo, la red ya se dice en texto.
export function ChannelDot({ platform, className }: { platform: string; className?: string }) {
  const { dot } = TONES[channelOf(platform).tone];
  return (
    <span
      aria-hidden
      data-channel-dot
      className={cn("size-2 shrink-0 rounded-full", dot, className)}
    />
  );
}
