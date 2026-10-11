// Destino tras iniciar sesión. Viene de la URL, así que solo vale una página de este origen.
// No basta con mirar cómo empieza: `/.//otro.sitio` es una ruta de este origen que, una vez
// resueltos sus puntos, queda en `//otro.sitio`, y el navegador la lee como otro sitio. Por
// eso se resuelve como lo haría el navegador y se devuelve la ruta ya resuelta.
export const DEFAULT_NEXT = "/o";
const ORIGIN = "https://destino.invalid"; // base para resolver; nunca se navega a ella
// Tampoco lo que no es una página ni el propio login, que dejaría dando vueltas a quien ya entró.
// Ni las páginas de recuperación: un `next` con el enlace de otra cuenta en su fragmento dejaría
// a quien acaba de entrar ante el formulario que cambia la contraseña de esa otra cuenta.
const NOT_A_PAGE = /^\/(?:login|recuperar|restablecer|api|_next)(?:[/?#]|$)/i;
// Controles (C0, DEL, C1), espacios raros y la barra invertida, que el navegador lee como `/`.
const UNSAFE = /[\u0000-\u001f\u007f-\u009f\u00a0\u2028\u2029\\]/;
// Next sirve también la ruta con letras codificadas (`/%72establecer`): se mira descodificada.
const plain = (path: string) =>
  path.replace(/%[\da-f]{2}/gi, (code) => String.fromCharCode(parseInt(code.slice(1), 16)));

export function safeNext(value: string | string[] | undefined): string {
  if (typeof value !== "string" || !value.startsWith("/") || UNSAFE.test(value)) {
    return DEFAULT_NEXT;
  }
  let url: URL;
  try {
    url = new URL(value, ORIGIN);
  } catch {
    return DEFAULT_NEXT;
  }
  const outside = url.origin !== ORIGIN || url.pathname.startsWith("//");
  if (outside || NOT_A_PAGE.test(plain(url.pathname))) return DEFAULT_NEXT;
  return url.pathname + url.search + url.hash;
}

export function loginPath(next: string): string {
  return `/login?next=${encodeURIComponent(next)}`;
}
