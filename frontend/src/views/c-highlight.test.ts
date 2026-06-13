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
})
