import { useSyncExternalStore } from "react";

// `useSearchParams` en jsdom: `location`, avisado por `history` y `popstate`.
for (const name of ["pushState", "replaceState"] as const) {
  const original = history[name].bind(history);
  history[name] = (...args) => (original(...args), dispatchEvent(new PopStateEvent("popstate")));
}
const listen = (notify: () => void) => {
  addEventListener("popstate", notify);
  return () => removeEventListener("popstate", notify);
};
export const useSearchParams = () =>
  new URLSearchParams(useSyncExternalStore(listen, () => location.search));
