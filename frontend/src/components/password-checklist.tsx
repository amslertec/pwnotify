import { Check } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { checkPassword, type PasswordRules } from '@/lib/password'
import { cn } from '@/lib/utils'

const RULES: { key: keyof PasswordRules; labelKey: string }[] = [
  { key: 'length', labelKey: 'password.rules.length10' },
  { key: 'upper', labelKey: 'password.rules.upper' },
  { key: 'lower', labelKey: 'password.rules.lower' },
  { key: 'digit', labelKey: 'password.rules.digit' },
  { key: 'special', labelKey: 'password.rules.special' },
]

/** Live-Checkliste der Passwort-Policy. Rein präsentational — keine Formular-/Fetch-Logik. */
export function PasswordChecklist({ password }: { password: string }) {
  const { t } = useTranslation()
  const rules = checkPassword(password)

  return (
    <ul className="space-y-1">
      {RULES.map(({ key, labelKey }) => {
        const met = rules[key]
        return (
          <li
            key={key}
            className={cn(
              'flex items-center gap-2 text-xs transition-colors',
              met ? 'text-success' : 'text-muted-foreground',
            )}
          >
            <span
              className={cn(
                'grid size-4 place-items-center rounded-full border',
                met ? 'border-success bg-success text-white' : 'border-border',
              )}
            >
              {met && <Check className="size-3" />}
            </span>
            {t(labelKey)}
          </li>
        )
      })}
    </ul>
  )
}
