export type InputMode = 'text' | 'file' | 'archive'

export function activeUpload(mode: InputMode, singleFile?: File, archiveFile?: File) {
  if (mode === 'file') return singleFile
  if (mode === 'archive') return archiveFile
  return undefined
}
