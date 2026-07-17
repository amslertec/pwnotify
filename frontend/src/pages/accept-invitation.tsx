import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { PasswordFields } from '@/components/password-fields'
import { PublicAuthLayout } from '@/components/public-auth-layout'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { api, ApiError } from '@/lib/api'
import { translateError } from '@/lib/errors'
import { passwordsMatch, passwordValid } from '@/lib/password'
import { resolveTokenGate } from '@/lib/token-gate'
import type { TokenInfo } from '@/lib/types'

export interface AcceptFormState {
  firstName: string
  lastName: string
  username: string
  password: string
  confirm: string
}

/** Freigabe-Bedingung fürs Absenden — rein und ohne DOM testbar. Deckt: Passwort-Policy +
 *  Übereinstimmung (Task 6, `lib/password.ts`), Mindestlänge Benutzername, nicht-leere
 *  Vor-/Nachname. */
export function canSubmitAccept(v: AcceptFormState): boolean {
  return (
    v.firstName.trim().length > 0 &&
    v.lastName.trim().length > 0 &&
    v.username.trim().length >= 3 &&
    passwordValid(v.password) &&
    passwordsMatch(v.password, v.confirm)
  )
}

export type AcceptSubmitError = 'username_taken' | 'invalid' | 'other'

/** Ordnet einen Backend-Fehlercode aus `POST /public/token/accept` einer der drei
 *  Reaktionen zu: Feldfehler + Formular behalten (Retry möglich, Token bleibt gültig),
 *  auf die Invalid-Ansicht wechseln, oder generisch per Toast melden (z. B. `password_policy`). */
export function classifyAcceptError(code: string | undefined): AcceptSubmitError {
  if (code === 'username_taken') return 'username_taken'
  if (code === 'token_invalid') return 'invalid'
  return 'other'
}

export default function AcceptInvitationPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const token = params.get('token')

  const { data: info, isLoading } = useQuery({
    queryKey: ['token-info', 'invite', token],
    queryFn: () =>
      api.get<TokenInfo>(
        `/public/token/info?token=${encodeURIComponent(token ?? '')}&purpose=invite`,
      ),
    enabled: !!token,
    retry: false,
  })

  const [firstName, setFirstName] = useState('')
  const [lastName, setLastName] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [usernameError, setUsernameError] = useState('')
  const [forcedInvalid, setForcedInvalid] = useState(false)

  const submit = useMutation({
    mutationFn: () =>
      api.post('/public/token/accept', {
        token,
        first_name: firstName.trim(),
        last_name: lastName.trim(),
        username: username.trim(),
        password,
      }),
    onSuccess: () => {
      toast.success(t('invite.accepted'))
      navigate('/login')
    },
    onError: (e) => {
      const code = e instanceof ApiError ? e.code : undefined
      const kind = classifyAcceptError(code)
      if (kind === 'username_taken') {
        setUsernameError(t('invite.usernameTaken'))
        return
      }
      if (kind === 'invalid') {
        setForcedInvalid(true)
        return
      }
      toast.error(translateError(e))
    },
  })

  const gate = forcedInvalid ? 'invalid' : resolveTokenGate(token, isLoading, info ?? null)

  if (gate === 'loading') {
    return (
      <PublicAuthLayout title={t('invite.title')}>
        <p className="text-muted-foreground text-sm">{t('invite.loading')}</p>
      </PublicAuthLayout>
    )
  }

  if (gate === 'missing' || gate === 'invalid') {
    return (
      <PublicAuthLayout title={t('invite.invalidTitle')}>
        <p className="text-muted-foreground text-sm">{t('invite.invalidDescription')}</p>
        <Button className="mt-4 w-full" variant="outline" onClick={() => navigate('/login')}>
          {t('invite.backToLogin')}
        </Button>
      </PublicAuthLayout>
    )
  }

  const canSubmit = canSubmitAccept({ firstName, lastName, username, password, confirm })

  return (
    <PublicAuthLayout title={t('invite.title')} subtitle={t('invite.subtitle')}>
      <div className="space-y-4">
        <div className="space-y-1.5">
          <Label>{t('invite.emailLabel')}</Label>
          <Input value={info?.email ?? ''} readOnly disabled />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <Label htmlFor="invite-first">{t('invite.firstName')}</Label>
            <Input
              id="invite-first"
              value={firstName}
              onChange={(e) => setFirstName(e.target.value)}
              autoFocus
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="invite-last">{t('invite.lastName')}</Label>
            <Input id="invite-last" value={lastName} onChange={(e) => setLastName(e.target.value)} />
          </div>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="invite-username">{t('invite.username')}</Label>
          <Input
            id="invite-username"
            autoComplete="username"
            value={username}
            onChange={(e) => {
              setUsername(e.target.value)
              setUsernameError('')
            }}
          />
          {usernameError && <p className="text-danger text-xs">{usernameError}</p>}
        </div>
        <PasswordFields
          password={password}
          onPasswordChange={setPassword}
          confirm={confirm}
          onConfirmChange={setConfirm}
          passwordLabel={t('invite.password')}
          confirmLabel={t('invite.confirmPassword')}
        />
        <Button
          className="w-full"
          onClick={() => submit.mutate()}
          loading={submit.isPending}
          disabled={!canSubmit}
        >
          {t('invite.submitButton')}
        </Button>
      </div>
    </PublicAuthLayout>
  )
}
