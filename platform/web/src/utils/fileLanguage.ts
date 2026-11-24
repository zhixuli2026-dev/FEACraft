import { FOAM_LANG } from "./openfoamLang.ts";

const LANG: [string, string][] = [
  [".py", "python"], [".md", "markdown"], [".json", "json"], [".sh", "shell"],
  [".yaml", "yaml"], [".yml", "yaml"], [".cpp", "cpp"], [".c", "c"], [".h", "cpp"],
];

const FOAM_NAMES = new Set([
  "casedict", "controldict", "fvschemes", "fvsolution", "blockmeshdict",
  "decomposepardict", "transportproperties", "turbulenceproperties",
  "thermophysicalproperties", "dynamicmeshdict", "snappyhexmeshdict",
  "setfieldsdict", "toposetdict", "createpatchdict", "extrudemeshdict",
  "surfacefeatureextractdict", "mapfieldsdict",
]);

const PLAIN_EXTENSIONS = new Set([".txt", ".csv", ".log", ".xml", ".html", ".htm", ".css", ".js"]);

export function langOf(path: string, detected?: string): string {
  if (detected === FOAM_LANG) return FOAM_LANG;
  const parts = path.replace(/\\/g, "/").split("/");
  const base = (parts.at(-1) ?? "").toLowerCase();
  if (FOAM_NAMES.has(base)) return FOAM_LANG;

  const dot = base.lastIndexOf(".");
  const ext = dot > 0 ? base.slice(dot) : "";
  if (ext === ".foam") return FOAM_LANG;
  const language = LANG.find(([suffix]) => suffix === ext)?.[1];
  if (language || PLAIN_EXTENSIONS.has(ext)) return language ?? "plaintext";

  const parent = parts.at(-2) ?? "";
  if (/^0(?:\.\d+|\.orig)?$/i.test(parent)) return FOAM_LANG;
  return "plaintext";
}
