import { Eye, EyeOff } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { PasswordChecklist } from '@/components/password-checklist'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

/**
 * Passwort + Bestätigung mit gemeinsamem Sichtbarkeits-Umschalter (ein Auge-Icon steuert
 * `type` beider Felder gleichzeitig) und der Live-Checkliste (Task 6) unter dem
 * Passwort-Feld. Geteilt zwischen Einladung-annehmen (`accept-invitation.tsx`) und
 * Passwort-zurücksetzen (`reset-password.tsx`, Task 8) — beide brauchen exakt dasselbe
 * Passwort-Paar, nur der Rest des Formulars unterscheidet sich.
 */
export function PasswordFields({
  password,
  onPasswordChange,
  confirm,
  onConfirmChange,
  passwordLabel,
  confirmLabel,
}: {
  password: string
  onPasswordChange: (v: string) => void
  confirm: string
  onConfirmChange: (v: string) => void
  passwordLabel: string
  confirmLabel: string
}) {
  const { t } = useTranslation()
  const [visible, setVisible] = useState(false)
  const type = visible ? 'text' : 'password'

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <Label htmlFor="pf-password">{passwordLabel}</Label>
          <button
            type="button"
            onClick={() => setVisible((v) => !v)}
            className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-xs"
          >
            {visible ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
            {visible ? t('password.hide') : t('password.show')}
          </button>
        </div>
        <Input
          id="pf-password"
          type={type}
          autoComplete="new-password"
          value={password}
          onChange={(e) => onPasswordChange(e.target.value)}
        />
        <PasswordChecklist password={password} />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="pf-confirm">{confirmLabel}</Label>
        <Input
          id="pf-confirm"
          type={type}
          autoComplete="new-password"
          value={confirm}
          onChange={(e) => onConfirmChange(e.target.value)}
        />
      </div>
    </div>
  )
}
