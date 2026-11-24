import { useSyncExternalStore } from "react";

// `useSearchParams` de Next.js en jsdom: lee `location` y cambia con `history` y `popstate`.
for (const name of ["pushState", "replaceState"] as const) {
  const original = history[name].bind(history);
  history[name] = (...args) => (original(...args), dispatchEvent(new Event("test:url")));
}
const listen = (notify: () => void) => {
  ["popstate", "test:url"].forEach((type) => addEventListener(type, notify));
  return () => ["popstate", "test:url"].forEach((type) => removeEventListener(type, notify));
};
export const useSearchParams = () =>
  new URLSearchParams(useSyncExternalStore(listen, () => location.search));
