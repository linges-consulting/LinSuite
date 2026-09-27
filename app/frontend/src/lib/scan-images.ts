/** Downsample on this device so camera photos fit the JSON upload limit. */
export async function downsampleScan(file: File): Promise<string> {
  const url = URL.createObjectURL(file)
  try {
    const image = new Image()
    image.src = url
    await image.decode()
    if (!image.naturalWidth || !image.naturalHeight) throw new Error('That image could not be opened.')
    const canvas = document.createElement('canvas')
    const scale = Math.min(1, 1650 / Math.max(image.naturalWidth, image.naturalHeight))
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale))
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale))
    let quality = 0.7
    for (;;) {
      const ctx = canvas.getContext('2d')
      if (!ctx) throw new Error('This browser cannot prepare scanned pages.')
      ctx.filter = 'grayscale(1)'
      ctx.drawImage(image, 0, 0, canvas.width, canvas.height)
      const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob(
        (value) => value ? resolve(value) : reject(new Error('That image could not be prepared.')),
        'image/jpeg', quality,
      ))
      // Eight pages at this cap remain under the server's total base64 limit.
      if (blob.size <= 170_000) {
        return new Promise<string>((resolve, reject) => {
          const reader = new FileReader()
          reader.onload = () => resolve(String(reader.result))
          reader.onerror = () => reject(new Error('That image could not be read.'))
          reader.readAsDataURL(blob)
        })
      }
      if (quality > 0.3) quality -= 0.15
      else if (Math.max(canvas.width, canvas.height) > 600) {
        canvas.width = Math.max(1, Math.round(canvas.width * 0.8))
        canvas.height = Math.max(1, Math.round(canvas.height * 0.8))
      } else throw new Error('That page is too large. Try a clearer, smaller photo.')
    }
  } finally { URL.revokeObjectURL(url) }
}
