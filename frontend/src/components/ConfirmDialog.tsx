import { useEffect, useId, useRef, useState, type ReactNode } from 'react'

/**
 * The confirmation every destructive action needs (doc 17, "Shared rules").
 *
 * ``requireText`` makes the user type an exact phrase before the confirm
 * button enables. 17d's merge approval uses it (`merge <pr#>`), and so does
 * any force-delete. Escape and the cancel button both dismiss the dialog.
 */
type ConfirmDialogProps = {
  open: boolean
  title: string
  children?: ReactNode
  confirmLabel?: string
  destructive?: boolean
  requireText?: string
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}

export function ConfirmDialog(props: ConfirmDialogProps) {
  // The body mounts only while open, so the typed phrase resets on close
  // without a state update in an effect.
  return props.open ? <DialogBody {...props} /> : null
}

function DialogBody({
  title,
  children,
  confirmLabel = 'Confirm',
  destructive = false,
  requireText,
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const [typed, setTyped] = useState('')
  const titleId = useId()
  const inputId = useId()
  const cancelRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    cancelRef.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onCancel()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])

  const canConfirm =
    !busy && (requireText === undefined || typed === requireText)
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="w-full max-w-md rounded-lg bg-white p-5 shadow-xl dark:bg-gray-900"
      >
        <h2 id={titleId} className="text-lg font-semibold">
          {title}
        </h2>
        {children ? <div className="mt-3 text-sm">{children}</div> : null}
        {requireText !== undefined ? (
          <div className="mt-4">
            <label htmlFor={inputId} className="block text-sm">
              Type <code className="font-mono">{requireText}</code> to confirm
            </label>
            <input
              id={inputId}
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              autoComplete="off"
              className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-950"
            />
          </div>
        ) : null}
        <div className="mt-5 flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="rounded border border-gray-300 px-3 py-1.5 dark:border-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={!canConfirm}
            className={`rounded px-3 py-1.5 text-white disabled:opacity-50 ${
              destructive ? 'bg-red-600' : 'bg-blue-600'
            }`}
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
