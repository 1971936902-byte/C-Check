export function createLatestRequest() {
  let generation = 0
  let disposed = false
  return {
    begin() {
      const current = ++generation
      return () => !disposed && current === generation
    },
    dispose() { disposed = true; generation += 1 },
  }
}
