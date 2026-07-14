import { Check, Copy, ExternalLink } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { Badge } from './ui/badge'
import { Button } from './ui/button'

const PERMISSIONS = [
  { name: 'User.Read.All', whyKey: 'entraGuide.permissions.userReadAll', optional: false },
  { name: 'Domain.Read.All', whyKey: 'entraGuide.permissions.domainReadAll', optional: false },
  { name: 'Mail.Send', whyKey: 'entraGuide.permissions.mailSend', optional: false },
  {
    name: 'GroupMember.Read.All',
    whyKey: 'entraGuide.permissions.groupMemberReadAll',
    optional: true,
  },
]

const STEP_KEYS = [
  'entraGuide.steps.step1',
  'entraGuide.steps.step2',
  'entraGuide.steps.step3',
  'entraGuide.steps.step4',
  'entraGuide.steps.step5',
  'entraGuide.steps.step6',
]

export function EntraGuide() {
  const { t } = useTranslation()
  const [copied, setCopied] = useState<string | null>(null)

  const copy = (text: string) => {
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(text)
      toast.success(t('entraGuide.copied'))
      setTimeout(() => setCopied(null), 1500)
    })
  }

  return (
    <div className="border-border bg-muted/40 rounded-lg border p-4">
      <div className="flex items-center justify-between">
        <h4 className="font-display text-sm font-semibold">{t('entraGuide.title')}</h4>
        <Button variant="outline" size="sm" asChild>
          <a
            href="https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"
            target="_blank"
            rel="noreferrer"
          >
            {t('entraGuide.openEntra')} <ExternalLink className="size-3.5" />
          </a>
        </Button>
      </div>

      <ol className="mt-3 space-y-2">
        {STEP_KEYS.map((stepKey, i) => (
          <li key={i} className="text-muted-foreground flex gap-3 text-sm">
            <span className="bg-primary/15 text-primary grid size-5 shrink-0 place-items-center rounded-full text-[11px] font-semibold">
              {i + 1}
            </span>
            <span>{t(stepKey)}</span>
          </li>
        ))}
      </ol>

      <div className="mt-4">
        <p className="text-muted-foreground mb-2 text-xs font-medium tracking-wide uppercase">
          {t('entraGuide.permissionsHeading')}
        </p>
        <div className="space-y-1.5">
          {PERMISSIONS.map((p) => (
            <div
              key={p.name}
              className="border-border bg-card flex items-center justify-between gap-3 rounded-md border px-3 py-2"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <code className="font-mono text-xs font-semibold">{p.name}</code>
                  {p.optional && (
                    <Badge variant="outline" className="shrink-0">
                      {t('entraGuide.optionalBadge')}
                    </Badge>
                  )}
                </div>
                <p className="text-muted-foreground truncate text-xs">{t(p.whyKey)}</p>
              </div>
              <button
                type="button"
                onClick={() => copy(p.name)}
                className="text-muted-foreground hover:text-foreground shrink-0 rounded p-1"
                aria-label={t('entraGuide.copyAriaLabel', { name: p.name })}
              >
                {copied === p.name ? (
                  <Check className="text-success size-3.5" />
                ) : (
                  <Copy className="size-3.5" />
                )}
              </button>
            </div>
          ))}
        </div>
        <div className="text-muted-foreground mt-3 flex items-start gap-2 text-xs">
          <Badge variant="outline">{t('entraGuide.importantBadge')}</Badge>
          <span>
            {t('entraGuide.importantNote1')}{' '}
            <strong>{t('entraGuide.importantAppPerms')}</strong> {t('entraGuide.importantNote2')}{' '}
            <strong> {t('entraGuide.importantConsent')}</strong> {t('entraGuide.importantNote3')}
          </span>
        </div>
      </div>
    </div>
  )
}
