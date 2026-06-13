import { describe, expect, it } from 'vitest'
import { displayNameFromFolder, formatBytes, isCSourceFile, modelDescriptionText } from './workspace-files'

const file = (name: string, webkitRelativePath = '') => {
  const source = new File(['int main(void) {}'], name)
  Object.defineProperty(source, 'webkitRelativePath', { value: webkitRelativePath })
  return source
}

describe('workspace file helpers', () => {
  it('recognizes C source files using either name or relative path', () => {
    expect(isCSourceFile(file('main.c'))).toBe(true)
    expect(isCSourceFile(file('main.txt', 'project/include/main.h'))).toBe(true)
    expect(isCSourceFile(file('README.md'))).toBe(false)
  })

  it('formats file sizes for upload summaries', () => {
    expect(formatBytes(18)).toBe('18 B')
    expect(formatBytes(2048)).toBe('2.0 KB')
    expect(formatBytes(2 * 1024 * 1024)).toBe('2.0 MB')
  })

  it('derives a stable folder display name from relative paths', () => {
    expect(displayNameFromFolder([file('main.c', 'demo/src/main.c')])).toBe('demo')
    expect(displayNameFromFolder([file('main.c')])).toBe('项目文件夹审查')
  })

  it('localizes known model registration descriptions', () => {
    expect(modelDescriptionText('Registered by manual deploy verification')).toContain('手动部署验证')
    expect(modelDescriptionText('custom note')).toBe('custom note')
  })
})
