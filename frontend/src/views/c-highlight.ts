const C_KEYWORDS = new Set([
  'auto', 'break', 'case', 'char', 'const', 'continue', 'default', 'do', 'double', 'else',
  'enum', 'extern', 'float', 'for', 'goto', 'if', 'inline', 'int', 'long', 'register',
  'restrict', 'return', 'short', 'signed', 'sizeof', 'static', 'struct', 'switch', 'typedef',
  'union', 'unsigned', 'void', 'volatile', 'while', 'NULL',
])

export function escapeHtml(value: string) {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

export function highlightCLine(line: string) {
  if (line.trimStart().startsWith('#')) {
    return `<span class="code-token-preprocessor">${escapeHtml(line)}</span>`
  }

  return line.replace(
    /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(\/\/.*$)|\b([A-Za-z_][A-Za-z0-9_]*)\b|\b(\d+(?:\.\d+)?)\b|[\s\S]/g,
    (match, stringToken, commentToken, wordToken, numberToken) => {
      if (stringToken) return `<span class="code-token-string">${escapeHtml(stringToken)}</span>`
      if (commentToken) return `<span class="code-token-comment">${escapeHtml(commentToken)}</span>`
      if (numberToken) return `<span class="code-token-number">${escapeHtml(numberToken)}</span>`
      if (C_KEYWORDS.has(wordToken)) return `<span class="code-token-keyword">${escapeHtml(wordToken)}</span>`
      return escapeHtml(match)
    },
  )
}

export function renderHighlightedCSource(sourceText: string) {
  if (!sourceText) return '<div class="code-preview-empty">暂无可预览内容</div>'
  return sourceText
    .split('\n')
    .map((line, index) => (
      `<div class="code-preview-line"><span class="code-preview-line-number">${index + 1}</span><code>${highlightCLine(line)}</code></div>`
    ))
    .join('')
}
