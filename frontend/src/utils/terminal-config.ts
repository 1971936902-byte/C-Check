export function terminalUrl(config: unknown): string | null {
  if (!config || typeof config !== 'object' || !('url' in config) || typeof config.url !== 'string') return null
  try {
    const url = new URL(config.url)
    // This static configuration is deployment-owned. Never forward C-Check credentials.
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) return null
    return url.href
  } catch {
    return null
  }
}
