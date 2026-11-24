type Shape = "rectangle" | "circle";
export type DiagramNode = { id: string; x: number; y: number; label: string; shape: Shape; line: number };
type DiagramLink = { from: string; to: string; arrow: boolean; line: number };

export const newTikzDocument = String.raw`\documentclass{article}
\usepackage{tikz}
\pagestyle{empty}
\begin{document}
\begin{tikzpicture}
  \node[draw,rectangle] (input) at (-2,0) {Input};
  \node[draw,circle] (output) at (2,0) {Output};
  \draw[->] (input) -- (output);
\end{tikzpicture}
\end{document}
`;

const number = "(-?(?:\\d+(?:\\.\\d*)?|\\.\\d+))";
const identifier = "([A-Za-z][\\w-]*)";
const nodePattern = new RegExp(`^\\s*\\\\node\\[draw,\\s*(rectangle|circle)\\]\\s*\\(${identifier}\\)\\s+at\\s*\\(${number},\\s*${number}\\)\\s*\\{(.*)\\};\\s*$`);
const linkPattern = new RegExp(`^\\s*\\\\draw(?:\\[(->)\\])?\\s*\\(${identifier}\\)\\s*--\\s*\\(${identifier}\\);\\s*$`);

export function parseDiagram(source: string) {
  const newline = source.includes("\r\n") ? "\r\n" : "\n";
  const lines = source.split(newline);
  const begin = lines.findIndex(line => /^\s*\\begin\{tikzpicture\}/.test(line));
  const end = lines.findIndex((line, index) => index > begin && /^\s*\\end\{tikzpicture\}/.test(line));
  const nodes: DiagramNode[] = [];
  const links: DiagramLink[] = [];
  let otherLines = 0;
  if (begin >= 0 && end > begin) {
    for (let line = begin + 1; line < end; line++) {
      const node = nodePattern.exec(lines[line]);
      const edge = linkPattern.exec(lines[line]);
      if (node && !nodes.some(value => value.id === node[2])
          && [node[3], node[4]].every(value => Number.isFinite(Number(value)))) {
        nodes.push({ shape: node[1] as Shape, id: node[2], x: Number(node[3]), y: Number(node[4]), label: node[5], line });
      } else if (edge) {
        links.push({ arrow: Boolean(edge[1]), from: edge[2], to: edge[3], line });
      } else if (lines[line].trim() && !lines[line].trimStart().startsWith("%")) {
        otherLines++;
      }
    }
  }
  return { lines, newline, begin, end, nodes, links, otherLines };
}

export type ParsedDiagram = ReturnType<typeof parseDiagram>;

export function nodeCommand(node: Pick<DiagramNode, "id" | "x" | "y" | "label" | "shape">) {
  return `  \\node[draw,${node.shape}] (${node.id}) at (${node.x},${node.y}) {${node.label}};`;
}

export function replaceLines(diagram: ParsedDiagram, from: number, count: number, ...lines: string[]) {
  const copy = [...diagram.lines];
  copy.splice(from, count, ...lines);
  return copy.join(diagram.newline);
}
