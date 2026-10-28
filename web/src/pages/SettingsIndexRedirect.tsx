import { Navigate } from "react-router-dom";
import { useLocation } from "@/lib/routing";

/** Query markers a connection callback adds when it returns to bare ``/settings``. */
const CONNECTION_OUTCOMES = ["github", "google_cloud", "google_drive", "databricks"];

/**
 * Redirect bare ``/settings`` to its canonical section. A connection callback's
 * outcome goes to Integrations with its query intact, so the control that
 * started the flow can show it.
 */
export function SettingsIndexRedirect({ prefix }: { prefix: string }) {
  const { search } = useLocation();
  const params = new URLSearchParams(search);
  const outcome = CONNECTION_OUTCOMES.some((name) => params.has(name));
  return (
    <Navigate
      to={`${prefix}/settings/${outcome ? "integrations" : "general"}${outcome ? search : ""}`}
      replace
    />
  );
}
