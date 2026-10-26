import { describe, expect, it } from 'vitest'
import { escapeHtml, highlightCLine, renderHighlightedCSource } from './c-highlight'

describe('C source highlighting', () => {
  it('escapes source text before injecting highlight markup', () => {
    expect(escapeHtml('<script>alert("x")</script>')).toBe('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;')
  })

  it('highlights C keywords, strings, numbers, comments, and preprocessor lines', () => {
    expect(highlightCLine('#include <stdio.h>')).toContain('code-token-preprocessor')
    expect(highlightCLine('int value = 42; // ok')).toContain('code-token-keyword')
    expect(highlightCLine('int value = 42; // ok')).toContain('code-token-number')
    expect(highlightCLine('printf("ok"); // done')).toContain('code-token-string')
    expect(highlightCLine('printf("ok"); // done')).toContain('code-token-comment')
  })

  it('renders an empty-state placeholder when no preview source is available', () => {
    expect(renderHighlightedCSource('')).toContain('暂无可预览内容')
  })

  it('escapes unmatched markup and operators in the actual rendered preview', () => {
    const html = renderHighlightedCSource('/* <img src=x onerror=alert()> */\nif (a < b && b > c) {}')
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img')
    expect(html).toContain('&amp;&amp;')
  })

  it('does not treat URL slashes inside a string as a comment', () => {
    const html = highlightCLine('const char *url = "https://example.test"; // comment')
    expect(html).toContain('<span class="code-token-string">&quot;https://example.test&quot;</span>')
    expect(html).toContain('<span class="code-token-comment">// comment</span>')
  })
})
