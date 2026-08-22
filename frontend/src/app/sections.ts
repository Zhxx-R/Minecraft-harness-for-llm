/** Top-level operational workspaces available from the dashboard portal. */
export type MainSection =
  | "home"
  | "quick-start"
  | "runtime"
  | "skills"
  | "knowledge"
  | "configuration"
  | "creative"
  | "reports";

/** Resolve a URL hash to a stable workspace, falling back to the portal home. */
export function sectionFromHash(hash: string): MainSection {
  const value = hash.replace(/^#\/?/, "");
  if (
    value === "quick-start" ||
    value === "runtime" ||
    value === "skills" ||
    value === "knowledge" ||
    value === "configuration" ||
    value === "creative" ||
    value === "reports"
  ) {
    return value;
  }
  return "home";
}
