"use strict";

const syntaxPatterns = {
  json: /(?<key>"(?:\\[\s\S]|[^"\\])*"(?=\s*:))|(?<string>"(?:\\[\s\S]|[^"\\])*")|(?<literal>\b(?:true|false|null)\b)|(?<number>-?\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\b)|(?<punctuation>[{}[\],:])/gi,
  sql: /(?<comment>--[^\r\n]*|\/\*[\s\S]*?(?:\*\/|$))|(?<string>'(?:''|[^'])*')|(?<key>"(?:""|[^"])*"|\[[^\]]*\]|\x60(?:\x60\x60|[^\x60])*\x60)|(?<keyword>\b(?:SELECT|WITH|RECURSIVE|AS|FROM|WHERE|JOIN|LEFT|RIGHT|FULL|INNER|OUTER|CROSS|ON|USING|GROUP|BY|HAVING|ORDER|LIMIT|OFFSET|ASC|DESC|NULLS|FIRST|LAST|DISTINCT|ALL|UNION|INTERSECT|EXCEPT|CASE|WHEN|THEN|ELSE|END|AND|OR|NOT|IS|IN|EXISTS|BETWEEN|LIKE|GLOB|ESCAPE|COLLATE|OVER|PARTITION|WINDOW|FILTER|ROWS|RANGE|GROUPS|CURRENT|ROW|UNBOUNDED|PRECEDING|FOLLOWING|VALUES|CAST)\b)|(?<literal>\b(?:NULL|TRUE|FALSE)\b)|(?<call>\b[a-z_]\w*(?=\s*\())|(?<number>\b(?:0x[0-9a-f]+|\d+(?:\.\d*)?(?:e[+-]?\d+)?)\b|(?<!\w)\.\d+\b)|(?<punctuation>[(),.;]|[+*/%<>=~-])/gi,
  powershell: /(?<comment>#[^\r\n]*)|(?<string>"(?:\x60[\s\S]|[^"\x60])*"|'(?:''|[^'])*')|(?<key>\$(?:\{[^}]+\}|[\w:]+)|(?<![\w-])--[\w-]+)|(?<keyword>(?<![\w./-])(?:uv|run|report|replay|python)(?![\w./-]))|(?<number>\b\d+\b)/g,
  text: /(?<key>\{\{[^{}\r\n]+\}\})/g,
};

function highlightCode(container, source, language) {
  const pattern = syntaxPatterns[language];
  let end = 0;
  for (const token of source.matchAll(pattern)) {
    const type = Object.keys(token.groups).find((name) => token.groups[name] !== undefined);
    container.append(source.slice(end, token.index));
    const span = document.createElement("span");
    span.className = "syntax-" + type;
    span.textContent = token[0];
    container.append(span);
    end = token.index + token[0].length;
  }
  container.append(source.slice(end));
}
