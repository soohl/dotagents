import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowDown, ArrowUp, ArrowUpRight, Check, Download, ImagePlus, Images, LoaderCircle, Maximize2, Menu, Plus, Trash2, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { MaskEditor } from './MaskEditor'

type AreaEdit = { base: number; mask: string; feather: number }
type Entry = { image: string; prompt: string; model: string; size: string; steps: number; seed: number; status: string; references: string[]; edit?: AreaEdit | null }
type Session = { id: string; entries: Entry[]; status: string; error?: string }
type Summary = { id: string; title: string; status: string }
type Options = { models: { id: string; label: string; sizes: string[] }[]; steps: number; max_steps: number; max_references: number }
type Health = { busy: boolean; queued: number; loaded?: boolean; note?: string | null; engine?: string; active_model?: string; progress?: { completed: number; total: number } | null }
type Reference = { src: string; name: string; data?: string }
type Settings = Pick<Entry, 'prompt' | 'model' | 'size' | 'steps' | 'seed'>
const defaults: Settings = { prompt: '', model: '', size: '1024x1024', steps: 25, seed: -1 }
const active = (status?: string) => status === 'queued' || status === 'generating'
const prefix = window.location.pathname.startsWith('/images/') ? '/images' : ''
async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(prefix + path, { cache: 'no-store', ...init })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : 'Something went wrong. Please try again.')
  }
  return response.status === 204 ? undefined as T : response.json()
}
async function getSession(id: string, signal?: AbortSignal): Promise<Session> {
  const session = await api<Session>(`/api/sessions/${id}`, { signal })
  session.entries = session.entries.map(entry => ({ ...entry,
    image: prefix + entry.image,
    references: entry.references.map(path => prefix + path),
    edit: entry.edit ? { ...entry.edit, mask: prefix + entry.edit.mask } : entry.edit,
  }))
  return session
}
function readFile(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(new Error('Could not read this image.')); reader.readAsDataURL(file) })
}
async function imageData(src: string) {
  if (src.startsWith('data:')) return src.split(',')[1]
  const response = await fetch(src, { cache: 'no-store' })
  if (!response.ok) throw new Error('An image is no longer available. Upload it again.')
  return (await readFile(await response.blob())).split(',')[1]
}

