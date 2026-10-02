import { Fragment, type ReactNode } from "react";

/**
 * A small, safe Markdown renderer for chat answers: code blocks, headings, lists, bold
 * and inline code. It builds React elements (never raw HTML), so model output cannot
 * inject markup or scripts.
 */
export function Markdown({ text }: { text: string }) {
  return <div className="space-y-3 leading-relaxed">{blocks(text)}</div>;
}

function inline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("`")) {
      out.push(
        <code key={`${keyBase}-${i++}`} className="rounded bg-zinc-200/70 px-1 py-0.5 text-[0.85em] dark:bg-zinc-800">
          {tok.slice(1, -1)}
        </code>,
      );
    } else {
      out.push(<strong key={`${keyBase}-${i++}`}>{tok.slice(2, -2)}</strong>);
    }
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function blocks(text: string): ReactNode[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    const line = lines[i] ?? "";
    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i] ?? "")) code.push(lines[i++] ?? "");
      i++; // closing fence (or end of a still-streaming block)
      out.push(
        <pre key={k++} className="overflow-x-auto rounded-lg bg-zinc-900 p-3 text-xs text-zinc-100 dark:bg-black">
          {fence[1] ? <div className="mb-2 text-[10px] uppercase tracking-wide text-zinc-400">{fence[1]}</div> : null}
          <code>{code.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      out.push(<hr key={k++} className="border-zinc-200 dark:border-zinc-800" />);
      i++;
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line)) {
      const rows: string[][] = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i] ?? "")) {
        const cells = (lines[i] ?? "").trim().slice(1, -1).split("|").map((c) => c.trim());
        // Skip the |---|:---:| separator row.
        if (!cells.every((c) => /^:?-{2,}:?$/.test(c))) rows.push(cells);
        i++;
      }
      const [head, ...body] = rows;
      out.push(
        <div key={k++} className="overflow-x-auto">
          <table className="w-full border-collapse text-left text-sm">
            {head ? (
              <thead>
                <tr>
                  {head.map((c, j) => (
                    <th key={j} className="border-b border-zinc-300 px-2 py-1.5 font-semibold dark:border-zinc-700">
                      {inline(c, `th${k}-${j}`)}
                    </th>
                  ))}
                </tr>
              </thead>
            ) : null}
            <tbody>
              {body.map((r, ri) => (
                <tr key={ri}>
                  {r.map((c, j) => (
                    <td key={j} className="border-b border-zinc-200 px-2 py-1.5 align-top dark:border-zinc-800">
                      {inline(c, `td${k}-${ri}-${j}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    if (heading) {
      const size = heading[1]!.length === 1 ? "text-lg" : "text-base";
      out.push(
        <p key={k++} className={`${size} font-semibold`}>
          {inline(heading[2] ?? "", `h${k}`)}
        </p>,
      );
      i++;
      continue;
    }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i] ?? "")) {
        items.push((lines[i] ?? "").replace(/^\s*([-*•]|\d+[.)])\s+/, ""));
        i++;
      }
      const ListTag = ordered ? "ol" : "ul";
      out.push(
        <ListTag key={k++} className={`${ordered ? "list-decimal" : "list-disc"} space-y-1 pl-5`}>
          {items.map((item, j) => (
            <li key={j}>{inline(item, `l${k}-${j}`)}</li>
          ))}
        </ListTag>,
      );
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    const para: string[] = [];
    while (
      i < lines.length &&
      (lines[i] ?? "").trim() &&
      !/^```/.test(lines[i] ?? "") &&
      !/^(#{1,3})\s/.test(lines[i] ?? "") &&
      !/^\s*\|.*\|\s*$/.test(lines[i] ?? "") &&
      !/^\s*([-*•]|\d+[.)])\s+/.test(lines[i] ?? "")
    ) {
      para.push(lines[i++] ?? "");
    }
    out.push(
      <p key={k++} className="whitespace-pre-wrap">
        {para.map((p, j) => (
          <Fragment key={j}>
            {j > 0 ? "\n" : null}
            {inline(p, `p${k}-${j}`)}
          </Fragment>
        ))}
      </p>,
    );
  }
  return out;
}
