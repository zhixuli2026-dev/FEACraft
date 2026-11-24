export interface CitationItem {
  title: string;
  authors: string[];
  year: string;
  id: string;
}

export type CitationStyle = "apa" | "ieee" | "chicago";

function parts(name: string) {
  const trimmed = name.trim();
  if (trimmed.includes(",")) {
    const [family, ...given] = trimmed.split(",");
    return { family: family.trim(), given: given.join(",").trim() };
  }
  const words = trimmed.split(/\s+/).filter(Boolean);
  return { family: words.pop() ?? "", given: words.join(" ") };
}

function initials(given: string) {
  return given.split(/[\s-]+/).filter(Boolean).map((part) => `${part[0].toUpperCase()}.`).join(" ");
}

function resourceLink(id: string) {
  if (id.startsWith("doi:")) return `https://doi.org/${id.slice(4)}`;
  if (id.startsWith("arXiv:")) return `https://arxiv.org/abs/${id.slice(6)}`;
  if (/^semanticscholar:[a-f0-9]{40}$/.test(id)) return `https://www.semanticscholar.org/paper/${id.slice(16)}`;
  return "";
}

function authorDateAuthors(authors: string[], chicago = false) {
  if (!authors.length) return "";
  if (authors.length > 2) {
    const first = parts(authors[0]);
    return chicago ? `${first.family}, ${first.given}, et al.` : `${first.family}, ${initials(first.given)} et al.`;
  }
  const formatted = authors.map((name, index) => {
    const { family, given } = parts(name);
    if (chicago) return index === 0 ? `${family}, ${given}` : `${given} ${family}`.trim();
    return `${family}, ${initials(given)}`;
  });
  return formatted.join(chicago ? ", and " : " & ");
}

/** 常见引用格式预览；不是任意 CSL 样式解释器。 */
export function formatCitation(item: CitationItem, style: CitationStyle, index = 1): string {
  const link = resourceLink(item.id);
  if (style === "ieee") {
    const authors = item.authors.length > 2
      ? `${initials(parts(item.authors[0]).given)} ${parts(item.authors[0]).family} et al.`
      : item.authors.map((name) => {
          const { family, given } = parts(name);
          return `${initials(given)} ${family}`.trim();
        }).join(" and ");
    const doi = item.id.startsWith("doi:") ? ` doi: ${item.id.slice(4)}.` : link ? ` ${link}.` : "";
    return `[${index}] ${authors}${authors ? ", " : ""}"${item.title},"${item.year ? ` ${item.year}.` : ""}${doi}`;
  }
  const chicago = style === "chicago";
  const authors = authorDateAuthors(item.authors, chicago);
  const year = item.year || (chicago ? "" : "n.d.");
  if (chicago) {
    return `${authors}${authors ? "." : ""}${year ? ` ${year}.` : ""} "${item.title}."${link ? ` ${link}` : ""}`;
  }
  const authorWithPeriod = authors && !authors.endsWith(".") ? `${authors}.` : authors;
  return `${authorWithPeriod ? `${authorWithPeriod} ` : ""}(${year}). ${item.title}.${link ? ` ${link}` : ""}`;
}
