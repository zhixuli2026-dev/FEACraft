import Papa from "papaparse";

export interface DelimitedResult {
  rows: string[][];
  errors: string[];
}

/** CSV/TSV/分号文件统一走标准解析器，保留引号内分隔符、换行和空格。 */
export function parseDelimited(source: string, delimiter = ""): DelimitedResult {
  const result = Papa.parse<string[]>(source, {
    delimiter, skipEmptyLines: "greedy", dynamicTyping: false,
  });
  return {
    rows: result.data,
    errors: result.errors.map((error) => `行 ${(error.row ?? 0) + 1}: ${error.message}`),
  };
}
