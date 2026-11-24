export type AppTheme = "dark" | "light";

export function initialAppTheme(): AppTheme {
  return localStorage.getItem("vf_theme") === "light" ? "light" : "dark";
}

export function applyAppTheme(theme: AppTheme) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.vfTheme = theme;
  if (document.body) {
    document.body.dataset.theme = theme;
    document.body.dataset.vfTheme = theme;
  }
  window.dispatchEvent(new CustomEvent("vibeflow:theme", { detail: theme }));
}
