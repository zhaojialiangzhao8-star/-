import { useEffect, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../services/api'
import { Modal } from '../components/ui'

/**
 * 策略编辑弹窗 —— 只改 META（名称/描述/标签/参数默认值），源码逻辑不动。
 * 后端会先把原文件备份成 `<file>.bak`。
 */
export default function StrategyEdit({
  id,
  onClose,
  onSaved,
}: {
  id: string
  onClose: () => void
  onSaved: () => void
}) {
  const q = useQuery({
    queryKey: ['strategy-detail', id],
    queryFn: () => api.strategyDetail(id),
  })

  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [tags, setTags] = useState('')
  const [params, setParams] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const meta = q.data?.meta as
    | { name?: string; description?: string; tags?: string[]; params?: { id: string; default: unknown }[] }
    | undefined

  useEffect(() => {
    if (!meta) return
    setName(meta.name ?? '')
    setDesc(meta.description ?? '')
    setTags((meta.tags ?? []).join(', '))
    setParams(
      (meta.params ?? [])
        .map((p) => `${p.id}=${typeof p.default === 'string' ? p.default : JSON.stringify(p.default)}`)
        .join('\n'),
    )
  }, [meta])

  const save = async () => {
    setErr('')
    setBusy(true)
    try {
      // 参数默认值：每行 `id=值`，值能当数字/布尔就转换，否则按字符串
      const nums = (meta?.params ?? []).map((p) => {
        const line = params.split('\n').find((l) => l.trim().startsWith(p.id + '='))
        if (!line) return { ...p }
        let v: unknown = line.slice(line.indexOf('=') + 1).trim()
        if (v === 'true') v = true
        else if (v === 'false') v = false
        else if (v !== '' && !Number.isNaN(Number(v))) v = Number(v)
        return { ...p, default: v }
      })
      await api.updateStrategy(id, {
        name,
        description: desc,
        tags: tags.split(/[,，]/).map((t) => t.trim()).filter(Boolean),
        params: nums,
      })
      onSaved()
      onClose()
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const readonly = q.data && !q.data.editable

  return (
    <Modal title="编辑策略" subtitle={id} onClose={onClose} width="max-w-[640px]">
      <div className="space-y-3 p-4">
        {q.isLoading && <div className="text-xs text-fg3">加载中…</div>}
        {readonly && (
          <div className="rounded-md border border-gold/40 bg-gold/10 px-3 py-2 text-sm2 text-gold">
            这个策略的 META 不是纯字面量（可能引用了变量或函数），改名有风险，已禁止编辑。
            需要改的话请直接改源码。
          </div>
        )}
        <Field label="策略名称">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={!!readonly}
            className="w-full rounded-md border border-line bg-card2 px-2.5 py-1.5 text-xs outline-none focus:border-brand/60 disabled:opacity-50"
          />
        </Field>
        <Field label="描述">
          <textarea
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            disabled={!!readonly}
            rows={2}
            className="w-full resize-none rounded-md border border-line bg-card2 px-2.5 py-1.5 text-xs outline-none focus:border-brand/60 disabled:opacity-50"
          />
        </Field>
        <Field label="标签（逗号分隔）">
          <input
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            disabled={!!readonly}
            className="w-full rounded-md border border-line bg-card2 px-2.5 py-1.5 text-xs outline-none focus:border-brand/60 disabled:opacity-50"
          />
        </Field>
        <Field label="参数默认值（每行 id=值）">
          <textarea
            value={params}
            onChange={(e) => setParams(e.target.value)}
            disabled={!!readonly}
            rows={Math.max(3, params.split('\n').length)}
            className="w-full resize-none rounded-md border border-line bg-card2 px-2.5 py-1.5 font-mono text-sm2 outline-none focus:border-brand/60 disabled:opacity-50"
          />
        </Field>

        {err && (
          <div className="rounded-md border border-up/40 bg-up/10 px-3 py-2 text-sm2 text-up">
            {err}
          </div>
        )}

        <div className="flex items-center gap-2 pt-1">
          <button
            onClick={save}
            disabled={busy || !!readonly}
            className="rounded-md bg-brand px-4 py-1.5 text-xs font-semibold text-white disabled:opacity-40"
          >
            {busy ? '保存中…' : '保存'}
          </button>
          <button
            onClick={onClose}
            className="rounded-md border border-line bg-card px-4 py-1.5 text-xs text-fg2 hover:text-fg"
          >
            取消
          </button>
          <span className="ml-auto text-xs2 text-fg3">
            保存前会自动备份原文件为 .bak
          </span>
        </div>
      </div>
    </Modal>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs2 text-fg3">{label}</span>
      {children}
    </label>
  )
}
