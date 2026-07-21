import { X } from 'lucide-react'
import type { ReactNode } from 'react'

import { Card } from '../ui/card'
import { Input } from '../ui/input'
import { Label } from '../ui/label'
import { Switch } from '../ui/switch'
import { cn } from '@/lib/utils'

/**
 * A settings card: a titled block with an optional right-aligned footer action row.
 * Kept intentionally compact (tighter padding + a lighter footer than a generic Card)
 * because settings tabs stack several of these, and dense chrome reads as clutter.
 */
export function Section({
  title,
  description,
  children,
  footer,
  contentClassName,
}: {
  title: string
  description?: string
  children: ReactNode
  footer?: ReactNode
  /** Override the content wrapper — e.g. `p-0` when the body is an edge-to-edge list. */
  contentClassName?: string
}) {
  return (
    <Card className="overflow-hidden">
      <div className="flex flex-col gap-0.5 px-5 pt-4 pb-3">
        <h3 className="font-display text-[15px] leading-tight font-semibold tracking-tight">
          {title}
        </h3>
        {description && (
          <p className="text-muted-foreground text-[13px] leading-snug">{description}</p>
        )}
      </div>
      <div className={cn('space-y-3 px-5 pb-5', contentClassName)}>{children}</div>
      {footer && (
        <div className="border-border bg-muted/30 flex items-center justify-end gap-2 border-t px-5 py-3">
          {footer}
        </div>
      )}
    </Card>
  )
}

/** A labelled form control with an optional hint underneath. */
export function Field({
  label,
  hint,
  children,
  className,
}: {
  label: string
  hint?: string
  children: ReactNode
  className?: string
}) {
  return (
    <div className={cn('space-y-1.5', className)}>
      <Label>{label}</Label>
      {children}
      {hint && <p className="text-muted-foreground text-xs leading-relaxed">{hint}</p>}
    </div>
  )
}

/**
 * A bordered container that groups related rows into a single list with hairline
 * dividers between them, instead of each row carrying its own box. Consecutive
 * toggles collapse into one calm list — the main lever against the "too crowded" feel.
 */
export function Panel({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn('border-border divide-border divide-y rounded-lg border', className)}>
      {children}
    </div>
  )
}

/**
 * A single compact toggle row (title + description + Switch). Rendered as a `<label>`
 * so the whole row is clickable and screen-reader-associated. Meant to sit inside a
 * `<Panel>`; use it standalone by wrapping a single row in its own `<Panel>`.
 */
export function ToggleRow({
  title,
  description,
  checked,
  onCheckedChange,
  disabled,
}: {
  title: string
  description?: ReactNode
  checked: boolean
  onCheckedChange: (v: boolean) => void
  disabled?: boolean
}) {
  return (
    <label
      className={cn(
        'flex items-start justify-between gap-4 px-4 py-3',
        disabled ? 'cursor-default opacity-70' : 'cursor-pointer',
      )}
    >
      <span className="min-w-0">
        <span className="block text-sm font-medium">{title}</span>
        {description && (
          <span className="text-muted-foreground mt-0.5 block text-xs leading-relaxed">
            {description}
          </span>
        )}
      </span>
      <Switch
        checked={checked}
        onCheckedChange={onCheckedChange}
        disabled={disabled}
        className="mt-0.5 shrink-0"
      />
    </label>
  )
}

/**
 * A tinted note box for inline guidance. `muted` for neutral help text, `warning` for
 * consequential settings. Both tints are theme-aware via the design tokens.
 */
export function Callout({
  variant = 'muted',
  icon,
  children,
  className,
}: {
  variant?: 'muted' | 'warning'
  icon?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        'flex gap-2.5 rounded-lg border p-3 text-xs leading-relaxed',
        variant === 'warning'
          ? 'border-warning/40 bg-warning/10 text-foreground'
          : 'border-border bg-muted/40 text-muted-foreground',
        className,
      )}
    >
      {icon && <span className="mt-px shrink-0">{icon}</span>}
      <div className="min-w-0 space-y-2">{children}</div>
    </div>
  )
}

/**
 * Editable list of short tokens (reminder days, filename patterns, alert recipients)
 * shown as removable chips with a trailing add-input. Enter or blur-add is the caller's
 * `onAdd`; this component only renders the chips + input and reports edits.
 */
export function ChipInput<T extends string | number>({
  values,
  chipLabel,
  onRemove,
  input,
  onInputChange,
  onAdd,
  placeholder,
  removeLabel,
  tone = 'muted',
  mono = false,
  type,
  inputClassName,
}: {
  values: T[]
  chipLabel: (v: T) => string
  onRemove: (v: T) => void
  input: string
  onInputChange: (v: string) => void
  onAdd: () => void
  placeholder?: string
  removeLabel: string
  tone?: 'primary' | 'muted'
  mono?: boolean
  type?: string
  inputClassName?: string
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {values.map((v) => (
        <span
          key={String(v)}
          className={cn(
            'inline-flex items-center gap-1 rounded-md py-1 pr-1 pl-2.5 text-sm font-medium',
            tone === 'primary' ? 'bg-primary/10 text-primary' : 'bg-muted text-foreground',
            mono && 'font-mono',
          )}
        >
          {chipLabel(v)}
          <button
            type="button"
            onClick={() => onRemove(v)}
            aria-label={removeLabel}
            className="hover:text-danger rounded p-0.5 transition-colors"
          >
            <X className="size-3" />
          </button>
        </span>
      ))}
      <Input
        type={type}
        value={input}
        onChange={(e) => onInputChange(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), onAdd())}
        placeholder={placeholder}
        className={cn('h-8 w-32', mono && 'font-mono', inputClassName)}
      />
    </div>
  )
}
