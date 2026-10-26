import type { CodeIndexEdge, CodeIndexFile } from '../types'

function normalizePath(path: string) {
  const parts: string[] = []
  for (const part of path.replace(/\\/g, '/').split('/')) {
    if (part === '..') parts.pop()
    else if (part && part !== '.') parts.push(part)
  }
  return parts.join('/')
}

export function graphTarget(edge: CodeIndexEdge, files: CodeIndexFile[]) {
  if (edge.target_id) return edge.target_id
  if (edge.edge_type !== 'FILE_INCLUDES_FILE') return undefined
  const include = edge.metadata.include
  if (typeof include !== 'string') return undefined
  const source = files.find((file) => file.id === edge.source_id)
  if (!source) return undefined
  const directory = source.file_path.split('/').slice(0, -1).join('/')
  // Only resolve exact project paths; basename guessing can connect unrelated headers.
  for (const path of [normalizePath(`${directory}/${include}`), normalizePath(include)]) {
    const target = files.find((file) => file.file_path === path)
    if (target) return target.id
  }
  return undefined
}
