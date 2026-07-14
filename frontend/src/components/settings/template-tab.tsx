import { useEffect, useState } from 'react'
import { Trans, useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../ui/select'
import { Switch } from '../ui/switch'
import { Tabs, TabsList, TabsTrigger } from '../ui/tabs'
import { Field, Section } from './section'
import type { SettingsTabProps } from '@/pages/settings'
import { api } from '@/lib/api'
import { translateError } from '@/lib/errors'

const PLACEHOLDERS = [
  'displayName',
  'upn',
  'daysLeft',
  'expiryDate',
  'resetUrl',
  'companyName',
  'logoUrl',
]

export function TemplateTab({ settings, save, saving }: SettingsTabProps) {
  const { t } = useTranslation()
  const [lang, setLang] = useState<'de' | 'en'>('de')
  const [subject, setSubject] = useState('')
  const [html, setHtml] = useState('')
  const [preview, setPreview] = useState('')
  const [perUser, setPerUser] = useState(Boolean(settings['template.language_per_user'] ?? true))
  const [defaultLang, setDefaultLang] = useState(
    String(settings['template.language_default'] ?? 'de'),
  )

  const saveLanguage = () =>
    save({ 'template.language_per_user': perUser, 'template.language_default': defaultLang })

  // Bei Sprachwechsel Felder aus den Settings laden
  useEffect(() => {
    setSubject(String(settings[`template.subject_${lang}`] ?? ''))
    setHtml(String(settings[`template.html_${lang}`] ?? ''))
  }, [lang, settings])

  // Live-Vorschau (debounced)
  useEffect(() => {
    const t = setTimeout(async () => {
      try {
        const res = await api.post<{ subject: string; html: string }>(
          '/settings/template/preview',
          {
            subject,
            html,
            locale: lang,
          },
        )
        setPreview(res.html)
      } catch {
        /* ignore */
      }
    }, 400)
    return () => clearTimeout(t)
  }, [subject, html, lang])

  const onSave = () =>
    save({ [`template.subject_${lang}`]: subject, [`template.html_${lang}`]: html })

  const onReset = async () => {
    try {
      const data = await api.post<Record<string, unknown>>('/settings/template/reset', {})
      setSubject(String(data[`template.subject_${lang}`] ?? ''))
      setHtml(String(data[`template.html_${lang}`] ?? ''))
      toast.success(t('templateTab.resetSuccess'))
    } catch (e) {
      toast.error(translateError(e))
    }
  }

  return (
    <div className="space-y-4">
      <Section
        title={t('templateTab.language.title')}
        description={t('templateTab.language.description')}
        footer={
          <Button onClick={saveLanguage} loading={saving}>
            {t('templateTab.save')}
          </Button>
        }
      >
        <div className="border-border flex items-center justify-between rounded-lg border p-4">
          <div>
            <p className="text-sm font-medium">{t('templateTab.perUser.title')}</p>
            <p className="text-muted-foreground text-xs">
              <Trans
                i18nKey="templateTab.perUser.description"
                components={{ code: <code className="font-mono" /> }}
              />
            </p>
          </div>
          <Switch checked={perUser} onCheckedChange={setPerUser} />
        </div>
        <Field label={t('templateTab.defaultLang.label')} hint={t('templateTab.defaultLang.hint')}>
          <Select value={defaultLang} onValueChange={setDefaultLang}>
            <SelectTrigger className="max-w-48">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="de">{t('templateTab.langOptions.de')}</SelectItem>
              <SelectItem value="en">{t('templateTab.langOptions.en')}</SelectItem>
            </SelectContent>
          </Select>
        </Field>
      </Section>

      <Section
        title={t('templateTab.template.title')}
        description={t('templateTab.template.description')}
        footer={
          <>
            <Button variant="ghost" onClick={onReset}>
              {t('templateTab.resetToDefault')}
            </Button>
            <Button onClick={onSave} loading={saving}>
              {t('templateTab.save')}
            </Button>
          </>
        }
      >
        <Tabs value={lang} onValueChange={(v) => setLang(v as 'de' | 'en')}>
          <TabsList>
            <TabsTrigger value="de">{t('templateTab.langOptions.de')}</TabsTrigger>
            <TabsTrigger value="en">{t('templateTab.langOptions.en')}</TabsTrigger>
          </TabsList>
        </Tabs>

        <Field label={t('templateTab.subject')}>
          <Input
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            className="font-mono text-sm"
          />
        </Field>

        <div>
          <p className="text-muted-foreground mb-1.5 text-xs">
            {t('templateTab.placeholders')}{' '}
            {PLACEHOLDERS.map((p) => (
              <code key={p} className="bg-muted mr-1 rounded px-1 py-0.5 font-mono text-[11px]">
                {`{{ ${p} }}`}
              </code>
            ))}
          </p>
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <Field label={t('templateTab.html')}>
            <textarea
              value={html}
              onChange={(e) => setHtml(e.target.value)}
              spellCheck={false}
              className="border-input bg-card focus-visible:ring-ring h-80 w-full resize-none rounded-md border p-3 font-mono text-[16px] shadow-sm focus-visible:ring-2 focus-visible:outline-none sm:text-xs"
            />
          </Field>
          <Field label={t('templateTab.livePreview')}>
            <iframe
              title={t('templateTab.previewTitle')}
              srcDoc={preview}
              className="border-border h-80 w-full rounded-md border bg-white"
              sandbox=""
            />
          </Field>
        </div>
      </Section>
    </div>
  )
}
