import {
  channelsSocialDisconnect,
  channelsSocialRotateToken,
  channelsWhatsappDisconnect,
  channelsWhatsappRotateToken,
} from "@/lib/api/client";

// La ruta de cada plataforma (F7b-06): WhatsApp tiene las suyas y Messenger e Instagram comparten
// `…/channels/{messenger|instagram}/`. La plataforma sale de la cuenta que dio la lista o del botón
// pulsado, nunca de lo que se escribe. Una plataforma que esta versión no conoce no tiene ruta.
export type Route = "whatsapp" | "messenger" | "instagram";
const ROUTES = { WHATSAPP: "whatsapp", MESSENGER: "messenger", INSTAGRAM: "instagram" } as const;
export const routeOf = (platform: string): Route | null =>
  Object.hasOwn(ROUTES, platform) ? ROUTES[platform as keyof typeof ROUTES] : null;

export const rotateToken = (slug: string, route: Route, id: string, access_token: string) =>
  route === "whatsapp"
    ? channelsWhatsappRotateToken(slug, id, { access_token })
    : channelsSocialRotateToken(slug, route, id, { access_token });

export const disconnect = (slug: string, route: Route, id: string) =>
  route === "whatsapp"
    ? channelsWhatsappDisconnect(slug, id)
    : channelsSocialDisconnect(slug, route, id);
