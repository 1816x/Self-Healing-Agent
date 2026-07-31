/**
 * Renders a unified diff.
 *
 * Hand-written rather than pulled from a package: the format is line-oriented
 * and classifying a line is a switch on its first character, so a dependency
 * here would be larger than the thing it replaces and harder to explain. The
 * one subtlety is ordering — `---`/`+++` are file headers and must be checked
 * before `-`/`+`, or every diff opens with a deleted and an added line.
 */
type LineClass = "file" | "hunk" | "add" | "del" | "ctx";

export function classifyLine(line: string): LineClass {
  if (line.startsWith("--- ") || line.startsWith("+++ ")) return "file";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "ctx";
}

export function DiffView({ diff }: { diff: string }) {
  const lines = diff.replace(/\n$/, "").split("\n");
  return (
    <pre className="diff">
      {lines.map((line, index) => (
        <span key={index} className={`diff-line ${classifyLine(line)}`}>
          {line === "" ? " " : line}
        </span>
      ))}
    </pre>
  );
}
