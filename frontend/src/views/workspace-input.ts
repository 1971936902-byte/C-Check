export type InputMode = 'text' | 'file' | 'archive' | 'folder'

export function activeUpload(mode: InputMode, singleFile?: File, archiveFile?: File) {
  if (mode === 'file') return singleFile
  if (mode === 'archive') return archiveFile
  return undefined
}

export function hasReviewInput(
  mode: InputMode,
  sourceText: string,
  singleFile?: File,
  archiveFile?: File,
  folderFiles: File[] = [],
) {
  if (mode === 'text') return Boolean(sourceText.trim())
  if (mode === 'folder') return folderFiles.length > 0
  return Boolean(activeUpload(mode, singleFile, archiveFile))
}

export function canSubmitReview(input: {
  mode: InputMode
  selectedModel: string
  sourceText: string
  singleFile?: File
  archiveFile?: File
  folderFiles?: File[]
  checkTypes: string[]
}) {
  return Boolean(
    input.selectedModel
    && input.checkTypes.length
    && hasReviewInput(input.mode, input.sourceText, input.singleFile, input.archiveFile, input.folderFiles || []),
  )
}
