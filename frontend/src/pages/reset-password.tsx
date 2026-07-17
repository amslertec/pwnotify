import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { PasswordFields } from '@/components/password-fields'
import { PublicAuthLayout } from '@/components/public-auth-layout'
import { Button } from '@/components/ui/button'
import { api, ApiError } from '@/lib/api'
import { translateError } from '@/lib/errors'
import { passwordsMatch, passwordValid } from '@/lib/password'
import { resolveTokenGate, type TokenGateState } from '@/lib/token-gate'
import type { TokenInfo } from '@/lib/types'

export interface ResetFormState {
  password: string
  confirm: string
}

/** Freigabe-Bedingung fürs Absenden — kein Benutzername/Namen hier: das Konto ist über
 *  das Token bereits fixiert (§7c). */
export function canSubmitReset(v: ResetFormState): boolean {
  return passwordValid(v.password) && passwordsMatch(v.password, v.confirm)
}

export type ResetSubmitError = 'invalid' | 'other'

/** Ordnet einen Backend-Fehlercode aus `POST /public/token/reset` einer Reaktion zu:
 *  auf die Invalid-Ansicht wechseln, oder generisch per Toast melden (`password_policy`). */
export function classifyResetError(code: string | undefined): ResetSubmitError {
  return code === 'token_invalid' ? 'invalid' : 'other'
}

export default function ResetPasswordPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const token = params.get('token')

  const { data: info, isLoading } = useQuery({
    queryKey: ['token-info', 'reset', token],
    queryFn: () =>
      api.get<TokenInfo>(
        `/public/token/info?token=${encodeURIComponent(token ?? '')}&purpose=reset`,
      ),
    enabled: !!token,
    retry: false,
  })

  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [forcedInvalid, setForcedInvalid] = useState(false)

  const submit = useMutation({
    mutationFn: () => api.post('/public/token/reset', { token, password }),
    onSuccess: () => {
      toast.success(t('passwordReset.success'))
      navigate('/login')
    },
    onError: (e) => {
      const code = e instanceof ApiError ? e.code : undefined
      if (classifyResetError(code) === 'invalid') {
        setForcedInvalid(true)
        return
      }
      toast.error(translateError(e))
    },
  })

  const gate: TokenGateState = forcedInvalid ? 'invalid' : resolveTokenGate(token, isLoading, info ?? null)

  if (gate === 'loading') {
    return (
      <PublicAuthLayout title={t('passwordReset.title')}>
        <p className="text-muted-foreground text-sm">{t('passwordReset.loading')}</p>
      </PublicAuthLayout>
    )
  }

  if (gate === 'missing' || gate === 'invalid') {
    return (
      <PublicAuthLayout title={t('passwordReset.invalidTitle')}>
        <p className="text-muted-foreground text-sm">{t('passwordReset.invalidDescription')}</p>
        <Button className="mt-4 w-full" variant="outline" onClick={() => navigate('/login')}>
          {t('passwordReset.backToLogin')}
        </Button>
      </PublicAuthLayout>
    )
  }

  const canSubmit = canSubmitReset({ password, confirm })

  return (
    <PublicAuthLayout title={t('passwordReset.title')} subtitle={t('passwordReset.subtitle')}>
      <div className="space-y-4">
        <PasswordFields
          password={password}
          onPasswordChange={setPassword}
          confirm={confirm}
          onConfirmChange={setConfirm}
          passwordLabel={t('passwordReset.password')}
          confirmLabel={t('passwordReset.confirmPassword')}
        />
        <Button
          className="w-full"
          onClick={() => submit.mutate()}
          loading={submit.isPending}
          disabled={!canSubmit}
        >
          {t('passwordReset.submitButton')}
        </Button>
      </div>
    </PublicAuthLayout>
  )
}
