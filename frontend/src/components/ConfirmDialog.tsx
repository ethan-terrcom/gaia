import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { Button } from './ui/Button'
import { Field } from './ui/Field'
import { Input } from './ui/Input'

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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-[3px]">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="w-full max-w-lg rounded-[var(--radius-xl)] border border-[var(--color-border)] bg-[var(--color-surface-overlay)] p-5 shadow-[var(--shadow-panel)]"
      >
        <h2
          id={titleId}
          className="text-[16px] font-semibold tracking-[-0.02em] text-[var(--color-ink)]"
        >
          {title}
        </h2>
        {children ? (
          <div className="mt-3 text-[13px] leading-relaxed text-[var(--color-ink-muted)]">
            {children}
          </div>
        ) : null}
        {requireText !== undefined ? (
          <div className="mt-4">
            <Field label={`Type ${requireText} to confirm`} htmlFor={inputId}>
              <Input
                id={inputId}
                value={typed}
                onChange={(event) => setTyped(event.target.value)}
                autoComplete="off"
                className="font-mono"
              />
            </Field>
          </div>
        ) : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button ref={cancelRef} variant="secondary" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            variant={destructive ? 'danger' : 'primary'}
            onClick={onConfirm}
            disabled={!canConfirm}
          >
            {busy ? 'Working…' : confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  )
}
