export function saveCredentialDownload(credential: Blob, filename: string): void {
  const url = URL.createObjectURL(credential)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.hidden = true
  document.body.appendChild(link)
  try { link.click() } finally { link.remove() }
  const revokeObjectUrl = URL.revokeObjectURL.bind(URL)
  window.setTimeout(() => revokeObjectUrl(url), 1000)
}