export default function App() {
  const [sessions, setSessions] = useState<Summary[]>([])
  const [session, setSession] = useState<Session | null>(null)
  const [selected, setSelected] = useState(-1)
  const [settings, setSettings] = useState<Settings>(defaults)
  const [references, setReferences] = useState<Reference[]>([])
  const [edit, setEdit] = useState<AreaEdit | null>(null)
  const [editing, setEditing] = useState<number | null>(null)
  const [options, setOptions] = useState<Options | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const [sidebar, setSidebar] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [lightbox, setLightbox] = useState<{ src: string; title: string } | null>(null)
  const current = useRef<string | null>(null)
  const initialized = useRef(false)
  const upload = useRef<HTMLInputElement>(null)
  const latest = useRef<Session | null>(null)
  const busy = sending || active(session?.status)
  const entry = session?.entries[selected]
  const model = options?.models.find(m => m.id === settings.model)
  const title = sessions.find(s => s.id === session?.id)?.title || 'New session'

  const restore = useCallback((item: Entry) => {
    setSettings({ prompt: item.prompt, model: item.model, size: item.size, steps: item.steps, seed: item.seed })
    setReferences(item.references.map((src, i) => ({ src, name: `Image ${i + 1}` })))
    setEdit(item.edit || null); setEditing(null)
  }, [])
  const location = (id: string | null) => window.history.replaceState(null, '', id ? `?session=${id}` : window.location.pathname)
  const openSession = useCallback(async (id: string, signal?: AbortSignal) => {
    current.current = id; location(id); setError(''); setSidebar(false)
    setSession(null); latest.current = null; setSelected(-1); setReferences([]); setEdit(null); setEditing(null); setSettings(s => ({ ...s, prompt: '' }))
    try {
      const result = await getSession(id, signal)
      if (signal?.aborted || current.current !== id) return
      setSession(result); latest.current = result; setSelected(result.entries.length - 1)
      if (result.entries.length) restore(result.entries.at(-1)!)
    } catch (e) { if (!signal?.aborted && current.current === id) setError((e as Error).message) }
  }, [restore])

  useEffect(() => {
    let stopped = false
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const refresh = async () => {
      try {
        const [list, status, config] = await Promise.allSettled([
          api<Summary[]>('/api/sessions', { signal: controller.signal }),
          api<Health>('/api/health', { signal: controller.signal }),
          api<Options>('/api/options', { signal: controller.signal }),
        ])
        if (stopped) return
        setHealth(status.status === 'fulfilled' ? status.value : null)
        if (config.status === 'fulfilled') {
          setOptions(config.value)
          setSettings(s => s.model ? s : { ...s, model: config.value.models[0]?.id || '', size: config.value.models[0]?.sizes[0] || s.size, steps: config.value.steps })
        }
        if (list.status === 'fulfilled') {
          setSessions(list.value)
          if (!initialized.current) {
            initialized.current = true
            const requested = new URLSearchParams(window.location.search).get('session')
            const id = list.value.find(s => s.id === requested)?.id || list.value[0]?.id
            if (id) await openSession(id, controller.signal)
          } else if (current.current) {
            const id = current.current
            try {
              const result = await getSession(id, controller.signal)
              if (stopped || current.current !== id) return
              const added = result.entries.length > (latest.current?.entries.length || 0)
              setSession(result); latest.current = result
              if (added) { setSelected(result.entries.length - 1); restore(result.entries.at(-1)!) }
            } catch { /* Keep the current image during a brief connection failure. */ }
          }
        } else setError('Could not load your sessions. Check your connection and refresh.')
      } finally {
        // Session switches must not stop the polling loop.
        if (!stopped) timer = setTimeout(refresh, 2500)
      }
    }
    void refresh()
    return () => { stopped = true; controller.abort(); clearTimeout(timer) }
  }, [openSession, restore])

  function newSession() {
    current.current = null; location(null); latest.current = null
    setSession(null); setSelected(-1); setReferences([]); setEdit(null); setEditing(null); setError(''); setSidebar(false)
    setSettings(s => ({ ...s, prompt: '', seed: -1 }))
  }
  async function addFiles(files: FileList | File[]) {
    try {
      const items = Array.from(files)
      const limit = options?.max_references || 10
      if (references.length + items.length > limit) throw new Error(`Use up to ${limit} reference images.`)
      if (items.some(f => !['image/png', 'image/jpeg', 'image/webp'].includes(f.type))) throw new Error('Choose PNG, JPEG, or WebP images.')
      if (items.reduce((n, f) => n + f.size, 0) > 20 * 1024 * 1024) throw new Error('Choose smaller images (up to 20 MB per upload).')
      const added = await Promise.all(items.map(async file => { const src = await readFile(file); return { src, name: file.name, data: src.split(',')[1] } }))
      setReferences(previous => [...previous, ...added]); setError('')
    } catch (e) { setError((e as Error).message) }
  }
  async function generate() {
    if (!settings.prompt.trim() || busy) return
    setSending(true); setError('')
    const id = current.current
    try {
      const data = await Promise.all(references.map(ref => ref.data || imageData(ref.src)))
      const selection = edit ? { ...edit, mask: await imageData(edit.mask) } : null
      const result = await api<{ id: string }>('/api/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...settings, session: id, references: data, edit: selection }) })
      if (current.current === id) {
        current.current = result.id; location(result.id)
        const next = { id: result.id, entries: latest.current?.entries || [], status: 'queued' }
        setSession(next); latest.current = next
      }
    } catch (e) { setError((e as Error).message) }
    finally { setSending(false) }
  }
  async function deleteSession() {
    if (!session) return
    try { await api(`/api/sessions/${session.id}`, { method: 'DELETE' }); setSessions(s => s.filter(item => item.id !== session.id)); setConfirmDelete(false); newSession() }
    catch (e) { setError((e as Error).message); setConfirmDelete(false) }
  }
  function moveReference(index: number, offset: number) {
    setReferences(items => { const next = [...items]; [next[index], next[index + offset]] = [next[index + offset], next[index]]; return next })
    setEdit(value => value ? { ...value, base: value.base === index ? index + offset : value.base === index + offset ? index : value.base } : null)
  }
  function removeReference(index: number) {
    setReferences(items => items.filter((_, i) => i !== index))
    setEdit(value => !value || value.base === index ? null : { ...value, base: value.base > index ? value.base - 1 : value.base })
  }
  function saveSelection(mask: string, ratio: number, feather: number) {
    setEdit({ base: editing!, mask, feather }); setEditing(null)
    // Keep the selected image's framing with the closest available aspect ratio.
    const dimensions = (size: string) => size.split('x').map(Number)
    const [w, h] = dimensions(settings.size)
    const score = (size: string) => { const [x, y] = dimensions(size); return Math.abs(Math.log(x / y / ratio)) * 100 + Math.abs(Math.log(x * y / (w * h))) }
    const size = [...(model?.sizes || [settings.size])].sort((a, b) => score(a) - score(b))[0]
    setSettings(s => ({ ...s, size }))
  }
  const engine = health?.engine ? ({ exllamav3: 'ExLlamaV3', ninfer: 'NInfer', diffusers: 'Diffusers' }[health.engine] || health.engine) : ''

  return <div className="workspace">
    {sidebar && <button className="sidebar-backdrop" aria-label="Dismiss sidebar overlay" onClick={() => setSidebar(false)} />}
    <aside className={`sidebar ${sidebar ? 'is-open' : ''}`} aria-label="Sessions and settings">
      <div className="sidebar-top mobile-only"><Button variant="ghost" size="icon-sm" aria-label="Close sidebar" onClick={() => setSidebar(false)}><X /></Button></div>
      <section className="session-section"><h2>YOUR SESSIONS <span>{sessions.length || ''}</span></h2>
        <nav aria-label="Saved sessions">{sessions.map(item => <button key={item.id} className={`session-link ${session?.id === item.id ? 'selected' : ''}`} onClick={() => void openSession(item.id)} title={item.title}>
          <span>{item.title}</span>{active(item.status) && <LoaderCircle size={13} className="spinning" />}
        </button>)}</nav>
      </section>
      <Button variant="outline" className="new-session" onClick={newSession}><Plus /> New session</Button>
      <section className="settings">
        <div className="settings-body"><label>Model<select value={settings.model} onChange={e => { const m = options?.models.find(m => m.id === e.target.value); setSettings(s => ({ ...s, model: e.target.value, size: m?.sizes[0] || s.size })) }}>
          {!options?.models.length && <option value={settings.model}>{settings.model || 'Connect inference to choose'}</option>}{options?.models.map(m => <option key={m.id} value={m.id}>{m.label.split(' · ')[0]}</option>)}</select></label>
          <label>Image size<select value={settings.size} onChange={e => setSettings(s => ({ ...s, size: e.target.value }))}>{(model?.sizes || [settings.size]).map(size => <option key={size} value={size}>{size.replace('x', ' × ')}</option>)}</select></label>
          <label>Steps <span>More steps take longer</span><input type="number" min="1" max={options?.max_steps || 50} value={settings.steps} onChange={e => setSettings(s => ({ ...s, steps: Number(e.target.value) }))} /></label>
          <label>Seed<input type="number" min="-1" max="4294967295" value={settings.seed} onChange={e => setSettings(s => ({ ...s, seed: Number(e.target.value) }))} /></label>
        </div>
      </section>
      <section className="references" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); void addFiles(e.dataTransfer.files) }}>
        <div className="section-heading"><h2>REFERENCES</h2></div>
        <input ref={upload} type="file" accept="image/png,image/jpeg,image/webp" multiple className="sr-only" aria-label="Upload reference images" onChange={e => { if (e.target.files) void addFiles(e.target.files); e.target.value = '' }} />
        <div className="reference-grid">{references.map((ref, index) => <div className={`reference-tile ${edit?.base === index ? 'has-selection' : ''}`} key={`${ref.src}-${index}`}>
          <button className="reference-preview" aria-label={`Enlarge reference ${index + 1}`} onClick={() => setLightbox({ src: ref.src, title: `Image ${index + 1}` })}><img src={ref.src} alt={`Image ${index + 1}: ${ref.name}`} /><span>{index + 1}</span></button>
          <div className="reference-actions"><button aria-label={`Move reference ${index + 1} earlier`} disabled={!index} onClick={() => moveReference(index, -1)}><ArrowUp size={12} /></button><button aria-label={`Move reference ${index + 1} later`} disabled={index === references.length - 1} onClick={() => moveReference(index, 1)}><ArrowDown size={12} /></button><button aria-label={`Remove reference ${index + 1}`} onClick={() => removeReference(index)}><X size={13} /></button></div>
          <button className="edit-area-button" aria-label={`Edit area of image ${index + 1}`} onClick={() => setEditing(index)}>{edit?.base === index ? 'Area selected' : 'Edit area'}</button>
        </div>)}</div>
        {edit && <button className="clear-selection" onClick={() => setEdit(null)}>Remove selection <X size={12} /></button>}
        <button className="upload-button" onClick={() => upload.current?.click()}><ImagePlus size={17} /><span>{references.length ? 'Add another image' : 'Drop images or browse'}</span></button>
      </section>
      <section className="server-status" aria-label="Inference status"><div><span className={`status-dot ${health ? 'online' : ''}`} /><strong>{health ? health.busy ? 'Inference working' : health.loaded === false ? 'Image model unloaded' : 'Inference online' : 'Inference offline'}</strong></div>
        {health && <p>{engine ? `${engine} · ${health.active_model || (health.busy ? 'Loading model' : 'Model unloaded')}` : 'Idle · no model loaded'}</p>}
        {health?.note && <p>{health.note}</p>}
        {health?.busy && <progress className="inference-progress" aria-label="Inference progress" max={health.progress?.total || 1} value={health.progress && health.progress.completed < health.progress.total ? health.progress.completed : undefined} />}
        {!!health?.queued && <p>{health.queued} waiting</p>}
      </section>
      {session && <Button variant="ghost" className="delete-session" disabled={busy} onClick={() => setConfirmDelete(true)}><Trash2 size={14} /> Delete session</Button>}
    </aside>
    {editing !== null && references[editing] && <MaskEditor src={references[editing].src} mask={edit?.base === editing ? edit.mask : undefined} feather={edit?.base === editing ? edit.feather : undefined} onClose={() => setEditing(null)} onSave={saveSelection} />}
    <main className="main-workspace">
      <header className="workspace-header"><div><Button variant="ghost" size="icon" className="mobile-only" aria-label="Open sidebar" onClick={() => setSidebar(true)}><Menu /></Button><span className="session-title">{title}</span>{!!session?.entries.length && <span className="saved-mark"><Check size={12} /> Saved</span>}</div>{model && <span className="model-tag">{model.label.split(' · ')[0]}</span>}</header>
      <div className="creation-area">
        <section className="main-column">
          {entry ? <div className="image-stage"><img className="main-image" src={entry.image} alt={entry.prompt} /><div className="image-tools"><Button variant="secondary" size="icon" aria-label="Enlarge generated image" onClick={() => setLightbox({ src: entry.image, title: entry.prompt })}><Maximize2 /></Button><Button variant="secondary" size="icon" asChild><a href={entry.image} download="image.png" aria-label="Download image"><Download /></a></Button></div></div>
            : <div className="empty-stage"><div className="empty-icon"><Images size={27} strokeWidth={1.25} /></div></div>}
          {entry && <div className="image-caption"><p>{entry.prompt}</p><span>{entry.size.replace('x', ' × ')} <b>·</b> {entry.steps} steps <b>·</b> seed {entry.seed}</span></div>}
          <div className="composer-area">
            {(error || session?.error) && <div className="error-message" role="alert">{error || session?.error}{error && <button aria-label="Dismiss error" onClick={() => setError('')}><X size={14} /></button>}</div>}
            <form className="composer" onSubmit={e => { e.preventDefault(); void generate() }}><textarea aria-label="Prompt" placeholder="Describe what you imagine…" maxLength={4000} value={settings.prompt} onChange={e => setSettings(s => ({ ...s, prompt: e.target.value }))} onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); void generate() } }} /><div className="composer-footer"><Button type="submit" disabled={busy || !settings.prompt.trim() || !settings.model || !health}>{busy ? <><LoaderCircle className="spinning" /> {sending ? 'Sending' : session?.status === 'queued' ? 'Queued' : 'Generating'}</> : <>Generate <ArrowUpRight /></>}</Button></div></form>
            <p className="composer-note" role="status">{sending ? 'Sending your prompt and references…' : busy ? 'You can browse other sessions while this image is being created.' : ''}</p>
          </div>
        </section>
        {!!session?.entries.length && <aside className="image-rail" aria-label="Generated images"><span className="rail-label">IMAGES</span>{session.entries.map((image, index) => <button key={image.image} className={`image-thumb ${index === selected ? 'selected' : ''}`} aria-label={`View image ${index + 1}`} aria-pressed={index === selected} onClick={() => { setSelected(index); restore(image) }} title={image.prompt}><img src={image.image} alt="" /><span>{String(index + 1).padStart(2, '0')}</span></button>)}</aside>}
      </div>
    </main>
    <Dialog open={!!lightbox} onOpenChange={open => { if (!open) setLightbox(null) }}><DialogContent className="lightbox"><DialogTitle className="sr-only">{lightbox?.title || 'Image preview'}</DialogTitle><DialogDescription className="sr-only">Full image preview. Press Escape to close.</DialogDescription>{lightbox && <img src={lightbox.src} alt={lightbox.title} />}</DialogContent></Dialog>
    <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}><DialogContent><DialogTitle>Delete this session?</DialogTitle><DialogDescription>Its images, prompts, and references will be permanently removed. Other sessions will stay saved.</DialogDescription><div className="dialog-actions"><Button variant="outline" onClick={() => setConfirmDelete(false)}>Keep session</Button><Button variant="destructive" onClick={() => void deleteSession()}>Delete permanently</Button></div></DialogContent></Dialog>
  </div>
}
