import { useEffect, useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import { Brush, Eraser, Undo2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'

type Props = { src: string; mask?: string; feather?: number; onClose: () => void; onSave: (mask: string, ratio: number, feather: number) => void }

export function MaskEditor({ src, mask, feather = 12, onClose, onSave }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const layer = useRef(document.createElement('canvas'))
  const previous = useRef<{ x: number; y: number } | null>(null)
  const history = useRef<ImageData[]>([])
  const [ready, setReady] = useState(false)
  const [erase, setErase] = useState(false)
  const [brush, setBrush] = useState(6)
  const [softness, setSoftness] = useState(feather)
  const [selected, setSelected] = useState(false)
  const [undoable, setUndoable] = useState(false)
  const [error, setError] = useState('')

  function render() {
    const view = canvas.current!
    const context = view.getContext('2d')!
    context.clearRect(0, 0, view.width, view.height)
    context.drawImage(layer.current, 0, 0)
    context.globalCompositeOperation = 'source-in'
    context.fillStyle = '#5bd4b0'
    context.fillRect(0, 0, view.width, view.height)
    context.globalCompositeOperation = 'source-over'
  }
  function updateSelection() {
    const { width, height } = layer.current
    const pixels = layer.current.getContext('2d')!.getImageData(0, 0, width, height).data
    setSelected(pixels.some((value, index) => index % 4 === 3 && value > 0))
    setUndoable(history.current.length > 0)
  }
  useEffect(() => {
    let stopped = false
    const load = (url: string) => new Promise<HTMLImageElement>((resolve, reject) => {
      const image = new Image(); image.onload = () => resolve(image); image.onerror = reject; image.src = url
    })
    void (async () => {
      try {
        const image = await load(src)
        const saved = mask ? await load(mask) : null
        if (stopped || !canvas.current) return
        const scale = Math.min(1, 1536 / Math.max(image.naturalWidth, image.naturalHeight))
        const width = Math.max(1, Math.round(image.naturalWidth * scale))
        const height = Math.max(1, Math.round(image.naturalHeight * scale))
        canvas.current.width = layer.current.width = width
        canvas.current.height = layer.current.height = height
        const ctx = layer.current.getContext('2d')!
        if (saved) {
          ctx.drawImage(saved, 0, 0, width, height)
          const pixels = ctx.getImageData(0, 0, width, height)
          for (let i = 0; i < pixels.data.length; i += 4) {
            pixels.data[i + 3] = Math.round(pixels.data[i] * pixels.data[i + 3] / 255)
            pixels.data[i] = pixels.data[i + 1] = pixels.data[i + 2] = 255
          }
          ctx.putImageData(pixels, 0, 0)
        }
        render(); updateSelection(); setReady(true)
      } catch { if (!stopped) setError('Could not load this image or selection.') }
    })()
    return () => { stopped = true }
  }, [src, mask])

  function checkpoint() {
    const layerCanvas = layer.current
    history.current.push(layerCanvas.getContext('2d')!.getImageData(0, 0, layerCanvas.width, layerCanvas.height))
    if (history.current.length > 12) history.current.shift()
    setUndoable(true)
  }
  function paint(event: PointerEvent<HTMLCanvasElement>, start = false) {
    if (!ready || (!start && !previous.current)) return
    const view = event.currentTarget
    if (start) {
      if (!event.isPrimary || event.button !== 0) return
      checkpoint(); view.setPointerCapture(event.pointerId)
    }
    const bounds = view.getBoundingClientRect()
    const point = { x: (event.clientX - bounds.left) * view.width / bounds.width, y: (event.clientY - bounds.top) * view.height / bounds.height }
    const ctx = layer.current.getContext('2d')!
    const radius = Math.min(view.width, view.height) * brush / 200
    ctx.globalCompositeOperation = erase ? 'destination-out' : 'source-over'
    ctx.strokeStyle = ctx.fillStyle = 'white'; ctx.lineWidth = radius * 2; ctx.lineCap = ctx.lineJoin = 'round'
    ctx.beginPath(); ctx.arc(point.x, point.y, radius, 0, Math.PI * 2); ctx.fill()
    if (!start && previous.current) {
      ctx.beginPath(); ctx.moveTo(previous.current.x, previous.current.y); ctx.lineTo(point.x, point.y); ctx.stroke()
    }
    ctx.globalCompositeOperation = 'source-over'
    previous.current = point; render()
  }
  function finish() { previous.current = null; if (ready) updateSelection() }
  function save() {
    const output = document.createElement('canvas')
    output.width = layer.current.width; output.height = layer.current.height
    const ctx = output.getContext('2d')!
    ctx.fillStyle = 'black'; ctx.fillRect(0, 0, output.width, output.height); ctx.drawImage(layer.current, 0, 0)
    onSave(output.toDataURL('image/png'), output.width / output.height, softness)
  }

  return <Dialog open onOpenChange={open => { if (!open) onClose() }}><DialogContent className="mask-dialog">
    <DialogTitle>Edit area</DialogTitle>
    <DialogDescription>Paint the area to change.</DialogDescription>
    <div className="mask-toolbar">
      <Button variant={erase ? 'outline' : 'secondary'} size="icon" aria-label="Brush" aria-pressed={!erase} onClick={() => setErase(false)}><Brush /></Button>
      <Button variant={erase ? 'secondary' : 'outline'} size="icon" aria-label="Eraser" aria-pressed={erase} onClick={() => setErase(true)}><Eraser /></Button>
      <label>Brush size<input aria-label="Brush size" type="range" min="1" max="25" value={brush} onChange={e => setBrush(Number(e.target.value))} /></label>
      <Button variant="ghost" size="icon" aria-label="Undo selection" disabled={!undoable} onClick={() => { layer.current.getContext('2d')!.putImageData(history.current.pop()!, 0, 0); render(); updateSelection() }}><Undo2 /></Button>
      <Button variant="ghost" disabled={!selected} onClick={() => { checkpoint(); layer.current.getContext('2d')!.clearRect(0, 0, layer.current.width, layer.current.height); render(); updateSelection() }}>Clear</Button>
    </div>
    <div className="mask-stage"><img src={src} alt="Image to edit" draggable={false} /><canvas ref={canvas} aria-label="Paint edit area" onPointerDown={e => paint(e, true)} onPointerMove={e => paint(e)} onPointerUp={finish} onPointerCancel={finish} onLostPointerCapture={finish} /></div>
    {error && <p role="alert">{error}</p>}
    <div className="mask-footer"><label>Edge softness<input aria-label="Edge softness" type="range" min="0" max="40" value={softness} onChange={e => setSoftness(Number(e.target.value))} /></label><Button variant="ghost" onClick={onClose}>Cancel</Button><Button disabled={!ready || !selected} onClick={save}>Use selection</Button></div>
  </DialogContent></Dialog>
}
