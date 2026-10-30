// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { renderAssistantMarkdown } from './assistant-markdown'

describe('assistant Markdown', () => {
  it('renders headings, lists, tables, quotes and inline code', () => {
    const html = renderAssistantMarkdown('# Result\n\n**Safe** and `NULL`\n\n- First\n- Second\n\n> Note\n\n| File | Status |\n| --- | --- |\n| a.c | OK |')
    for (const tag of ['h1', 'strong', 'code', 'ul', 'blockquote', 'table', 'th', 'td']) {
      expect(html).toContain(`<${tag}`)
    }
  })
  it('escapes fenced C code and preserves its whitespace', () => {
    const html = renderAssistantMarkdown('```c\nif (a < b) {\n  return "<script>";\n}\n```')
    expect(html).toContain('class="language-c"')
    expect(html).toContain('a &lt; b')
    const fragment = document.createElement('div')
    fragment.innerHTML = html
    expect(fragment.querySelector('pre code')?.textContent).toContain('  return')
    expect(html).not.toContain('<script>')
  })
  it('renders unfinished streaming Markdown without throwing', () => {
    expect(renderAssistantMarkdown('```c\nint main(')).toContain('<pre>')
    expect(renderAssistantMarkdown('**part')).toContain('part')
    expect(renderAssistantMarkdown('')).toBe('')
  })
  it('does not execute or interpret raw model HTML', () => {
    const html = renderAssistantMarkdown('<img src=x onerror=alert(1)>\n<script>alert(2)</script>\n<svg onload=alert(3)>')
    expect(html).not.toMatch(/<(img|script|svg)\b/i)
    expect(html).toContain('&lt;')
  })
  it('rejects executable links and avoids tracking image requests', () => {
    const html = renderAssistantMarkdown('[bad](javascript:alert%281%29)\n![pixel](https://example.test/pixel)')
    expect(html).not.toContain('href="javascript:')
    expect(html).not.toContain('<img')
  })
  it('opens ordinary links without exposing the application opener', () => {
    const html = renderAssistantMarkdown('[docs](https://example.test/docs)')
    expect(html).toContain('href="https://example.test/docs"')
    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer"')
  })
  it('highlights known languages and provides a safe copy slot per fenced block', () => {
    const html = renderAssistantMarkdown('```c\nreturn 42;\n```\n\n```unknown\n<script>x</script>\n```')
    expect(html).toContain('hljs-keyword')
    expect(html).toContain('data-code-index="0"')
    expect(html).toContain('data-code-index="1"')
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
  })
  it('does not interpret fence metadata as markup or event handlers', () => {
    const html = renderAssistantMarkdown('```c\"onclick=alert(1)\nreturn 0;\n```')
    expect(html).not.toContain('onclick=')
    expect(html).toContain('language-text')
  })
})
