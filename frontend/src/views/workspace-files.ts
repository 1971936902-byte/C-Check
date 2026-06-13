export function isCSourceFile(file: File) {
  const name = (file.webkitRelativePath || file.name).toLowerCase()
  return name.endsWith('.c') || name.endsWith('.h')
}

export function formatBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

export function modelDescriptionText(value: string | null | undefined) {
  if (!value) return ''
  if (value === 'Registered by manual deploy verification') return '由手动部署验证登记，可直接用于代码审查。'
  if (value === 'Registered by deploy/native/c-check-deploy.sh') return '由部署脚本自动登记，可直接用于代码审查。'
  return value
}

export function displayNameFromFolder(files: File[]) {
  const firstPath = files[0]?.webkitRelativePath || files[0]?.name || ''
  return firstPath.includes('/') ? firstPath.split('/')[0] : '项目文件夹审查'
}
