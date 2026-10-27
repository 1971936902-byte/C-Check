import { describe, expect, it } from 'vitest'
import { terminalUrl } from './terminal-config'

describe('independent terminal URL', () => {
  it('accepts an HTTPS gateway without credentials or tokens', () => {
    expect(terminalUrl({ url: 'https://terminal.example.com/guacamole/' })).toBe('https://terminal.example.com/guacamole/')
    expect(terminalUrl({ url: 'https://localhost:18843/guacamole/' })).toBe('https://localhost:18843/guacamole/')
  })
  it.each([null, {}, { url: '' }, { url: 42 }, { url: '/guacamole/' },
    { url: 'javascript:alert(1)' }, { url: 'http://terminal.example.com' },
    { url: 'https://user:password@example.com' }, { url: 'https://example.com/?token=secret' },
    { url: 'https://example.com/#token=secret' }])('rejects invalid or credential-bearing config %j', value => {
    expect(terminalUrl(value)).toBeNull()
  })
})
