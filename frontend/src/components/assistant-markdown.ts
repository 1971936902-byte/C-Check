import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/core'
import c from 'highlight.js/lib/languages/c'
import cpp from 'highlight.js/lib/languages/cpp'
import python from 'highlight.js/lib/languages/python'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import bash from 'highlight.js/lib/languages/bash'
import json from 'highlight.js/lib/languages/json'

for (const [name, grammar] of Object.entries({ c, cpp, python, javascript, typescript, bash, json })) hljs.registerLanguage(name, grammar)

const markdown = new MarkdownIt({ html: false, breaks: true, linkify: true })
// Model output must not trigger automatic external image requests.
markdown.disable('image')
markdown.renderer.rules.fence = (tokens, index, _options, env) => {
  const token = tokens[index]!, raw = token.info.trim().split(/\s+/)[0] || 'text'
  const language = /^[\w+#.-]{1,40}$/.test(raw) ? raw : 'text'
  const escaped = markdown.utils.escapeHtml(token.content)
  const highlighted = token.content.length <= 100000 && hljs.getLanguage(language)
    ? hljs.highlight(token.content, { language, ignoreIllegals: true }).value : escaped
  const block = typeof env?.codeIndex === 'number' ? env.codeIndex : 0
  if (env) env.codeIndex = block + 1
  return `<div class="code-block"><div class="code-toolbar"><span>${markdown.utils.escapeHtml(language)}</span><span class="code-copy-slot" data-code-index="${block}"></span></div><pre><code class="language-${markdown.utils.escapeHtml(language)}">${highlighted}</code></pre></div>\n`
}
markdown.renderer.rules.link_open = (tokens, index, options, _env, renderer) => {
  tokens[index]!.attrSet('target', '_blank')
  tokens[index]!.attrSet('rel', 'noopener noreferrer')
  return renderer.renderToken(tokens, index, options)
}

export function renderAssistantMarkdown(content: string): string {
  return DOMPurify.sanitize(markdown.render(content, { codeIndex: 0 }), {
    ALLOWED_TAGS: ['p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 's',
      'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'a', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'span', 'div'],
    ALLOWED_ATTR: ['href', 'title', 'target', 'rel', 'class', 'start', 'data-code-index'],
    ALLOW_DATA_ATTR: false,
  })
}
